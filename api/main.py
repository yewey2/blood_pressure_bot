#!/usr/bin/env python
# This program is dedicated to the public domain under the CC0 license.
# pylint: disable=import-error,unused-argument
"""
Simple example of a bot that uses a custom webhook setup and handles custom updates.
For the custom webhook setup, the libraries `flask`, `asgiref` and `uvicorn` are used. Please
install them as `pip install flask[async]~=2.3.2 uvicorn~=0.23.2 asgiref~=3.7.2`.
Note that any other `asyncio` based web server framework can be used for a custom webhook setup
just as well.

Usage:
Set bot Token, URL, admin CHAT_ID and PORT after the imports.
You may also need to change the `listen` value in the uvicorn configuration to match your setup.
Press Ctrl-C on the command line or send a signal to the process to stop the bot.
"""

import asyncio
import base64
import binascii
import html
import logging
import re
from dataclasses import dataclass
from http import HTTPStatus

from flask import Flask, Response, abort, make_response, request

from telegram import InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackContext,
    CommandHandler,
    ContextTypes,
    ExtBot,
    TypeHandler,
)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


import os
import asyncio


## ================
## My stuff
import os
import logging
import io
import json
from PIL import Image
import json
import json_repair
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Use python-dotenv to load environment variables from a .env file for local development
# In production (like on Render), you will set these directly.
from dotenv import load_dotenv
load_dotenv()

import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime
import pytz

import google.generativeai as genai
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters

## ================

# Load your secret keys from environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")
AQICN_API_KEY = os.getenv("AQICN_API_KEY")

# Define configuration constants
ADMIN_CHAT_ID = TELEGRAM_USER_ID
TOKEN = TELEGRAM_BOT_TOKEN  # nosec B105

# genai.configure(api_key=GEMINI_API_KEY)
genai.configure(api_key=GEMINI_API_KEY, transport="rest")

@dataclass
class WebhookUpdate:
    """Simple dataclass to wrap a custom update type"""

    user_id: int
    payload: str


class CustomContext(CallbackContext[ExtBot, dict, dict, dict]):
    """
    Custom CallbackContext class that makes `user_data` available for updates of type
    `WebhookUpdate`.
    """

    @classmethod
    def from_update(
        cls,
        update: object,
        application: "Application",
    ) -> "CustomContext":
        if isinstance(update, WebhookUpdate):
            return cls(application=application, user_id=update.user_id)
        return super().from_update(update, application)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command."""
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Hello! I'm your Blood Pressure reading assistant. Send me a clear picture of your BP monitor's screen."
    )


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


def get_singapore_psi() -> tuple[dict, list[dict]]:
    """Fetch Singapore-wide and the five NEA regional AQICN feeds only.

    AQICN's map bounds endpoint can include Johor stations. The named feeds
    below intentionally avoid map discovery: they are the five Singapore
    regions the bot reports and averages.
    """
    def fetch_feed(feed_name: str):
        try:
            return _aqicn_request(f"feed/{feed_name}/")
        except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError) as exc:
            logger.info("Unable to fetch AQICN feed %s: %s", feed_name, exc)
            return None

    feed_names = ["Singapore", *[f"Singapore/{region}" for region in SINGAPORE_REGIONS]]
    feeds = _parallel_fetch(feed_names, fetch_feed)
    general = feeds[0]
    if not general:
        raise RuntimeError("The Singapore-wide AQICN feed is unavailable.")

    regions = []
    for region, feed in zip(SINGAPORE_REGIONS, feeds[1:]):
        regions.append(_compact_aqicn_feed(region, feed))
    return general, regions


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


def _aqi_label(value) -> str:
    number = _number(value)
    if number is None:
        return "Unavailable"
    if number <= 50:
        return "Good"
    if number <= 100:
        return "Moderate"
    if number <= 150:
        return "Unhealthy for sensitive groups"
    if number <= 200:
        return "Unhealthy"
    if number <= 300:
        return "Very unhealthy"
    return "Hazardous"


def _format_pm_values(feed: dict) -> str:
    """PM values are AQI sub-indices, never misleading mass concentrations."""
    return (
        f"PM2.5: {_display_number(_pollutant_aqi(feed, 'pm25'))}\n"
        f"PM10: {_display_number(_pollutant_aqi(feed, 'pm10'))}\n"
    )


def _format_difference(general_value, regional_mean) -> str:
    general_number = _number(general_value)
    average_number = _number(regional_mean)
    if general_number is None or average_number is None:
        return "N/A"
    difference = general_number - average_number
    sign = "+" if difference > 0 else ""
    return f"{sign}{_display_number(difference)}"


async def psi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report PM2.5 and PM10 AQI for the five Singapore regions."""
    if not AQICN_API_KEY:
        await update.effective_message.reply_text("AQICN_API_KEY is not configured.")
        return

    try:
        general, stations = await asyncio.to_thread(get_singapore_psi)
    except (HTTPError, URLError, TimeoutError, RuntimeError, ValueError) as exc:
        logger.warning("AQICN request failed: %s", exc)
        await update.effective_message.reply_text(
            "Could not fetch Singapore PM readings from AQICN right now. Please try again later."
        )
        return

    regional_aqi_mean = _mean(station.get("aqi") for station in stations)
    regional_pm25_mean = _mean(_pollutant_aqi(station, "pm25") for station in stations)
    regional_pm10_mean = _mean(_pollutant_aqi(station, "pm10") for station in stations)
    available_regions = sum(_number(station.get("aqi")) is not None for station in stations)

    lines = [
        "<b>Singapore particulate air quality</b>",
        # "AQICN 1-hour AQI view • PM2.5 and PM10 only",
        "",
        f"<b>Five-region mean</b> ({available_regions}/5 reporting)",
        f"AQI {_display_number(regional_aqi_mean)} --- ({_aqi_label(regional_aqi_mean)})",
        f"PM2.5: {_display_number(regional_pm25_mean)}",
        f"PM10: {_display_number(regional_pm10_mean)}",
        "",
        "<b>Singapore-wide AQICN feed</b>",
        f"AQI {_display_number(general.get('aqi'))} --- ({_aqi_label(general.get('aqi'))})",
        _format_pm_values(general),
        f"Difference from five-region mean: {_format_difference(general.get('aqi'), regional_aqi_mean)} AQI",
    ]

    lines.extend(["", "<b>Regional AQI readings</b>"])
    for station in stations:
        lines.append(
            f"<b>{html.escape(station['region'])}</b>: \n"
            f"AQI {_display_number(station.get('aqi'))} ({_aqi_label(station.get('aqi'))})"
        )
        lines.append(_format_pm_values(station))

    if general.get("time", {}).get("s"):
        lines.append(f"Updated: {html.escape(str(general['time']['s']))}")
    # lines.extend([
    #     "",
    #     "PM2.5 and PM10 are pollutant-specific AQI values (not µg/m³). "
    #     "AQI 100 is the top of the Moderate band; 101 begins Unhealthy for Sensitive Groups.",
    #     "Source: Singapore NEA data as presented by World Air Quality Index (AQICN). /psigraph sends the regional trends.",
    # ])
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


def _aqicn_city_page(url: str) -> str:
    """Read a public AQICN city page, which contains its native PM graphs."""
    request_headers = {"User-Agent": "Mozilla/5.0 (compatible; BloodPressureBot/1.0)"}
    with urlopen(Request(url, headers=request_headers), timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


_AQICN_GRAPH_PATTERN = re.compile(
    r"<td\s+id=['\"]td_(pm25|pm10)['\"][^>]*>.*?"
    r"<img[^>]+src=['\"]data:image/png;base64,([A-Za-z0-9+/=\s]+)['\"]",
    re.IGNORECASE | re.DOTALL,
)


def _extract_pm_graphs(page: str) -> dict[str, bytes]:
    """Extract AQICN's embedded PM2.5/PM10 sparklines from a city page."""
    graphs = {}
    for match in _AQICN_GRAPH_PATTERN.finditer(page):
        pollutant = match.group(1).lower()
        try:
            graphs[pollutant] = base64.b64decode(
                re.sub(r"\s+", "", match.group(2)), validate=True
            )
        except (binascii.Error, ValueError):
            logger.warning("AQICN returned an invalid %s graph image.", pollutant)
    return graphs


def get_singapore_pm_graphs() -> list[tuple[str, str, bytes]]:
    """Fetch AQICN's original PM2.5/PM10 graph images for each region."""
    def fetch_region(region: str):
        try:
            return region, _extract_pm_graphs(_aqicn_city_page(AQICN_CITY_PAGES[region]))
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
            logger.info("Unable to fetch AQICN graph for %s: %s", region, exc)
            return region, {}

    graph_sets = _parallel_fetch(list(SINGAPORE_REGIONS), fetch_region, max_workers=5)
    graphs = []
    for region, graph_bytes in graph_sets:
        for pollutant in ("pm25", "pm10"):
            image_bytes = graph_bytes.get(pollutant)
            if image_bytes:
                graphs.append((region, pollutant, image_bytes))
    return graphs


async def psigraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send AQICN's original PM trend images for all five regions."""
    try:
        graphs = await asyncio.to_thread(get_singapore_pm_graphs)
    except (HTTPError, URLError, TimeoutError, ValueError, OSError) as exc:
        logger.warning("AQICN graph request failed: %s", exc)
        await update.effective_message.reply_text(
            "Could not fetch AQICN's PM trend graphs right now. Please try again later."
        )
        return

    if not graphs:
        await update.effective_message.reply_text(
            "AQICN did not provide usable PM2.5/PM10 graph images right now. Please try again later."
        )
        return

    caption = (
        "<b>Singapore PM trend graphs</b>\n"
        "PM2.5 and PM10 AQI only. Images are shown in AQICN's original format. "
        "Source: Singapore NEA via World Air Quality Index (AQICN)."
    )
    media = []
    for index, (region, pollutant, image_bytes) in enumerate(graphs):
        image_file = io.BytesIO(image_bytes)
        image_file.name = f"singapore-{region.lower()}-{pollutant}.png"
        image_caption = f"{region} — {'PM2.5' if pollutant == 'pm25' else 'PM10'} AQI"
        media.append(
            InputMediaPhoto(
                media=image_file,
                caption=f"{caption}\n{image_caption}" if index == 0 else image_caption,
                parse_mode=ParseMode.HTML if index == 0 else None,
            )
        )
    if len(media) == 1:
        only_photo = media[0]
        await update.effective_message.reply_photo(
            photo=only_photo.media,
            caption=only_photo.caption,
            parse_mode=only_photo.parse_mode,
        )
        return
    for start in range(0, len(media), 10):
        await update.effective_message.reply_media_group(media=media[start : start + 10])


try:
    # Make sure 'firebase-credentials.json' is in the same folder as your bot script
    if os.path.exists('firebase-credentials.json'):
        cred = credentials.Certificate("firebase-credentials.json")
    else:
        firebase_creds_json_str = os.getenv("FIREBASE_CREDENTIALS_JSON")    
        if not firebase_creds_json_str:
            raise ValueError("FIREBASE_CREDENTIALS_JSON environment variable not set.")
        firebase_creds_dict = json_repair.loads(firebase_creds_json_str)
        cred = credentials.Certificate(firebase_creds_dict)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("✅ Firebase initialized successfully.")
except Exception as e:
    logger.error(f"🔥 Error initializing Firebase: {e}. Make sure 'firebase-credentials.json' is present.")
    db = None # Set db to None if initialization fails
    raise Exception(f"🔥 Error initializing Firebase: {e}. Make sure 'firebase-credentials.json' is present.")

# --- Gemini AI Function ---
async def get_bp_from_image(image_bytes: bytes) -> dict:
    """Sends an image to Gemini and returns the extracted BP data as a dictionary."""
    
    # The prompt is the key to getting reliable results!
    prompt = """\
Provide your reply in a JSON format. 

The main JSON should have 2 keys: `values` and `status`.
It should include the systolic blood pressure as `SBP`, diastolic blood pressure as `DBP`, and heart rate as `HR`, as the 3 keys in the values.
If no values are found, `status` should be `failed`, and values should be null.
If values are found, `status` should be `success`.

If any of the values are not visible in the image, set them to null.

ONLY provide the full JSON, nothing else, starting with ```json
    """
    
    # Use Pillow to open the image from bytes
    img = Image.open(io.BytesIO(image_bytes))
    
    # Use the fast and capable Gemini 1.5 Flash model
    model = genai.GenerativeModel('gemini-2.5-flash-lite')
    
    logger.info("Sending image to Gemini API...")
    try:
        # response = await model.generate_content_async([prompt, img])
        response = await asyncio.to_thread(model.generate_content, [prompt, img])
        
        # Clean up the response to get pure JSON
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "")
        logger.info(f"Received raw response: {cleaned_text}")
        
        # Parse the JSON string into a Python dictionary
        data = json_repair.loads(cleaned_text) ## Use json repair in case anything wrong with the json.
        return data

    except Exception as e:
        logger.error(f"Error processing image with Gemini: {e}")
        return {"error": "Could not process the image or parse the response."}

# --- NEW FIREBASE FUNCTION ---
def save_reading_to_firestore(sbp: int, dbp: int, hr: int):
    """Saves a new blood pressure reading to the Firestore database."""
    if not db:
        logger.error("Firestore client not available. Skipping save.")
        return False
    
    try:
        # Create a new document in the 'readings' collection
        doc_ref = db.collection('readings').document()
        doc_ref.set({
            'timestamp': datetime.now(pytz.timezone('Asia/Singapore')), # Use current server time
            'sbp': int(sbp),
            'dbp': int(dbp),
            'hr': int(hr)
        })
        logger.info(f"Successfully saved reading to Firestore.")
        return True
    except Exception as e:
        logger.error(f"Error saving to Firestore: {e}")
        return False


async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for when the user sends a photo."""
    chat_id = update.effective_chat.id
    if int(chat_id) != int(TELEGRAM_USER_ID):  # Replace with your Telegram user ID for security
        await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized to use this bot.")
        return
    
    # Check if the message contains a photo
    if not update.message.photo:
        await context.bot.send_message(chat_id=chat_id, text="Please send an image file.")
        return

    await context.bot.send_message(chat_id=chat_id, text="Processing your image, please wait... 🤖")

    # Get the photo file sent by the user (we take the highest resolution one)
    photo_file = await update.message.photo[-1].get_file()
    
    # Download the photo into memory as bytes
    photo_bytes = await photo_file.download_as_bytearray()

    # Call our Gemini function to process the image
    bp_data = await get_bp_from_image(bytes(photo_bytes))
    
    try:
        if bp_data and bp_data.get('status') == "success":
            values = bp_data.get('values', {})
            sbp = values.get('SBP')
            dbp = values.get('DBP')
            hr = values.get('HR')
            # Ensure all values are present before trying to save
            if sbp is not None and dbp is not None and hr is not None:
                # --- SAVE TO FIREBASE ---
                save_successful = save_reading_to_firestore(
                    sbp=sbp,
                    dbp=dbp,
                    hr=hr
                )
                reply_text = (
                    f"✅ **Blood Pressure Reading Extracted**\n\n"
                    f"🩺 **Systolic (SBP):** {sbp}\n"
                    f"❤️ **Diastolic (DBP):** {dbp}\n"
                    f"💓 **Heart Rate (HR):** {hr}\n\n"
                )
                if save_successful:
                    reply_text += "💾 *Data saved to database.*"
                else:
                    reply_text += "⚠️ *Could not save data to database.*"
            else:
                sbp_text = sbp if sbp is not None else 'N/A'
                dbp_text = dbp if dbp is not None else 'N/A'
                hr_text = hr if hr is not None else 'N/A'
                reply_text = (
                    f"🟡 **Partial Reading Extracted**\n\n"
                    f"🩺 **Systolic (SBP):** {sbp_text}\n"
                    f"❤️ **Diastolic (DBP):** {dbp_text}\n"
                    f"💓 **Heart Rate (HR):** {hr_text}\n\n"
                    f"💾 *Not saved to database because some values are missing.*"
                )
        elif bp_data and bp_data.get('status' == "failed"):
            reply_text = f"Sorry, I couldn't read the values. Please try a clearer picture."
        else:
            reply_text = f"Sorry, something went wrong. Data is {bp_data}"
    except Exception as e:
        reply_text = f"Error encountered:\n\n{traceback.format_exc()}"
    finally:
        await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode="Markdown")
    

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors caused by updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)


async def webhook_update(update: WebhookUpdate, context: CustomContext) -> None:
    """Handle custom updates."""
    chat_member = await context.bot.get_chat_member(chat_id=update.user_id, user_id=update.user_id)
    payloads = context.user_data.setdefault("payloads", [])
    payloads.append(update.payload)
    combined_payloads = "</code>\n• <code>".join(payloads)
    text = (
        f"The user {chat_member.user.mention_html()} has sent a new payload. "
        f"So far they have sent the following payloads: \n\n• <code>{combined_payloads}</code>"
    )
    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode=ParseMode.HTML)


# --- Build the PTB application at module level so it exists on every cold start ---
# NB: do NOT name this `app` or `application` — Vercel treats those as the WSGI/ASGI
# entrypoint and would try to call this PTB object as a web app.
context_types = ContextTypes(context=CustomContext)
# updater=None: Telegram delivers updates via webhook, so we don't need an Updater.
ptb_app = (
    Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
)

# register handlers
ptb_app.add_handler(CommandHandler("start", start))
ptb_app.add_handler(CommandHandler("psi", psi))
ptb_app.add_handler(CommandHandler("psigraph", psigraph))
ptb_app.add_handler(MessageHandler(filters.PHOTO, image_handler))
ptb_app.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))
ptb_app.add_error_handler(error_handler)

# --- A single event loop reused across warm serverless invocations ---
# asyncio.run() closes its loop after each call. Since ptb_app is built once at
# import and its internal asyncio primitives bind to the first loop they run on,
# a later request in the same warm container would hit "Event loop is closed".
# Keeping one loop alive (never closed) avoids that.
_loop = asyncio.new_event_loop()
asyncio.set_event_loop(_loop)

# --- Flask app exposed at module level as `app` for the Vercel Python runtime ---
flask_app = Flask(__name__)


@flask_app.post("/telegram")
def telegram() -> Response:
    """Process one incoming Telegram update synchronously, then return.

    On serverless there is no long-running worker, so we can't enqueue and walk
    away. We initialize the application, handle the update in-request, and shut down.
    """

    async def _process() -> None:
        async with ptb_app:
            update = Update.de_json(data=request.get_json(force=True), bot=ptb_app.bot)
            await ptb_app.process_update(update)

    _loop.run_until_complete(_process())
    return Response(status=HTTPStatus.OK)


@flask_app.route("/submitpayload", methods=["GET", "POST"])
def custom_updates() -> Response:
    """Handle a custom webhook update synchronously."""
    try:
        user_id = int(request.args["user_id"])
        payload = request.args["payload"]
    except KeyError:
        abort(
            HTTPStatus.BAD_REQUEST,
            "Please pass both `user_id` and `payload` as query parameters.",
        )
    except ValueError:
        abort(HTTPStatus.BAD_REQUEST, "The `user_id` must be a string!")

    async def _process() -> None:
        async with ptb_app:
            await ptb_app.process_update(WebhookUpdate(user_id=user_id, payload=payload))

    _loop.run_until_complete(_process())
    return Response(status=HTTPStatus.OK)


@flask_app.route("/setwebhook", methods=["GET", "POST"])
def set_webhook() -> Response:
    """One-off endpoint: register this deployment's URL with Telegram.

    Hit this once after deploying (e.g. open it in the browser). The webhook URL
    is derived from the host this request arrived on, so it works on any deployment.
    """
    webhook_url = f"https://{request.host}/telegram"

    async def _set() -> None:
        async with ptb_app:
            await ptb_app.bot.set_webhook(
                url=webhook_url, allowed_updates=Update.ALL_TYPES
            )

    _loop.run_until_complete(_set())
    response = make_response(f"Webhook set to {webhook_url}", HTTPStatus.OK)
    response.mimetype = "text/plain"
    return response


@flask_app.get("/healthcheck")
def health() -> Response:
    """For the health endpoint, reply with a simple plain text message."""
    response = make_response("The bot is still running fine :)", HTTPStatus.OK)
    response.mimetype = "text/plain"
    return response


# Vercel's Python runtime imports this module and looks for a top-level `app`.
app = flask_app
