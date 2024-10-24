"""
Python bot to operate APRS from Telegram
"""

import os
import logging
from logging.handlers import RotatingFileHandler
import sqlite3
import re
from datetime import datetime
from dateutil import parser
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, CallbackContext

# Configuration
UNAUTHORIZED_MESSAGE = "You are not registered or approved yet, please send the /start command to begin"

# Logger oject
app_logger: logging.Logger

# Database cursor
sqlite_cursor: sqlite3.Connection.cursor
sqlite_connection: sqlite3.Connection

# Initialize logger
def initialize_logger() -> None:
    # Global logger object
    global app_logger
    # Check if log foler exists
    if not os.path.exists("logs"):
        os.makedirs("logs")
    # Create files handler
    log_handler = RotatingFileHandler("logs/bot_output.log", 
                                    mode='a', 
                                    maxBytes=5*1024*1024,
                                    backupCount=2, 
                                    encoding="utf-8",
                                    delay=False)
    # Apply formatter
    log_formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(funcName)s(%(lineno)d):%(message)s')
    log_handler.setFormatter(log_formatter)
    # Set default level to be written
    log_handler.setLevel(logging.INFO)
    # Application logger
    app_logger = logging.getLogger(__name__)
    app_logger.addHandler(log_handler)
    app_logger.setLevel(logging.INFO)
    # Reduce logging for http requests
    logging.getLogger('httpx').setLevel(logging.WARNING)

# Connect to the local SQLite file
def connect_to_sqlite() -> None:
    global sqlite_cursor
    global app_logger
    global sqlite_connection
    # Create database folder if not existing
    app_logger.info("Checking if database folder exists")
    if not os.path.exists("db"):
        app_logger.info("Creating 'db' folder")
        os.makedirs("db")
    # Open the database object
    app_logger.info("Opening connection to database file")
    sqlite_connection = sqlite3.connect("db/database.sqlite")
    # Get a cursor to write queries
    app_logger.info("Creating connection cursor")
    sqlite_cursor = sqlite_connection.cursor()
    # Initialize tables
    app_logger.info("Initializing database tables")
    sqlite_cursor.execute("CREATE TABLE IF NOT EXISTS users (user_name TEXT, user_id INTEGER NOT NULL, registration_date DATETIME NOT NULL, approved BOOL DEFAULT False, user_callsign TEXT, user_comment TEXT)")
    sqlite_connection.commit()

# Starts a conversation with the user
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.info(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")
    sqlite_cursor.execute("SELECT user_id, registration_date, approved FROM users WHERE user_id = ?", (update.effective_sender.id,))
    query_result = sqlite_cursor.fetchall()
    if len(query_result) > 0:
        app_logger.info(f"User with id [{update.effective_user.id}] was already registered")
        first_line = query_result[0]
        await update.message.reply_text(
            f'Welcome back {update.effective_user.first_name}\n\n' +
            f'\- Registration date: `{datetime_print(first_line[1])} UTC`\n' +
            f'\- Account status: `{"approved" if bool(first_line[2]) else "pending approval" }`', 
            parse_mode='MarkdownV2')
    else:
        try:
            app_logger.info(f"Registering new user with id [{update.effective_user.id}]")
            sqlite_cursor.execute("INSERT INTO users(user_name, user_id, registration_date) VALUES (?,?,?)", (update.effective_user.name, update.effective_user.id,datetime.utcnow()))
            sqlite_connection.commit()
            await update.message.reply_text(
                f'Welcome {update.effective_user.first_name}\n' +
                f'You just accessed the IU2FRL APRS bot\n\n' +
                f'\- Registration date: `{datetime_print(datetime.utcnow())} UTC`\n' +
                '\- Account status: `pending approval`', 
                parse_mode='MarkdownV2')
        except Exception as ret_exc:
            app_logger.error(ret_exc)
            await update.message.reply_text(f'Welcome `{update.effective_user.first_name}`\nSomething was wrong while processing your registration request, please try again later')

# Sets the callsign for the user
async def cmd_setcall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.info(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")
    if is_user_approved(update.effective_sender.id):
        clean_message = str(update.effective_message.text).replace("/setcall ", "").strip().split(" ")[0]
        if validate_callsign(clean_message):
            app_logger.info(f"User: [{update._effective_sender.username}] updated callsign to: [{clean_message}]")
            sqlite_cursor.execute("UPDATE users SET user_callsign = ? WHERE user_id = ? ", (clean_message, update.effective_sender.id))
            sqlite_connection.commit()
            await update.message.reply_text(f"Callsign was updated to `{clean_message}`")
        else:
            app_logger.warning(f"User: [{update._effective_sender.username}] tried to updat callsign to: [{clean_message}] which is invalid")
            await update.message.reply_text(f"Callsign `{clean_message}` could not be recognized as valid callsign")
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Parse the location and send it via APRS
async def msg_location(update: Update, context: CallbackContext) -> None:
    if is_user_approved(update.effective_sender.id):
        await update.message.reply_text(f'Received location: `{update}`', parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Check if user is approved
def is_user_approved(user_id: int) -> bool:
    global sqlite_cursor
    sqlite_cursor.execute("SELECT user_id FROM users WHERE user_id = ? AND approved = True", (user_id,))
    if len(sqlite_cursor.fetchall()) > 0:
        return True
    else:
        return False

# Format the datetime to something we can print as message
def datetime_print(input_date: any, markdown: bool = True) -> str:
    if type(input_date) == str:
        input_date = parser.parse(input_date)

    if markdown:
        return input_date.strftime("%d\/%m\/%Y %H\:%M\:%S")
    else:
        return input_date.strftime("%d/%m/%Y %H:%M:%S")

# Load the bot token from environment variables
def load_bot_token() -> str:
    """
    Loads the BOT token from local environment

    Returns:
        str: Token of the bot
    """
    global app_logger
    bot_token = os.environ.get("BOT_TOKEN", None)
    if bot_token is not None:
        return bot_token
    else:
        app_logger.error(f"Cannot load environment variables")
        Exception("Cannot load BOT_TOKEN variable")

# Identify the callsign in the given string
def validate_callsign(input_call: str) -> bool:
    global app_logger
    split_call = input_call.split("/")
    app_logger.debug(f'Split call: [{split_call}]')
    # Callsign is normally the longest one
    call = max(split_call, key = len)
    app_logger.debug(f'Longest record: [{call}]')
    return is_callsign(call)

# Check if the given string matches any callsign
def is_callsign(input_call: str) -> bool:
    #  All amateur radio call signs:
    # [a-zA-Z0-9]{1,3}[0-9][a-zA-Z0-9]{0,3}[a-zA-Z]
    # Non-US call signs:
    # \b(?!K)(?!k)(?!N)(?!n)(?!W)(?!w)(?!A[A-L])(?!a[a-l])[a-zA-Z0-9][a-zA-Z0-9]?[a-zA-Z0-9]?[0-9][a-zA-Z0-9][a-zA-Z0-9]?[a-zA-Z0-9]?[a-zA-Z0-9]?\b
    # US call signs:
    # [AKNWaknw][a-zA-Z]{0,2}[0-9][a-zA-Z]{1,3}
    global app_logger
    app_logger.debug(f'Checking validity for [{input_call}]')
    return re.match("[a-zA-Z0-9]{1,3}[0-9][a-zA-Z0-9]{0,3}[a-zA-Z]", input_call)

# Check if user is the administrator of the application
def is_admin(user_id: int) -> bool:
    global app_logger

    admin_id = get_admin_id()
    app_logger.info(f"Checking if [{user_id}] is admin")

    if admin_id == user_id:
        return True
    else:
        app_logger.error(f"Cannot load environment variables")
        return False

# Get the administrator ID
def get_admin_id() -> int:
    bot_token = os.environ.get("BOT_ADMIN", None)

    if bot_token is None:
        app_logger.error("BOT_TOKEN variable is empty")
        return -1
    else:
        try:
            return int(bot_token)
        except Exception as ret_exc:
            app_logger.error(ret_exc)
            return -1

# Approve new user
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global app_logger
    if is_admin(update.effective_user.id):
        if len(str(update.effective_message.text).split(" ")) != 2:
            await update.message.reply_text(f"Missing or invalid target user to be approved", parse_mode='MarkdownV2')
            return

        try:
            target_user = int(str(update.effective_message.text).replace("/approve ", "").strip().split(" ")[0])
        except Exception as ret_exc:
            app_logger.error(ret_exc)
            await update.message.reply_text(f"Cannot approve user: `{ret_exc}`", parse_mode='MarkdownV2')

        if not is_user_approved(target_user):
            app_logger.info(f"User: [{int(target_user)}] will be approved")
            sqlite_cursor.execute("UPDATE users SET approved = True WHERE user_id = ? ", (target_user,))
            sqlite_connection.commit()
            await update.message.reply_text(f"User `{target_user}` was approved", parse_mode='MarkdownV2')
        else:
            app_logger.info(f"User: [{target_user}] will be disapproved")
            sqlite_cursor.execute("UPDATE users SET approved = False WHERE user_id = ? ", (target_user,))
            sqlite_connection.commit()
            await update.message.reply_text(f"User `{target_user}` was disapproved", parse_mode='MarkdownV2')
    else:
        app_logger.warning(f"User [{update.effective_user.id}] is not an administrator")

# Start polling of the bot
def start_telegram_polling() -> None:
    global app_logger
    app_logger.info("Loading token from environment and building application")
    app = ApplicationBuilder().token(load_bot_token()).build()
    app_logger.info("Creating command handlers")
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setcall", cmd_setcall))
    app.add_handler(CommandHandler("approve", cmd_approve))
    #app.add_handler(CommandHandler("setmsg", cmd_setcall))
    #app.add_handler(CommandHandler("setssid", cmd_setcall))
    app.add_handler(MessageHandler(filters.LOCATION, msg_location))
    app_logger.info("Starting polling")
    app.run_polling()

if __name__ == "__main__":
    initialize_logger()
    connect_to_sqlite()
    start_telegram_polling()