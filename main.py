"""
Python bot to operate APRS from Telegram
"""

import os
import logging
from logging.handlers import RotatingFileHandler
import sqlite3
from datetime import datetime
from dateutil import parser
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, CallbackContext

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
    sqlite_cursor.execute("SELECT user_id FROM users WHERE user_id = ? AND approved = True", (update.effective_sender.id,))
    if len(sqlite_cursor.fetchall()) > 0:
        NotImplementedError()
    else:
        await update.message.reply_text(f'You are not registered or approved yet, please send the /start command to begin')

# Parse the location and send it via APRS
async def msg_location(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(f'Received location: `{update}`', parse_mode='MarkdownV2')

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

# Start polling of the bot
def start_telegram_polling() -> None:
    global app_logger
    app_logger.info("Loading token from environment and building application")
    app = ApplicationBuilder().token(load_bot_token()).build()
    app_logger.info("Creating command handlers")
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("setcall", cmd_setcall))
    app.add_handler(MessageHandler(filters.LOCATION, msg_location))
    app_logger.info("Starting polling")
    app.run_polling()

if __name__ == "__main__":
    initialize_logger()
    connect_to_sqlite()
    start_telegram_polling()