"""Shared air-quality commands for webhook and polling deployments."""
import asyncio
import base64
import binascii
import html
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytz
from PIL import Image, ImageDraw
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)
AQICN_API_KEY = os.getenv("AQICN_API_KEY")

def _aqicn_request(path: str, params: dict | None = None) -> dict:
    """Fetch one AQICN JSON feed using this bot's private API token."""
    if not AQICN_API_KEY:
        raise RuntimeError("AQICN_API_KEY is not configured.")
    query = {"token": AQICN_API_KEY, **(params or {})}
    url = f"https://api.waqi.info/{path}?{urlencode(query)}"
    with urlopen(url, timeout=15) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "ok":
        raise RuntimeError(payload.get("data", "AQICN returned an unexpected response."))
    return payload["data"]


SINGAPORE_REGIONS = ("North", "South", "East", "West", "Central")
AQICN_CITY_PAGES = {
    region: f"https://aqicn.org/city/singapore/{region.lower()}/"
    for region in SINGAPORE_REGIONS
}


def _parallel_fetch(items, fetch, max_workers: int = 6):
    """Run independent, blocking AQICN requests concurrently."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(max_workers, len(items) or 1)) as pool:
        return list(pool.map(fetch, items))


def get_singapore_psi() -> list[dict]:
    """Fetch the five regional AQICN feeds; Singapore aliases South."""
    def fetch_region(region):
        try:
            feed = _aqicn_request(f"feed/Singapore/{region}/")
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError) as exc:
            logger.warning("AQICN feed unavailable for %s: %s", region, exc)
            feed = None
        return _compact_aqicn_feed(region, feed)

    return _parallel_fetch(SINGAPORE_REGIONS, fetch_region)


def _compact_aqicn_feed(region: str, feed: dict | None) -> dict:
    """Keep the fields needed by the PM display and preserve missing regions."""
    if not feed:
        return {"region": region, "aqi": None, "iaqi": {}, "time": {}}
    return {
        "region": region,
        "aqi": feed.get("aqi"),
        "iaqi": feed.get("iaqi") or {},
        "time": feed.get("time") or {},
        "city": feed.get("city") or {},
    }


def _pollutant_aqi(feed: dict, pollutant: str):
    """Return AQICN's pollutant-specific AQI, or None when it is unavailable."""
    entry = (feed.get("iaqi") or {}).get(pollutant)
    return entry.get("v") if isinstance(entry, dict) else None


def _number(value) -> float | None:
    """Safely coerce AQICN's numeric fields, excluding non-finite values."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _mean(values) -> float | None:
    numeric_values = [number for value in values if (number := _number(value)) is not None]
    return sum(numeric_values) / len(numeric_values) if numeric_values else None


def _display_number(value) -> str:
    number = _number(value)
    if number is None:
        return "N/A"
    return str(round(number)) if number.is_integer() else f"{number:.1f}"


def get_latest_nea_psi() -> dict:
    request = Request(_NEA_AIR_API, headers={"User-Agent": "BloodPressureBot/1.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != 0:
        raise RuntimeError("NEA PSI unavailable")
    items = (payload.get("data") or {}).get("items") or []
    if not items:
        raise RuntimeError("NEA PSI unavailable")
    return max(items, key=lambda item: item.get("timestamp", ""))


def _format_comparison(stations, nea):
    readings = nea.get("readings") or {}
    lines = ["<b>Singapore air quality</b>",
             "? AQICN: AQI scale", "? NEA: 24-hour PSI scale",
             "? Comparison: different scales / averaging periods", "",
             "<b>Regional means</b>"]
    for label, aq_key, nea_key in (("PM2.5", "pm25", "pm25_sub_index"),
                                  ("PM10", "pm10", "pm10_sub_index")):
        aq = [_number(_pollutant_aqi(s, aq_key)) for s in stations]
        ne = [_number((readings.get(nea_key) or {}).get(r.lower())) for r in SINGAPORE_REGIONS]
        lines.append(f"? {label} AQI: {_display_number(_mean(aq))} ({sum(v is not None for v in aq)}/5)")
        lines.append(f"? {label} NEA index: {_display_number(_mean(ne))} ({sum(v is not None for v in ne)}/5)")
    for station in stations:
        region = station["region"]
        key = region.lower()
        def nea_value(metric):
            return _display_number((readings.get(metric) or {}).get(key))
        lines.extend(["", f"<b>{html.escape(region)}</b>",
            f"? AQICN AQI: {_display_number(station.get('aqi'))}",
            f"? NEA PSI (24h): {nea_value('psi_twenty_four_hourly')}",
            f"? PM2.5 AQI: {_display_number(_pollutant_aqi(station, 'pm25'))}",
            f"? PM2.5 NEA index: {nea_value('pm25_sub_index')}",
            f"? PM10 AQI: {_display_number(_pollutant_aqi(station, 'pm10'))}",
            f"? PM10 NEA index: {nea_value('pm10_sub_index')}",
            f"? AQICN updated: {html.escape(str(station.get('time', {}).get('s') or 'N/A'))}"])
    lines.extend(["", f"? NEA updated: {html.escape(str(nea.get('timestamp') or 'N/A'))}",
                  "? Sources: AQICN / NEA via data.gov.sg"])
    return "\n".join(lines)


async def psi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    results = await asyncio.gather(asyncio.to_thread(get_singapore_psi),
                                   asyncio.to_thread(get_latest_nea_psi), return_exceptions=True)
    stations, nea = results
    if isinstance(stations, Exception):
        logger.warning("AQICN readings unavailable: %s", stations)
        stations = [_compact_aqicn_feed(r, None) for r in SINGAPORE_REGIONS]
    if isinstance(nea, Exception):
        logger.warning("NEA readings unavailable: %s", nea)
        nea = {}
    await update.effective_message.reply_text(_format_comparison(stations, nea), parse_mode=ParseMode.HTML)


_NEA_AIR_API = "https://api-open.data.gov.sg/v2/real-time/api/psi"
_NEA_REGION_KEYS = {region.lower(): region for region in SINGAPORE_REGIONS}


def _fetch_nea_psi_day(day: str) -> list[dict]:
    """Fetch every paginated NEA PSI record for one Singapore calendar day."""
    records = []
    page_token = None
    seen_tokens = set()
    while True:
        params = {"date": day}
        if page_token:
            params["paginationToken"] = page_token
        url = f"{_NEA_AIR_API}?{urlencode(params)}"
        request = Request(url, headers={"User-Agent": "BloodPressureBot/1.0"})
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("code") != 0:
            raise RuntimeError(payload.get("errorMsg") or "NEA air-quality API error")
        data = payload.get("data") or {}
        records.extend(data.get("items") or [])
        next_token = data.get("paginationToken")
        if not next_token:
            return records
        if next_token in seen_tokens:
            raise RuntimeError("NEA API returned a repeated pagination token")
        seen_tokens.add(next_token)
        page_token = next_token


def get_singapore_pm_history() -> list[dict]:
    """Fetch the last 24 hours of NEA's regional PM pollutant sub-indices."""
    singapore_tz = pytz.timezone("Asia/Singapore")
    now = datetime.now(singapore_tz)
    cutoff = now - timedelta(hours=24)
    days = sorted({cutoff.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")})
    day_records = _parallel_fetch(days, _fetch_nea_psi_day, max_workers=2)
    points_by_time = {}

    for record in (item for day in day_records for item in day):
        timestamp_text = record.get("timestamp") or record.get("updatedTimestamp")
        if not timestamp_text:
            continue
        try:
            timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = singapore_tz.localize(timestamp)
            timestamp = timestamp.astimezone(singapore_tz)
        except (TypeError, ValueError):
            logger.info("Skipping NEA record with invalid timestamp %r", timestamp_text)
            continue
        if not cutoff <= timestamp <= now:
            continue

        readings = record.get("readings") or {}
        values = {}
        for pollutant, key in (("PM2.5", "pm25_sub_index"), ("PM10", "pm10_sub_index")):
            regional = readings.get(key) or {}
            cleaned = {
                _NEA_REGION_KEYS[region.lower()]: number
                for region, value in regional.items()
                if region.lower() in _NEA_REGION_KEYS
                and (number := _number(value)) is not None
            }
            values[pollutant] = cleaned
            values[f"{pollutant} Singapore mean"] = _mean(cleaned.values())
        points_by_time[timestamp] = values

    return [
        {"timestamp": timestamp, **values}
        for timestamp, values in sorted(points_by_time.items())
    ]


def _render_singapore_pm_graph(points: list[dict]) -> bytes:
    """Render the five regions and their per-time mean using NEA's own index."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(13, 9), sharex=True, constrained_layout=True)
    colors = {
        "North": "#0077b6",
        "South": "#e76f51",
        "East": "#2a9d8f",
        "West": "#9b5de5",
        "Central": "#f4a261",
        "mean": "#172a3a",
    }
    for ax, pollutant in zip(axes, ("PM2.5", "PM10")):
        for region in SINGAPORE_REGIONS:
            times = [point["timestamp"] for point in points if point[pollutant].get(region) is not None]
            values = [point[pollutant][region] for point in points if point[pollutant].get(region) is not None]
            if times:
                ax.plot(times, values, color=colors[region], linewidth=1.5, alpha=0.85, label=region)
        mean_key = f"{pollutant} Singapore mean"
        mean_points = [point for point in points if point[mean_key] is not None]
        if mean_points:
            ax.plot(
                [point["timestamp"] for point in mean_points],
                [point[mean_key] for point in mean_points],
                color=colors["mean"], linewidth=3, label="Singapore mean (available regions)",
            )
        ax.set_title(pollutant, loc="left", fontsize=14, fontweight="bold")
        ax.set_ylabel("NEA pollutant sub-index")
        ax.set_ylim(bottom=0)
        ax.grid(axis="y", color="#d9e0e5", linewidth=0.7)
        ax.legend(loc="upper left", ncol=3, fontsize=8, frameon=False)

    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%d %b\n%H:%M", tz=pytz.timezone("Asia/Singapore")))
    axes[-1].set_xlabel("Singapore time (SGT)")
    fig.suptitle("Singapore NEA PM indices by region — past 24 hours", fontsize=17, fontweight="bold")
    fig.text(
        0.5, -0.01,
        "NEA readings via data.gov.sg • NEA pollutant sub-indices (not the AQICN/US EPA AQI scale)",
        ha="center", fontsize=9, color="#4b5963",
    )
    output = io.BytesIO()
    try:
        fig.savefig(output, format="png", dpi=180, bbox_inches="tight", facecolor="white")
    finally:
        plt.close(fig)
    return output.getvalue()


async def neagraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send a labeled history chart of NEA PM pollutant sub-indices."""
    try:
        points = await asyncio.to_thread(get_singapore_pm_history)
        if not points:
            raise RuntimeError("NEA returned no PM index data for the past 24 hours")
        graph_bytes = await asyncio.to_thread(_render_singapore_pm_graph, points)
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, OSError) as exc:
        logger.warning("Singapore PM history chart failed: %s", exc)
        await update.effective_message.reply_text(
            "Could not fetch Singapore's PM history right now. Please try again later."
        )
        return
    image_file = io.BytesIO(graph_bytes)
    image_file.name = "singapore-nea-past-24-hours.png"
    await update.effective_message.reply_photo(
        photo=image_file,
        caption="NEA PM indices\n? Period: past 24 hours\n? Regions: North / South / East / West / Central\n? Mean: available regions\n? Source: NEA / data.gov.sg",
    )


def _aqicn_city_page(url: str) -> str:
    """Fetch the AQICN regional page containing its native pollutant graphs."""
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BloodPressureBot/1.0)"})
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


class _AQICNGraphParser(HTMLParser):
    """Read images only inside their own pollutant cell."""
    def __init__(self):
        super().__init__()
        self.pollutant = None
        self.graphs = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "td":
            cell = attrs.get("id", "").lower()
            self.pollutant = cell[3:] if cell in ("td_pm25", "td_pm10") else None
        if tag != "img" or not self.pollutant:
            return
        source = attrs.get("src", "")
        prefix = "data:image/png;base64,"
        if not source.startswith(prefix):
            return
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", source[len(prefix):]), validate=True)
            with Image.open(io.BytesIO(raw)) as image:
                image.verify()
            self.graphs[self.pollutant] = raw
        except (binascii.Error, ValueError, OSError):
            logger.warning("Invalid AQICN %s image", self.pollutant)

    def handle_endtag(self, tag):
        if tag == "td":
            self.pollutant = None


def get_aqicn_pm_graphs() -> list[tuple[str, str, bytes]]:
    """Fetch AQICN's native per-pollutant graphs for each Singapore region."""
    def fetch_region(region: str):
        try:
            page = _aqicn_city_page(AQICN_CITY_PAGES[region])
            parser = _AQICNGraphParser()
            parser.feed(page)
            return region, parser.graphs
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            logger.info("Unable to fetch AQICN graph for %s: %s", region, exc)
            return region, {}

    fetched = _parallel_fetch(list(SINGAPORE_REGIONS), fetch_region, max_workers=5)
    return [
        (region, pollutant, graph_bytes)
        for region, graphs in fetched
        for pollutant in ("pm25", "pm10")
        if (graph_bytes := graphs.get(pollutant))
    ]


def _render_aqicn_graph_panel(graphs) -> bytes:
    """Place native AQICN sparklines on a readable Telegram-sized canvas."""
    lookup = {(region, pollutant): raw for region, pollutant, raw in graphs}
    canvas = Image.new("RGB", (1000, 1040), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text((25, 15), "AQICN Singapore - native AQI trends - past 2 days", fill="black", font_size=24)
    draw.text((25, 50), "Source: aqicn.org | Left to right: older to newer", fill="black", font_size=18)
    for row, region in enumerate(SINGAPORE_REGIONS):
        y = 95 + row * 185
        draw.text((25, y), region, fill="black", font_size=23)
        for col, pollutant in enumerate(("pm25", "pm10")):
            x = 25 + col * 490
            label = "PM2.5 AQI" if pollutant == "pm25" else "PM10 AQI"
            draw.text((x, y + 32), label, fill="black", font_size=19)
            raw = lookup.get((region, pollutant))
            if not raw:
                draw.text((x, y + 75), "Unavailable", fill="gray", font_size=18)
                continue
            with Image.open(io.BytesIO(raw)) as source:
                source = source.convert("RGBA")
                source.thumbnail((450, 100))
                scale = min(450 / source.width, 100 / source.height)
                resized = source.resize((round(source.width * scale), round(source.height * scale)))
                canvas.paste(resized, (x, y + 65), resized)
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


async def psigraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        graphs = await asyncio.to_thread(get_aqicn_pm_graphs)
        if not graphs:
            raise RuntimeError("No AQICN graphs")
        graph_bytes = await asyncio.to_thread(_render_aqicn_graph_panel, graphs)
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, OSError) as exc:
        logger.warning("AQICN graph request failed: %s", exc)
        await update.effective_message.reply_text("? AQICN graphs: unavailable\n? Retry: /psigraph")
        return
    image_file = io.BytesIO(graph_bytes)
    image_file.name = "singapore-aqicn-trends.png"
    await update.effective_message.reply_photo(
        photo=image_file,
        caption=f"AQICN PM trends\n? Scale: AQI\n? Period: past 2 days\n? Graphs: {len(graphs)}/10\n? Source: aqicn.org",
    )
