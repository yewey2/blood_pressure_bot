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
            source_name = str((feed.get("city") or {}).get("name") or "")
            if region.casefold() not in source_name.casefold():
                raise RuntimeError(
                    f"AQICN returned {source_name or 'an unnamed feed'} for {region}"
                )
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



def _epa_pm25_aqi(concentration):
    """Convert NEA PM2.5 concentration (ug/m3) to the current US EPA AQI."""
    value = _number(concentration)
    if value is None or value < 0:
        return None
    value = int(value * 10) / 10  # EPA truncates PM2.5 concentration to 0.1 ug/m3.
    breakpoints = (
        (0.0, 9.0, 0, 50),
        (9.1, 35.4, 51, 100),
        (35.5, 55.4, 101, 150),
        (55.5, 125.4, 151, 200),
        (125.5, 225.4, 201, 300),
        (225.5, 325.4, 301, 500),
    )
    for c_low, c_high, i_low, i_high in breakpoints:
        if c_low <= value <= c_high:
            return round((i_high - i_low) / (c_high - c_low) * (value - c_low) + i_low)
    if value > 325.4:
        c_low, c_high, i_low, i_high = breakpoints[-1]
        return round((i_high - i_low) / (c_high - c_low) * (value - c_low) + i_low)
    return None

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



def get_latest_nea_pm25_hourly() -> dict:
    """Fetch NEA's separate one-hour PM2.5 concentration readings."""
    request = Request(_NEA_PM25_API, headers={"User-Agent": "BloodPressureBot/1.0"})
    with urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("code") != 0:
        raise RuntimeError("NEA one-hour PM2.5 readings unavailable")
    items = (payload.get("data") or {}).get("items") or []
    if not items:
        raise RuntimeError("NEA one-hour PM2.5 readings unavailable")
    return max(items, key=lambda item: item.get("timestamp", ""))

def _format_comparison(stations, nea, nea_pm25_hourly=None):
    readings = nea.get("readings") or {}
    lines = ["<b>Singapore air quality</b>",
             "- AQICN: current AQI (Instant Cast)",
             "- NEA PSI and sub-indices: rolling 24-hour",
             "- NEA PM2.5 concentration: 1-hour and 24-hour",
             "- PSI: highest pollutant sub-index",
             "- AQICN Singapore data source: NEA",
             "",
             "<b>Regional means</b> (equal-weight mean; not an official Singapore-wide index)"]
    aq_pm25 = [_number(_pollutant_aqi(station, "pm25")) for station in stations]
    nea_pm25_original = [_number((readings.get("pm25_sub_index") or {}).get(region.lower())) for region in SINGAPORE_REGIONS]
    nea_pm25_conc = [_number((readings.get("pm25_twenty_four_hourly") or {}).get(region.lower())) for region in SINGAPORE_REGIONS]
    nea_pm25_scaled = [_epa_pm25_aqi(value) for value in nea_pm25_conc]
    lines.extend([
        f"- PM2.5 AQICN current AQI: {_display_number(_mean(aq_pm25))} ({sum(v is not None for v in aq_pm25)}/5)",
        f"- PM2.5 NEA sub-index (24h): {_display_number(_mean(nea_pm25_original))} ({sum(v is not None for v in nea_pm25_original)}/5)",
        f"- PM2.5 NEA EPA AQI estimate (24h): {_display_number(_mean(nea_pm25_scaled))} ({sum(v is not None for v in nea_pm25_scaled)}/5)",
    ])
    aq_pm10 = [_number(_pollutant_aqi(station, "pm10")) for station in stations]
    nea_pm10 = [_number((readings.get("pm10_sub_index") or {}).get(region.lower())) for region in SINGAPORE_REGIONS]
    lines.extend([
        f"- PM10 AQICN-reported AQI: {_display_number(_mean(aq_pm10))} ({sum(v is not None for v in aq_pm10)}/5)",
        f"- PM10 NEA sub-index (24h): {_display_number(_mean(nea_pm10))} ({sum(v is not None for v in nea_pm10)}/5)",
    ])
    pm10_matches = sum(
        aq is not None and nea_value is not None and aq == nea_value
        for aq, nea_value in zip(aq_pm10, nea_pm10)
    )
    if pm10_matches:
        lines.append(
            f"- PM10 AQICN and NEA values match: {pm10_matches}/5 regions; shared NEA source, not independent confirmation"
        )
    psi_values = [
        _number((readings.get("psi_twenty_four_hourly") or {}).get(region.lower()))
        for region in SINGAPORE_REGIONS
    ]
    pm25_matches_psi = sum(
        psi is not None and subindex is not None and psi == subindex
        for psi, subindex in zip(psi_values, nea_pm25_original)
    )
    if pm25_matches_psi:
        lines.append(
            f"- PSI equals the PM2.5 sub-index: {pm25_matches_psi}/5 regions; PM2.5 is the PSI driver there"
        )
    aqicn_overall = [_number(station.get("aqi")) for station in stations]
    aqicn_pm25_matches = sum(
        overall is not None and pm25 is not None and overall == pm25
        for overall, pm25 in zip(aqicn_overall, aq_pm25)
    )
    if aqicn_pm25_matches:
        lines.append(
            f"- AQICN overall equals PM2.5 AQI: {aqicn_pm25_matches}/5 regions; PM2.5 drives the reported AQI there"
        )
    hourly_readings = ((nea_pm25_hourly or {}).get("readings") or {}).get("pm25_one_hourly") or {}
    nea_pm25_hourly_values = [
        _number(hourly_readings.get(region.lower())) for region in SINGAPORE_REGIONS
    ]
    lines.append(
        f"- PM2.5 NEA concentration (1h): {_display_number(_mean(nea_pm25_hourly_values))} ug/m3 ({sum(v is not None for v in nea_pm25_hourly_values)}/5)"
    )
    for station in stations:
        region = station["region"]
        key = region.lower()
        pm25_concentration = (readings.get("pm25_twenty_four_hourly") or {}).get(key)
        pm25_scaled = _epa_pm25_aqi(pm25_concentration)
        lines.extend(["", f"<b>{html.escape(region)}</b>",
            f"- AQICN current overall AQI: {_display_number(station.get('aqi'))}",
            f"- NEA PSI (24h): {_display_number((readings.get('psi_twenty_four_hourly') or {}).get(key))}",
            f"- PM2.5 AQICN current AQI: {_display_number(_pollutant_aqi(station, 'pm25'))}",
            f"- PM2.5 NEA 1h concentration: {_display_number(hourly_readings.get(key))} ug/m3",
            f"- PM2.5 NEA sub-index (24h): {_display_number((readings.get('pm25_sub_index') or {}).get(key))}",
            f"- PM2.5 NEA concentration (24h): {_display_number(pm25_concentration)} ug/m3",
            f"- PM2.5 NEA EPA AQI estimate (24h): {_display_number(pm25_scaled)}",
            f"- PM10 AQICN-reported AQI: {_display_number(_pollutant_aqi(station, 'pm10'))}",
            f"- PM10 NEA sub-index (24h): {_display_number((readings.get('pm10_sub_index') or {}).get(key))}",
            f"- AQICN updated: {html.escape(str(station.get('time', {}).get('s') or 'N/A'))}"])
    lines.extend(["", f"- NEA PSI updated: {html.escape(str(nea.get('timestamp') or 'N/A'))}",
                  f"- NEA PM2.5 1h updated: {html.escape(str((nea_pm25_hourly or {}).get('timestamp') or 'N/A'))}",
                  "- EPA AQI estimate uses US EPA 2024 PM2.5 breakpoints.",
                  "- Sources: AQICN / NEA via data.gov.sg"])
    return "\n".join(lines)


async def psi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    results = await asyncio.gather(asyncio.to_thread(get_singapore_psi),
                                   asyncio.to_thread(get_latest_nea_psi),
                                   asyncio.to_thread(get_latest_nea_pm25_hourly), return_exceptions=True)
    stations, nea, nea_pm25_hourly = results
    if isinstance(stations, Exception):
        logger.warning("AQICN readings unavailable: %s", stations)
        stations = [_compact_aqicn_feed(r, None) for r in SINGAPORE_REGIONS]
    if isinstance(nea, Exception):
        logger.warning("NEA readings unavailable: %s", nea)
        nea = {}
    if isinstance(nea_pm25_hourly, Exception):
        logger.warning("NEA one-hour PM2.5 request failed: %s", nea_pm25_hourly)
        nea_pm25_hourly = {}
    await update.effective_message.reply_text(
        _format_comparison(stations, nea, nea_pm25_hourly), parse_mode=ParseMode.HTML
    )


_NEA_AIR_API = "https://api-open.data.gov.sg/v2/real-time/api/psi"
_NEA_PM25_API = "https://api-open.data.gov.sg/v2/real-time/api/pm25"
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
        caption="NEA PM indices\nPeriod: past 24 hours\nRegions: North / South / East / West / Central\nMean: available regions\nSource: NEA / data.gov.sg",
    )


def _aqicn_city_page(url: str) -> str:
    """Fetch a regional AQICN page with the published PM AQI range values."""
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; BloodPressureBot/1.0)"})
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


class _AQICNStatsParser(HTMLParser):
    """Read AQICN's published current, minimum, and maximum AQI values."""
    def __init__(self):
        super().__init__()
        self.cell_id = None
        self.parts = []
        self.values = {}

    def handle_starttag(self, tag, attrs):
        if tag == "td":
            self.cell_id = dict(attrs).get("id", "").lower()
            self.parts = []

    def handle_data(self, data):
        if self.cell_id:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "td" and self.cell_id:
            match = re.search(r"\d+(?:\.\d+)?", " ".join(self.parts))
            if match:
                self.values[self.cell_id] = float(match.group())
            self.cell_id = None
            self.parts = []


def get_aqicn_pm_graphs() -> list[dict]:
    """Fetch AQICN's current and past-two-day range for each pollutant/region."""
    def fetch_region(region: str):
        try:
            parser = _AQICNStatsParser()
            parser.feed(_aqicn_city_page(AQICN_CITY_PAGES[region]))
            result = {"region": region}
            for pollutant in ("pm25", "pm10"):
                values = [parser.values.get(f"{kind}_{pollutant}") for kind in ("min", "max", "cur")]
                minimum, maximum, current = values
                if minimum is not None and maximum is not None and current is not None:
                    result[pollutant] = {"min": minimum, "max": maximum, "current": current}
            return result
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            logger.info("Unable to fetch AQICN values for %s: %s", region, exc)
            return {"region": region}

    return _parallel_fetch(list(SINGAPORE_REGIONS), fetch_region, max_workers=5)


def _render_aqicn_graph_panel(regions) -> bytes:
    """Plot AQICN's published 48-hour min/max ranges and current values."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    fig, axes = plt.subplots(1, 2, figsize=(16, 8), constrained_layout=True)
    colors = {"North": "#0077b6", "South": "#e76f51", "East": "#2a9d8f",
              "West": "#9b5de5", "Central": "#f4a261"}
    for ax, pollutant, title in zip(axes, ("pm25", "pm10"), ("PM2.5 AQI", "PM10 AQI")):
        valid = [(i, region, region.get(pollutant)) for i, region in enumerate(regions)
                 if region.get(pollutant)]
        if not valid:
            ax.text(0.5, 0.5, "AQICN values unavailable", ha="center", va="center",
                    transform=ax.transAxes, fontsize=14)
            ax.set_title(title)
            continue
        lows = [entry[2]["min"] for entry in valid]
        highs = [entry[2]["max"] for entry in valid]
        axis_min = max(0, min(lows) - max(5, (max(highs) - min(lows)) * 0.12))
        axis_max = min(500, max(highs) + max(5, (max(highs) - min(lows)) * 0.12))
        for y, region, stats in valid:
            color = colors[region["region"]]
            ax.hlines(y, stats["min"], stats["max"], color=color, linewidth=8, alpha=0.72)
            ax.scatter(stats["current"], y, color=color, edgecolor="black", s=100,
                       zorder=3, label="Current" if y == valid[0][0] else None)
            ax.annotate(f"{stats['min']:g}", (stats["min"], y), xytext=(0, -18),
                        textcoords="offset points", ha="center", fontsize=10)
            ax.annotate(f"{stats['max']:g}", (stats["max"], y), xytext=(0, -18),
                        textcoords="offset points", ha="center", fontsize=10)
            ax.annotate(f"Now {stats['current']:g}", (stats["current"], y), xytext=(0, 13),
                        textcoords="offset points", ha="center", fontsize=10, fontweight="bold")
        ax.set_yticks(range(len(SINGAPORE_REGIONS)), SINGAPORE_REGIONS)
        ax.set_ylim(-0.65, len(SINGAPORE_REGIONS) - 0.35)
        ax.invert_yaxis()
        ax.set_xlim(axis_min, max(axis_min + 1, axis_max))
        ax.set_xlabel("AQI")
        ax.set_title(title, loc="left", fontsize=17, fontweight="bold")
        ax.xaxis.set_major_locator(MaxNLocator(nbins=8, integer=True))
        ax.grid(axis="x", color="#d9e0e5", linewidth=0.8)
        ax.set_axisbelow(True)
    fig.suptitle("AQICN Singapore | current and 48-hour AQI range", fontsize=20, fontweight="bold")
    fig.text(0.5, -0.015, "Bars: minimum to maximum | Dot: current | Source: AQICN", ha="center", fontsize=11)
    output = io.BytesIO()
    fig.savefig(output, format="png", dpi=180, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return output.getvalue()


async def psigraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        regions = await asyncio.to_thread(get_aqicn_pm_graphs)
        if not any(region.get("pm25") or region.get("pm10") for region in regions):
            raise RuntimeError("No AQICN values")
        graph_bytes = await asyncio.to_thread(_render_aqicn_graph_panel, regions)
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError, OSError) as exc:
        logger.warning("AQICN graph request failed: %s", exc)
        await update.effective_message.reply_text("AQICN values: unavailable\nRetry: /psigraph")
        return
    image_file = io.BytesIO(graph_bytes)
    image_file.name = "singapore-aqicn-ranges.png"
    await update.effective_message.reply_photo(
        photo=image_file,
        caption="AQICN PM2.5 and PM10\nRange: past 48 hours\nBars: minimum to maximum\nDot: current value\nSource: AQICN",
    )
