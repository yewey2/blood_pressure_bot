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
import html
import logging
from dataclasses import dataclass
from http import HTTPStatus

import uvicorn
from asgiref.wsgi import WsgiToAsgi
from flask import Flask, Response, abort, make_response, request

from telegram import Update
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

# Define configuration constants
URL =  os.getenv("VERCEL_URL")
URL = f"https://{URL}" if URL else "http://localhost:8000"
ADMIN_CHAT_ID = TELEGRAM_USER_ID
PORT = 8000
TOKEN = TELEGRAM_BOT_TOKEN  # nosec B105


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


# async def start(update: Update, context: CustomContext) -> None:
#     """Display a message with instructions on how to use this bot."""
#     payload_url = html.escape(f"{URL}/submitpayload?user_id=<your user id>&payload=<payload>")
#     text = (
#         f"To check if the bot is still running, call <code>{URL}/healthcheck</code>.\n\n"
#         f"To post a custom update, call <code>{payload_url}</code>."
#     )
#     await update.message.reply_html(text=text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command."""
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Hello! I'm your Blood Pressure reading assistant. Send me a clear picture of your BP monitor's screen."
    )

# async def image_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Handler for when the user sends a photo."""
#     chat_id = update.effective_chat.id
#     if int(chat_id) != int(TELEGRAM_USER_ID):  # Replace with your Telegram user ID for security
#         await context.bot.send_message(chat_id=chat_id, text="Sorry, you are not authorized to use this bot.")
#         return
    
#     # Check if the message contains a photo
#     if not update.message.photo:
#         await context.bot.send_message(chat_id=chat_id, text="Please send an image file.")
#         return

#     await context.bot.send_message(chat_id=chat_id, text="Processing your image, please wait... 🤖")

#     # Get the photo file sent by the user (we take the highest resolution one)
#     photo_file = await update.message.photo[-1].get_file()
    
#     # Download the photo into memory as bytes
#     photo_bytes = await photo_file.download_as_bytearray()

#     # Call our Gemini function to process the image
#     bp_data = await get_bp_from_image(bytes(photo_bytes))
    
#     try:
#         if bp_data and bp_data.get('status') == "success":
#             values = bp_data.get('values', {})
#             sbp = values.get('SBP')
#             dbp = values.get('DBP')
#             hr = values.get('HR')
#             # Ensure all values are present before trying to save
#             if sbp is not None and dbp is not None and hr is not None:
#                 # --- SAVE TO FIREBASE ---
#                 save_successful = save_reading_to_firestore(
#                     sbp=sbp,
#                     dbp=dbp,
#                     hr=hr
#                 )
#                 reply_text = (
#                     f"✅ **Blood Pressure Reading Extracted**\n\n"
#                     f"🩺 **Systolic (SBP):** {sbp}\n"
#                     f"❤️ **Diastolic (DBP):** {dbp}\n"
#                     f"💓 **Heart Rate (HR):** {hr}\n\n"
#                 )
#                 if save_successful:
#                     reply_text += "💾 *Data saved to database.*"
#                 else:
#                     reply_text += "⚠️ *Could not save data to database.*"
#             else:
#                 sbp_text = sbp if sbp is not None else 'N/A'
#                 dbp_text = dbp if dbp is not None else 'N/A'
#                 hr_text = hr if hr is not None else 'N/A'
#                 reply_text = (
#                     f"🟡 **Partial Reading Extracted**\n\n"
#                     f"🩺 **Systolic (SBP):** {sbp_text}\n"
#                     f"❤️ **Diastolic (DBP):** {dbp_text}\n"
#                     f"💓 **Heart Rate (HR):** {hr_text}\n\n"
#                     f"💾 *Not saved to database because some values are missing.*"
#                 )
#         elif bp_data and bp_data.get('status' == "failed"):
#             reply_text = f"Sorry, I couldn't read the values. Please try a clearer picture."
#         else:
#             reply_text = f"Sorry, something went wrong. Data is {bp_data}"
#     except Exception as e:
#         reply_text = f"Error encountered:\n\n{traceback.format_exc()}"
#     finally:
#         await context.bot.send_message(chat_id=chat_id, text=reply_text, parse_mode="Markdown")
    

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


async def main() -> None:
    """Set up PTB application and a web application for handling the incoming requests."""
    context_types = ContextTypes(context=CustomContext)
    # Here we set updater to None because we want our custom webhook server to handle the updates
    # and hence we don't need an Updater instance
    application = (
        Application.builder().token(TOKEN).updater(None).context_types(context_types).build()
    )

    # register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(TypeHandler(type=WebhookUpdate, callback=webhook_update))

    application.add_error_handler(error_handler)

    # Pass webhook settings to telegram
    await application.bot.set_webhook(url=f"{URL}/telegram", allowed_updates=Update.ALL_TYPES)

    # Set up webserver
    flask_app = Flask(__name__)

    @flask_app.post("/telegram")  # type: ignore[untyped-decorator]
    async def telegram() -> Response:
        """Handle incoming Telegram updates by putting them into the `update_queue`"""
        await application.update_queue.put(Update.de_json(data=request.json, bot=application.bot))
        return Response(status=HTTPStatus.OK)

    @flask_app.route("/submitpayload", methods=["GET", "POST"])  # type: ignore[untyped-decorator]
    async def custom_updates() -> Response:
        """
        Handle incoming webhook updates by also putting them into the `update_queue` if
        the required parameters were passed correctly.
        """
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

        await application.update_queue.put(WebhookUpdate(user_id=user_id, payload=payload))
        return Response(status=HTTPStatus.OK)

    @flask_app.get("/healthcheck")  # type: ignore[untyped-decorator]
    async def health() -> Response:
        """For the health endpoint, reply with a simple plain text message."""
        response = make_response("The bot is still running fine :)", HTTPStatus.OK)
        response.mimetype = "text/plain"
        return response

    webserver = uvicorn.Server(
        config=uvicorn.Config(
            app=WsgiToAsgi(flask_app),
            port=PORT,
            use_colors=False,
            host="127.0.0.1",
        )
    )

    # Run application and webserver together
    async with application:
        await application.start()
        await webserver.serve()
        await application.stop()


if __name__ == "__main__":
    asyncio.run(main())