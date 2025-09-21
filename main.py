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

# --- Configuration ---
# Load your secret keys from environment variables
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_USER_ID = os.getenv("TELEGRAM_USER_ID")

# Configure the Gemini API client
genai.configure(api_key=GEMINI_API_KEY)

# Set up basic logging to see errors
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

try:
    # Make sure 'firebase-credentials.json' is in the same folder as your bot script
    cred = credentials.Certificate("firebase-credentials.json")
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
        response = await model.generate_content_async([prompt, img])
        
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

# --- Telegram Bot Handlers ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command."""
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Hello! I'm your Blood Pressure reading assistant. Send me a clear picture of your BP monitor's screen."
    )

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



# --- Main Application Setup ---
if __name__ == '__main__':
    if not GEMINI_API_KEY or not TELEGRAM_BOT_TOKEN:
        raise ValueError("API keys not found! Please set GEMINI_API_KEY and TELEGRAM_BOT_TOKEN environment variables.")

    application = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler('start', start))
    application.add_handler(MessageHandler(filters.PHOTO, image_handler))
    
    # Add error handler
    application.add_error_handler(error_handler)
    
    print("Bot is starting... Press Ctrl+C to stop.")
    # Start the bot
    application.run_polling()