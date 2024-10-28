"""
Python Telegram bot that interfaces with APRS-IS to send position reports and track user locations.
Allows users to configure their APRS parameters and share their location either as one-time positions
or continuous live tracking updates.

Features:
- User registration and admin approval system
- APRS parameter configuration (callsign, SSID, message, update interval)
- One-time position sharing
- Live location tracking with configurable intervals
- SQLite database for user data persistence
"""

import os
import logging
from logging.handlers import RotatingFileHandler
import sqlite3
import re
import socket
import aprslib
import time
import asyncio
from sys import stdout
from datetime import datetime, UTC, timedelta
from dateutil import parser
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters, CallbackContext
from typing import Dict, Optional
from dataclasses import dataclass

# Configuration
UNAUTHORIZED_MESSAGE = "You are not registered or approved yet, please send the /start command to begin and/or to check the registration status"

# Logger oject
app_logger: logging.Logger = None

# Database cursor
sqlite_cursor: sqlite3.Connection.cursor = None
sqlite_connection: sqlite3.Connection = None

# APRS socket
aprs_socket: aprslib.inet.IS = None
aprs_socket_busy: bool = False
aprs_user: str = ""

# Telegram bot
telegram_app = None

@dataclass
class LiveLocationSession:
    """
    Represents an active live location sharing session.
    
    Attributes:
        user_id (int): Telegram user ID of the session owner
        chat_id (int): Telegram chat ID where the session was started
        callsign (str): User's amateur radio callsign
        ssid (str): APRS SSID for the user
        comment (str): APRS comment/status text
        next_update (datetime): When the next position update should be sent
        end_sharing (datetime): When the live sharing session should end
        start_message (int): ID of the message that started the session
    """
    user_id: int
    chat_id: int
    callsign: str
    ssid: str
    comment: str
    next_update: datetime
    end_sharing: datetime
    start_message: int

@dataclass
class UserParameters:
    """
    Contains APRS configuration parameters for a user.
    
    Attributes:
        user_id (int): Telegram user ID
        user_callsign (str): Amateur radio callsign
        user_comment (str): APRS comment/status text
        user_ssid (str): APRS SSID
        aprs_icon (str): APRS icon code
        user_interval (int): Minimum interval between position updates in seconds
        username (text): The Telegram username
        registration_date (datetime): The first login to the app
    """
    user_id: int
    aprs_callsign: str
    aprs_comment: str
    aprs_ssid: str
    aprs_icon: str
    update_interval: int
    username: str
    registration_date: datetime

class CustomLogHandler(logging.Handler):
    def emit(self, record):
        if record.levelno in (logging.WARNING, logging.ERROR):
            self.forward_to_method(record)

    def forward_to_method(self, record):
        try:
            # Replace this with your custom method
            message = f"Received: `{record.levelname}`\n\nError:\n```{str(record)}```"
            # Call the async method in a synchronous way
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:  # No event loop is running
                asyncio.run(send_to_admin(message))
            else:
                loop.create_task(send_to_admin(message))
        except Exception as ret_exc:
            print(f"Cannot forward message to Telegram admin, error: {ret_exc}")

# Global dictionary to store active live location sessions
active_sessions: Dict[int, LiveLocationSession] = {}

# Live location was sent
async def handle_live_location(update: Update, context: CallbackContext) -> None:
    """
    Process incoming live location updates from Telegram users.
    
    Creates or updates a live location tracking session for the user.
    Sends position updates to APRS-IS based on configured intervals.
    
    Args:
        update (Update): The Telegram update containing location data
        context (CallbackContext): The Telegram context
        
    Returns:
        None
    """
    global telegram_app
    new_session = True

    if not is_user_approved(update.effective_sender.id):
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)
        return

    # Get user configuration
    user_config = sqlite_cursor.execute(
        "SELECT user_id, user_callsign, user_comment, user_ssid, aprs_interval FROM users WHERE user_id = ?", 
        (update.effective_sender.id,)
    ).fetchone()
    
    if not user_config:
        await update.message.reply_text("Cannot find user configuration. Please set up your APRS parameters first.")
        return

    user_id, callsign, comment, ssid, db_interval = user_config
    
    # If there's an existing session for this user, cancel it
    for beacon in list(active_sessions.values()):  # Create a copy of values to avoid runtime modification issues
        if user_id == beacon.user_id:
            new_session = False
            app_logger.debug(f"Received coordinates update, but user [{user_id}] already has a tracking session enabled")
            if beacon.next_update > datetime.now(UTC):
                return
    
    # Create new session
    session = LiveLocationSession(
        user_id=user_id,
        chat_id=update.effective_chat.id,
        callsign=callsign,
        ssid=ssid,
        comment=comment,
        next_update=datetime.now(UTC) + timedelta(seconds=db_interval),
        end_sharing=update.effective_message.date + timedelta(seconds=update.effective_message.location.live_period),
        start_message = -1
    )
    
    active_sessions[user_id] = session
    
    if new_session:
        app_logger.info(f"Starting beacon for `{callsign}-{ssid}`")
        initial_message = await telegram_app.bot.sendMessage(
            chat_id=user_id,
            text=
                f"Started live location tracking:\n\n" +
                f"Minimum update interval: `{db_interval}s`\n" +
                f"Sending beacons until: `{datetime_print(session.end_sharing)} UTC`\n",
                #f"Next update after: `{datetime_print(session.next_update)} UTC`",
            parse_mode='MarkdownV2'
        )
        app_logger.info(f"Started logger for user [{user_id}] with first message [{initial_message.id}]")
        active_sessions[user_id].start_message = initial_message.id
    else:
        # For some reason it always return "message not found"
        # await telegram_app.bot.edit_message_text(
        #     chat_id=user_id,
        #     message_id=active_sessions[user_id].start_message, 
        #     text=
        #         f"Live location tracking:\n\n" +
        #         f"Minimum update interval: `{db_interval}s`\n" +
        #         f"Sending beacons until: `{datetime_print(session.end_sharing)} UTC`\n" +
        #         f"Next update after: `{datetime_print(session.next_update)} UTC`",
        #     )
        app_logger.debug(f"Updating position for [{callsign}-{ssid}]")

    aprs_parameters = load_aprs_parameters_for_user(update.effective_user.id)
    if aprs_parameters is not None:
        send_position(aprs_parameters, update.effective_message.location.latitude, update.effective_message.location.longitude)

# Stop the process
async def stop_live_tracking(user_id: int) -> bool:
    """
    Stop live location tracking for a specific user.
    
    Args:
        user_id (int): Telegram user ID to stop tracking
        
    Returns:
        bool: True if tracking was stopped, False if no active tracking found
    """
    # If there's an existing session for this user, cancel it
    deleted_tracker = False
    for beacon in list(active_sessions.values()):  # Create a copy of values to avoid runtime modification issues
        if beacon.user_id == user_id:
            try:
                del active_sessions[user_id]
                app_logger.info(f"Stopped tracking for user {user_id}")
                deleted_tracker = True
            except Exception as ret_exc:
                app_logger.error(f"Cannot delete tracker for: {user_id}, error: {ret_exc}")
    return deleted_tracker

# Initialize logger
def initialize_logger() -> None:
    """
    Initialize the application logger with file and console handlers.
    
    Creates a rotating file handler that keeps backup log files and
    adds both file and console output handlers to the logger.
    """
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
    # Add stdio output
    consoleHandler = logging.StreamHandler(stdout)
    consoleHandler.setFormatter(log_formatter)
    app_logger.addHandler(consoleHandler)
    # Add the custom handler to forward warning and errors
    custom_handler = CustomLogHandler()
    custom_handler.setFormatter(log_formatter)
    app_logger.addHandler(custom_handler)

# Datetime handler for sqlite3
def adapt_datetime(dt):
    return dt.isoformat()

# Datetime handler for sqlite3
def convert_datetime(s):
    if isinstance(s, str):
        return datetime.fromisoformat(s)
    # Handle bytes case which can occur when reading from SQLite
    if isinstance(s, bytes):
        return datetime.fromisoformat(s.decode())
    raise ValueError(f"Cannot convert {type(s)} to datetime")

# Connect to the local SQLite file
def connect_to_sqlite() -> None:
    """
    Initialize SQLite database connection and create required tables.
    
    Creates database directory if it doesn't exist and initializes
    the users table with required columns for APRS configuration.
    """
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
    sqlite3.register_adapter(datetime, adapt_datetime)
    sqlite3.register_converter("datetime", convert_datetime)
    sqlite_connection = sqlite3.connect(
        "db/database.sqlite",
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES
        )
    # Get a cursor to write queries
    app_logger.info("Creating connection cursor")
    sqlite_cursor = sqlite_connection.cursor()
    # Initialize tables
    app_logger.info("Initializing database tables")
    # Create the table if it doesn't exist
    sqlite_cursor.execute(
        "CREATE TABLE IF NOT EXISTS users (" +
            "user_name TEXT DEFAULT \"\", " +
            "user_id INTEGER NOT NULL, " + 
            "registration_date DATETIME NOT NULL, " + 
            "approved BOOL DEFAULT False, " + 
            "user_callsign TEXT DEFAULT \"\", " +
            "user_comment TEXT DEFAULT \"IU2FRL Telegram APRS bot\", " + 
            "user_ssid TEXT DEFAULT \"9\", " + 
            "aprs_interval INTEGER DEFAULT 30, " +
            "aprs_icon TEXT DEFAULT \"$/\"" +
        ")"
    )
    sqlite_connection.commit()

    # Add or modify columns if they don't exist
    columns_to_add = [
        ("user_name", "TEXT DEFAULT \"\""),
        ("user_id", "INTEGER NOT NULL"),
        ("registration_date", "DATETIME NOT NULL"),
        ("approved", "BOOL DEFAULT False"),
        ("user_callsign", "TEXT DEFAULT \"\""),
        ("user_comment", "TEXT DEFAULT \"IU2FRL Telegram APRS bot\""),
        ("user_ssid", "TEXT DEFAULT \"9\""),
        ("aprs_interval", "INTEGER DEFAULT 30"),
        ("aprs_icon", "TEXT DEFAULT \"$/\"")
    ]

    for column_name, column_definition in columns_to_add:
        try:
            sqlite_cursor.execute(f"ALTER TABLE users ADD COLUMN {column_name} {column_definition}")
            app_logger.info(f"Updating column {column_name}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e):
                app_logger.info(f"Column {column_name} already exists")
                pass  # Column already exists, no need to add
            else:
                raise

    sqlite_connection.commit()

# Starts a conversation with the user
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the /start command to register new users or check registration status.
    
    Args:
        update (Update): The Telegram update containing the command
        context (ContextTypes.DEFAULT_TYPE): The Telegram context
        
    Returns:
        None
    """
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.debug(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")
    sqlite_cursor.execute("SELECT user_id, registration_date, approved FROM users WHERE user_id = ?", (update.effective_sender.id,))
    query_result = sqlite_cursor.fetchall()
    if len(query_result) > 0:
        app_logger.info(f"User with id [{update.effective_user.id}] was already registered")
        first_line = query_result[0]
        await update.message.reply_text(
            f'Welcome back {update.effective_user.first_name}\n\n' +
            f' Registration date: `{datetime_print(first_line[1])} UTC`\n' +
            f' Account status: `{"approved" if bool(first_line[2]) else "pending approval" }`', 
            parse_mode='MarkdownV2')
    else:
        try:
            app_logger.info(f"Registering new user with id [{update.effective_user.id}]")
            sqlite_cursor.execute("INSERT INTO users(user_name, user_id, registration_date) VALUES (?,?,?)", (update.effective_user.name, update.effective_user.id,datetime.now(UTC)))
            sqlite_connection.commit()
            await update.message.reply_text(
                f'Welcome {update.effective_user.first_name}\n' +
                f'You just accessed the IU2FRL APRS bot\n\n' +
                f' Registration date: `{datetime_print(datetime.now(UTC))} UTC`\n' +
                ' Account status: `pending approval`', 
                parse_mode='MarkdownV2')
        except Exception as ret_exc:
            app_logger.error(ret_exc)
            await update.message.reply_text(f'Welcome `{update.effective_user.first_name}`\nSomething was wrong while processing your registration request, please try again later', parse_mode='MarkdownV2')
        # Send notification to admin
        await send_to_admin(
            r"New user registered\: \@" +  str(update.effective_user.username) + "\n\n" + 
            r"Name\: " + str(update.effective_sender.first_name) + "\n" + 
            r"Surname\: " + str(update.effective_sender.last_name) + "\n" +
            r"Approve it with: `/approve " + str(update.effective_user.id) + "`")

# Sets the callsign for the user
async def cmd_setcall(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sets or updates a user's amateur radio callsign in the database.

    This command handler processes the /setcall command, validates the provided
    callsign, and updates it in the database for approved users. The command
    expects the format '/setcall CALLSIGN' where CALLSIGN is a valid amateur
    radio callsign.

    Args:
        update (Update): The update object containing message and user information
        context (ContextTypes.DEFAULT_TYPE): The context object for the callback

    Returns:
        None

    Notes:
        - Only approved users (verified via is_user_approved()) can use this command
        - Callsigns are automatically converted to uppercase
        - The command requires exactly one argument (the callsign)
        - Callsigns are validated through validate_callsign() before being accepted
        - Updates are logged using app_logger at various severity levels
        - Database updates are performed using global sqlite cursor and connection

    Example:
        User input: /setcall W1AW
        Bot response: "Callsign was updated to W1AW"

    Raises:
        No explicit exceptions, but may raise database errors during execution
    """
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.debug(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")
    if is_user_approved(update.effective_sender.id):
        # Check if the callsign was provided
        if len(update.effective_message.text.split(" ")) != 2:
            app_logger.warning(f"Invalid callsign received [{update.effective_message.text}] from [{update.effective_sender.id}]")
            await update.message.reply_text(f"Cannot detect callsign argument, syntax is: `/setcall AA0BBB`", parse_mode='MarkdownV2')
            return
        # Clean the string and check validity
        clean_message = str(update.effective_message.text).replace("/setcall ", "").strip().split(" ")[0].upper()
        try:
            call = validate_callsign(clean_message)
            app_logger.info(f"User: [{update._effective_sender.username}] updated callsign to: [{call}]")
            sqlite_cursor.execute("UPDATE users SET user_callsign = ? WHERE user_id = ? ", (call, update.effective_sender.id))
            sqlite_connection.commit()
            await update.message.reply_text(f"Callsign was updated to `{call}`", parse_mode='MarkdownV2')
        except:
            app_logger.warning(f"User: [{update._effective_sender.username}] tried to update callsign to: [{clean_message}] which is invalid")
            await update.message.reply_text(f"The requested callsign `{clean_message}` could not be recognized as valid callsign, please remove all subfixes and prefixes and try again", parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Sets the message for the user
async def cmd_setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sets or updates a user's custom message in the database.

    This command handler processes the /setmsg command and updates the user's 
    custom message in the database for approved users. The command expects
    the format '/setmsg MESSAGE' where MESSAGE is any non-empty text string.

    Args:
        update (Update): The update object containing message and user information
        context (ContextTypes.DEFAULT_TYPE): The context object for the callback

    Returns:
        None

    Notes:
        - Only approved users (verified via is_user_approved()) can use this command
        - The message must be non-empty after stripping whitespace
        - Leading/trailing whitespace is automatically removed
        - Updates are logged using app_logger at various severity levels
        - Database updates are performed using global sqlite cursor and connection
        - The command requires at least one word for the message

    Example:
        User input: /setmsg Hello from Seattle!
        Bot response: "Message was updated to Hello from Seattle!"

    Raises:
        No explicit exceptions, but may raise database errors during execution
    """
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.debug(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")
    if is_user_approved(update.effective_sender.id):
        # Check if the message was provided
        if len(update.effective_message.text.split(" ")) < 2:
            app_logger.warning(f"Invalid message received [{update.effective_message.text}]")
            await update.message.reply_text(r"Cannot detect message argument, syntax is: `/setmsg Hello World!`", parse_mode='MarkdownV2')
            return
        # Clean the string and check validity
        clean_message = str(update.effective_message.text).replace("/setmsg ", "").strip()
        if len(clean_message) > 0:
            app_logger.info(f"User: [{update._effective_sender.username}] updated message to: [{clean_message}]")
            sqlite_cursor.execute("UPDATE users SET user_comment = ? WHERE user_id = ? ", (clean_message, update.effective_sender.id))
            sqlite_connection.commit()
            await update.message.reply_text(f"Message was updated to `{clean_message}`", parse_mode='MarkdownV2')
        else:
            app_logger.warning(f"User: [{update._effective_sender.username}] tried to update message to: [{clean_message}] which is invalid")
            await update.message.reply_text(f"The requested message `{clean_message}` could not processed", parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Sets the message for the user
async def cmd_setssid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sets the SSID (Service Set Identifier) for the user.

    This function handles the `/setssid` command, allowing approved users to set their SSID.
    It performs the following steps:
    1. Logs the entry into the method and the sender's ID.
    2. Checks if the user is approved.
    3. Validates the provided SSID argument.
    4. Updates the SSID in the database if valid.
    5. Sends appropriate response messages to the user.

    Args:
        update (Update): The update object containing the message and user information.
        context (ContextTypes.DEFAULT_TYPE): The context object for the callback.

    Returns:
        None
    """
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.debug(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")
    if is_user_approved(update.effective_sender.id):
        # Check if the message was provided
        if len(update.effective_message.text.split(" ")) != 2:
            app_logger.warning(f"Invalid SSID received [{update.effective_message.text}]")
            await update.message.reply_text(r"Cannot detect SSID argument, syntax is: `/setssid 9`", parse_mode='MarkdownV2')
            return
        # Clean the string and check validity
        clean_message = str(update.effective_message.text).replace("/setssid ", "").strip().upper()
        if len(clean_message) in [1, 2] :
            app_logger.info(f"User: [{update._effective_sender.username}] updated SSID to: [{clean_message}]")
            sqlite_cursor.execute("UPDATE users SET user_ssid = ? WHERE user_id = ? ", (clean_message, update.effective_sender.id))
            sqlite_connection.commit()
            await update.message.reply_text(f"SSID was updated to `{clean_message}`", parse_mode='MarkdownV2')
        else:
            app_logger.warning(f"User: [{update._effective_sender.username}] tried to update SSID to: [{clean_message}] which is invalid")
            await update.message.reply_text(f"The requested SSID `{clean_message}` could not processed, length must be 1 or 2 characters", parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Sets the APRS map icon for the user
async def cmd_seticon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sets the icon for the user.

    This function handles the `/seticon` command, allowing approved users to set their icon.
    It performs the following steps:
    1. Logs the entry into the method and the sender's ID.
    2. Checks if the user is approved.
    3. Validates the provided icon argument.
    4. Updates the icon in the database if valid.
    5. Sends appropriate response messages to the user.

    Args:
        update (Update): The update object containing the message and user information.
        context (ContextTypes.DEFAULT_TYPE): The context object for the callback.

    Returns:
        None
    """
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.debug(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")
    if is_user_approved(update.effective_sender.id):
        # Check if the message was provided
        if len(update.effective_message.text.split(" ")) != 2:
            app_logger.warning(f"Invalid icon received [{update.effective_message.text}]")
            await update.message.reply_text(r"Cannot detect icon argument, syntax is: `/seticon XX`", parse_mode='MarkdownV2')
            return
        # Clean the string and check validity
        clean_message = str(update.effective_message.text).replace("/seticon ", "").strip().upper()
        if len(clean_message) == 2:
            app_logger.info(f"User: [{update._effective_sender.username}] updated icon to: [{clean_message}]")
            sqlite_cursor.execute("UPDATE users SET aprs_icon = ? WHERE user_id = ? ", (clean_message, update.effective_sender.id))
            sqlite_connection.commit()
            await update.message.reply_text(f"Icon was updated to `{clean_message}`", parse_mode='MarkdownV2')
        else:
            app_logger.warning(f"User: [{update._effective_sender.username}] tried to update icon to: [{clean_message}] which is invalid")
            await update.message.reply_text(f"The requested icon `{clean_message}` could not processed, length must be 2 characters", parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Sets the update interval for the user
async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sets the update interval for the user.

    This function handles the `/setinterval` command, allowing approved users to set their update interval.
    It performs the following steps:
    1. Logs the entry into the method and the sender's ID.
    2. Checks if the user is approved.
    3. Validates the provided interval argument.
    4. Updates the interval in the database if valid.
    5. Sends appropriate response messages to the user.

    Args:
        update (Update): The update object containing the message and user information.
        context (ContextTypes.DEFAULT_TYPE): The context object for the callback.

    Returns:
        None
    """
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.debug(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")
    if is_user_approved(update.effective_sender.id):
        # Check if the message was provided
        if len(update.effective_message.text.split(" ")) != 2:
            app_logger.warning(f"Invalid time received [{update.effective_message.text}]")
            await update.message.reply_text(r"Cannot detect interval value, syntax is: `/setinterval 120`", parse_mode='MarkdownV2')
            return
        # Clean the string and check validity
        clean_message = str(update.effective_message.text).replace("/setinterval ", "").strip()
        try:
            update_time = int(clean_message)
            app_logger.info(f"User: [{update._effective_sender.username}] updated interval to: [{update_time}]s")
            sqlite_cursor.execute("UPDATE users SET aprs_interval = ? WHERE user_id = ? ", (update_time, update.effective_sender.id))
            sqlite_connection.commit()
            await update.message.reply_text(f"Update interval was updated to `{clean_message}` seconds", parse_mode='MarkdownV2')
        except Exception as ret_exc:
            app_logger.warning(f"User: [{update._effective_sender.username}] tried to update interval to: [{clean_message}]s which is invalid, error: {ret_exc}")
            await update.message.reply_text(f"The requested update interval `{clean_message}` could not processed, please try again", parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Sets the message for the user
async def cmd_printcfg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Prints the current configuration for the user.

    This function handles the `/printcfg` command, allowing approved users to view their current configuration.
    It performs the following steps:
    1. Logs the entry into the method and the sender's ID.
    2. Checks if the user is approved.
    3. Retrieves the user's configuration from the database.
    4. Sends the configuration details to the user.
    5. Handles any errors that occur during the process.

    Args:
        update (Update): The update object containing the message and user information.
        context (ContextTypes.DEFAULT_TYPE): The context object for the callback.

    Returns:
        None
    """
    global app_logger
    global sqlite_cursor
    global sqlite_connection

    # Register new users to the application
    app_logger.debug(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")

    if is_user_approved(update.effective_sender.id):
        try:
            result = load_aprs_parameters_for_user(update.effective_sender.id)
            await update.message.reply_text(
                "Current configuration:\n\n" +
                f"User ID: `{result.user_id}`\n" +
                f"Callsign: `{result.aprs_callsign}`\n" + 
                f"SSID: `{result.aprs_ssid}`\n" + 
                f"APRS callsign: `{result.aprs_callsign}-{result.aprs_ssid}`\n" +
                f"Comment: `{result.aprs_comment}`\n" +
                f"Icon: `{result.aprs_icon}`\n" +
                f"Beacon interval: `{result.update_interval}s`",
                parse_mode='MarkdownV2')
        except Exception as ret_exc:
            app_logger.error(ret_exc)
            await update.message.reply_text("Error while retrieving data from the database, please try again later")
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Parse the location and send it via APRS
async def msg_location(update: Update, context: CallbackContext) -> None:
    """
    Handles the user's location message.

    This function processes the `/msg_location` command, allowing approved users to send their location.
    It performs the following steps:
    1. Checks if the user is approved.
    2. Determines if the location is live or regular.
    3. Handles live location updates.
    4. Processes regular location updates.
    5. Sends the location to the APRS system.
    6. Stops live tracking if applicable.
    7. Sends appropriate response messages to the user.
    8. Handles any errors that occur during the process.

    Args:
        update (Update): The update object containing the message and user information.
        context (CallbackContext): The context object for the callback.

    Returns:
        None
    """
    if not is_user_approved(update.effective_sender.id):
        await telegram_app.bot.sendMessage(update.effective_sender.id, UNAUTHORIZED_MESSAGE)
        return
        
    if update.effective_message.location.live_period:
        # Handle live location
        await handle_live_location(update, context)
    else:
        # Handle regular location (existing code)
        aprs_parameters = load_aprs_parameters_for_user(update.effective_user.id)
        if aprs_parameters is not None and update.message is not None:
            try:
                send_position(aprs_parameters, update.effective_message.location.latitude, update.effective_message.location.longitude)
                await update.effective_message.reply_text(
                    f'Position was sent:\n\nLatitude: `{update.effective_message.location.latitude}`\n' + 
                    f'Longitude: `{update.effective_message.location.longitude}`\n' +
                    r'Map link\: https\:\/\/aprs\.fi\/\#\!call\=a\%2F' + aprs_parameters.aprs_callsign + r'\-' + aprs_parameters.aprs_ssid + r'\&timerange\=3600\&tail\=3600',
                    parse_mode='MarkdownV2'
                )
            except Exception as ret_exc:
                app_logger.error(ret_exc)
                await update.message.reply_text('An error occurred while sending the APRS location, please try again')
        else:
            if update.message is not None:
                await update.message.reply_text('Some configuration field is invalid or missing, please check instructions')
            else:
                app_logger.warning("Cannot reply to message, it was probably deleted")
        
        try:
            deleted_tracker = await stop_live_tracking(update.effective_user.id)
            if deleted_tracker:
                await send_to_user("Beaconing was stopped", update.effective_user.id)
        except Exception as ret_exc:
            app_logger.info(f"No trackers were enabled for user {update.effective_user.id}, error: {ret_exc}")

# Load parameters from DB
def load_aprs_parameters_for_user(user_id: int) -> UserParameters:
    """
    Loads APRS parameters for a given user from the database.

    This function retrieves the APRS (Automatic Packet Reporting System) parameters for a user based on their user ID.
    It performs the following steps:
    1. Executes a SQL query to fetch the user's parameters from the database.
    2. Handles any exceptions that occur during the database query.
    3. Checks if the query returned the expected result.
    4. Constructs and returns an AprsParameters object if the data is valid.
    5. Logs a warning if the query result is unexpected.

    Args:
        user_id (int): The ID of the user whose parameters are to be loaded.

    Returns:
        AprsParameters: An object containing the user's APRS parameters, or None if an error occurs or the data is invalid.
    """
    try:
        line = sqlite_cursor.execute(
                "SELECT user_id, user_callsign, user_comment, user_ssid, aprs_icon, aprs_interval, registration_date, user_name FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
    except Exception as ret_exc:
        app_logger.error(f"Cannot load user data from database, error: {ret_exc}")
        return None

    if line is not None and len(line) == 8:
        return UserParameters(
            user_id=line[0],
            aprs_callsign=line[1],
            aprs_comment=line[2],
            aprs_ssid=line[3],
            aprs_icon=line[4],
            update_interval=line[5],
            registration_date=line[6],
            username=line[7]
        )
    else:
        app_logger.warn("Database returned an unexpected result length for this query")
        return None

# Send the help message
async def cmd_help(update: Update, context: CallbackContext) -> None:
    """
    Prints the instructions for all users

    Args:
        update (Update): The update object containing the message and user information.
        context (CallbackContext): The context object for the callback.

    Returns:
        None
    """
    try:
        await update.message.reply_text(
            r"Here are the instructions for the APRS bot, there are few simple steps to configure it" + "\n\n" +
            r"First you need to start the communication with the bot using the command `/start`, this will add you to the database\." + "\n" +
            r"The same `/start` command can also be used to check if your account was enabled by an administrator, this is a manual process and may take some time\." + "\n\n" +
            r"Once your account is enabled, you can start configure the APRS parameters as follows:" + "\n" +
            r"`/setcall AA0BBB` to set your callsign to AA0BBB" + "\n" +
            r"`/setssid 9` to set your APRS SSID to 9 \(default value for mobile stations\)" + "\n" +
            r"`/seticon $/` to set your APRS icon to a phone icon" + "\n" +
            r"`/setinterval 120` to set the minimum beaconing interval to 120s" + "\n" +
            r"`/setmsg Hello` to set the APRS message to be sent" + "\n\n" +
            r"`/printcfg` can be used to validate the APRS parameters, make sure to use it before sending any position" + "\n\n" +
            r"Once everything is setup, you can just send your position and this will be sent to the APRS\-IS server\. You can also share a live position to enable automatic beaconing."
        , parse_mode='MarkdownV2')
    except Exception as ret_exc:
        app_logger.error(ret_exc)

# Check if user is approved
def is_user_approved(user_id: int) -> bool:
    """
    Checks if a user is approved.

    This function verifies whether a user with the given user ID is approved by querying the database.
    It performs the following steps:
    1. Executes a SQL query to check if the user is approved.
    2. Returns True if the user is approved, otherwise returns False.

    Args:
        user_id (int): The ID of the user to check.

    Returns:
        bool: True if the user is approved, False otherwise.
    """
    global sqlite_cursor
    sqlite_cursor.execute("SELECT user_id FROM users WHERE user_id = ? AND approved = True", (user_id,))
    if len(sqlite_cursor.fetchall()) > 0:
        return True
    else:
        return False

# Format the datetime to something we can print as message
def datetime_print(input_date: any, markdown: bool = True) -> str:
    """
    Formats a given date into a string.

    This function converts a date input into a formatted string. If the input is a string, it parses it into a datetime object.
    It performs the following steps:
    1. Checks if the input date is a string and parses it if necessary.
    2. Formats the date into a string, with optional Markdown escaping.

    Args:
        input_date (any): The date to format, either as a string or a datetime object.
        markdown (bool): Whether to format the string with Markdown escaping. Defaults to True.

    Returns:
        str: The formatted date string.
    """
    if type(input_date) == str:
        input_date = parser.parse(input_date)

    if markdown:
        return input_date.strftime(r"%d\/%m\/%Y %H\:%M\:%S")
    else:
        return input_date.strftime("%d/%m/%Y %H:%M:%S")

# Load the bot token from environment variables
def load_bot_token() -> str:
    """
    Loads the bot token from environment variables.

    This function retrieves the bot token from the local environment variables.
    It performs the following steps:
    1. Attempts to load the bot token from the environment variable `BOT_TOKEN`.
    2. Logs an error and raises an exception if the token is not found.

    Returns:
        str: The bot token.

    Raises:
        Exception: If the `BOT_TOKEN` environment variable is not found.
    """
    global app_logger
    bot_token = os.environ.get("BOT_TOKEN", None)
    if bot_token is not None:
        return bot_token
    else:
        app_logger.error(f"Cannot load environment variables")
        raise Exception("Cannot load BOT_TOKEN variable")

# Check if callsign is valid by removing prefixes and suffixes
def validate_callsign(input_call: str) -> str:
    """
    Identifies and validates the callsign in the given string.

    This function splits the input string by '/' and selects the longest segment as the callsign.
    It then checks if this segment is a valid callsign. If valid, it returns the callsign; otherwise, it raises an exception.

    Args:
        input_call (str): The input string containing the callsign.

    Returns:
        str: The validated callsign.

    Raises:
        Exception: If the identified callsign is not valid.
    """
    global app_logger
    split_call = input_call.split("/")
    app_logger.debug(f'Split call: [{split_call}]')
    # Callsign is normally the longest one
    call = max(split_call, key = len)
    app_logger.debug(f'Longest record: [{call}]')
    if is_callsign(call):
        return call
    else:
        app_logger.warning(f"The callsign [{call}] does not appear to be valid")
        raise Exception("Invalid callsign")

# Use regex to validate callsign
def is_callsign(input_call: str) -> bool:
    """
    Checks if the given string matches any callsign pattern.

    This function uses regular expressions to validate if the input string matches the pattern for amateur radio call signs.
    It supports both US and non-US call signs.

    Args:
        input_call (str): The input string to check.

    Returns:
        bool: True if the input string is a valid callsign, False otherwise.
    """
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
    """
    Checks if a user is an admin.

    This function verifies whether a user with the given user ID is an admin by comparing it with the admin ID.
    It performs the following steps:
    1. Retrieves the admin ID.
    2. Logs the check process.
    3. Compares the user ID with the admin ID.
    4. Returns True if the user ID matches the admin ID, otherwise returns False.

    Args:
        user_id (int): The ID of the user to check.

    Returns:
        bool: True if the user is an admin, False otherwise.
    """
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
    """
    Retrieves the admin ID from environment variables.

    This function attempts to load the admin ID from the environment variable `BOT_ADMIN`.
    It performs the following steps:
    1. Retrieves the `BOT_ADMIN` environment variable.
    2. Logs an error and returns -1 if the variable is not found.
    3. Converts the variable to an integer and returns it.
    4. Handles any exceptions during the conversion, logs the error, and returns -1.

    Returns:
        int: The admin ID if successfully retrieved and converted, otherwise -1.
    """
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

# Get the APRS IS address
def get_aprs_is() -> str:
    return os.environ.get("APRS_SERVER", "rotate.aprs2.net")

# Get the APRS port
def get_aprs_port() -> int:
    """
    Retrieves the APRS port from environment variables.

    This function attempts to load the APRS port from the environment variable `APRS_PORT`.
    It performs the following steps:
    1. Retrieves the `APRS_PORT` environment variable.
    2. Returns the default port 14580 if the variable is not found.
    3. Converts the variable to an integer and returns it.
    4. Handles any exceptions during the conversion, logs the error, and returns the default port 14580.

    Returns:
        int: The APRS port if successfully retrieved and converted, otherwise 14580.
    """
    port = os.environ.get("APRS_PORT", None)

    if port is None:
        return 14580
    else:
        try:
            return int(port)
        except Exception as ret_exc:
            app_logger.error(ret_exc)
            return 14580

# Convert the coordinates to APRS format
def decimal_to_aprs(latitude, longitude):
    """
    Converts decimal latitude and longitude to APRS format.

    This function converts latitude and longitude from decimal degrees to the APRS (Automatic Packet Reporting System) position format.
    It performs the following steps:
    1. Converts latitude to degrees, minutes, and direction (N/S).
    2. Converts longitude to degrees, minutes, and direction (E/W).
    3. Formats the latitude and longitude into the APRS position format (DDMM.MM[N/S] and DDDMM.MM[E/W]).

    Args:
        latitude (float): The latitude in decimal degrees.
        longitude (float): The longitude in decimal degrees.

    Returns:
        tuple: A tuple containing the formatted latitude and longitude strings in APRS format.
    """
    # Latitude conversion
    lat_deg = int(abs(latitude))  # Degrees
    lat_min = (abs(latitude) - lat_deg) * 60  # Minutes
    lat_dir = 'N' if latitude >= 0 else 'S'  # Direction

    # Longitude conversion
    lon_deg = int(abs(longitude))  # Degrees
    lon_min = (abs(longitude) - lon_deg) * 60  # Minutes
    lon_dir = 'E' if longitude >= 0 else 'W'  # Direction

    # Format into APRS position format (DDMM.MM[N/S] and DDDMM.MM[E/W])
    lat_aprs = f"{lat_deg:02d}{lat_min:05.2f}{lat_dir}"
    lon_aprs = f"{lon_deg:03d}{lon_min:05.2f}{lon_dir}"

    return lat_aprs, lon_aprs

# Send the APRS position
def send_position(aprs_details: UserParameters, latitude: float, longitude: float) -> None:
    """
    Sends the user's position to the APRS-IS network.

    This function handles the process of sending a position report to the APRS-IS (Automatic Packet Reporting System - Internet Service) network.
    It performs the following steps:
    1. Waits if the APRS socket is busy.
    2. Connects to the APRS-IS server if not already connected.
    3. Converts the latitude and longitude to APRS format.
    4. Creates and sends the APRS position report.
    5. Handles any exceptions that occur during the process.

    Args:
        aprs_details (AprsParameters): The APRS parameters for the user.
        latitude (float): The latitude of the position to send.
        longitude (float): The longitude of the position to send.

    Returns:
        None

    Raises:
        Exception: If the APRS packet cannot be sent.
    """
    global app_logger
    global aprs_socket
    global aprs_socket_busy
    global aprs_user

    while aprs_socket_busy:
        time.sleep(1)

    try:
        # Prevent multiple socket operations
        aprs_socket_busy = True

        # If socket is not connected, perform a connection
        if aprs_socket is None or not aprs_socket._connected:
            app_logger.info("Loading APRS-IS parameters")
            aprs_host = get_aprs_is()
            aprs_port = get_aprs_port()
            aprs_user = os.environ.get("APRS_USER", None)
            if aprs_user is None:
                app_logger.warning("No APRS_USER was configured, APRS-IS will run in read-only mode")
                aprs_socket = aprslib.IS("N0CALL", host=aprs_host, port=aprs_port)
            else:
                aprs_pass = aprslib.passcode(aprs_user)
                app_logger.info(f"Connecting to APRS-IS with user: [{aprs_user}] and passcode: [{aprs_pass}]")
                aprs_socket = aprslib.IS(aprs_user, passwd=aprs_pass, host=aprs_host, port=aprs_port)
            # Open connection
            app_logger.info("Opening connection to the server")
            aprs_socket.connect(blocking=False, retry=3)
            #app_logger.info("Creating callback for the server")
            #aprs_socket.consumer(aprs_callback, raw=True)
            # send a single status message
            app_logger.info("Sending status message")
            aprs_socket.sendall("N0CALL>APRS,TCPIP*:>status text")
            app_logger.info("Logged to the server")

        # Coordinates conversion
        aprs_lat, aprs_lon = decimal_to_aprs(latitude, longitude)
        timestamp = time.strftime("%H%M%Sz", time.gmtime())

        # Create the APRS position report
        message = {
            'from': f"{aprs_details.aprs_callsign}-{aprs_details.aprs_ssid}",
            'to': 'APRS',
            'msg': f"={aprs_lat}/{aprs_lon}{aprs_details.aprs_icon}{aprs_details.aprs_comment}",
            #'msg': f"@{timestamp}{aprs_lat}/{aprs_lon}{aprs_details.aprs_icon}{aprs_details.aprs_comment}", <- Creates issues with timestamp
            'path': f'APRS,TCPIP*,qAC,{aprs_user}'  # Digipeater path
        }

        # Send the APRS message
        aprs_packet = f"{message['from']}>{message['path']}:{message['msg']}"
        app_logger.info(f"Sending: [{aprs_packet}]")
        aprs_socket.sendall(aprs_packet)
        app_logger.info(f"Package was sent succesfully")
    except Exception as ret_exc:
        app_logger.error(ret_exc)
        raise Exception("Cannot send APRS packet")
    finally:
        aprs_socket_busy = False

# APRS callback
def aprs_callback(aprs_packet):
    raise NotImplementedError()

# Approve new user
async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Approves or disapproves a user based on the command.

    This function handles the `/approve` command, allowing an admin to approve or disapprove a user.
    It performs the following steps:
    1. Checks if the command sender is an admin.
    2. Validates the command format and extracts the target user ID.
    3. Approves the user if they are not already approved.
    4. Disapproves the user if they are already approved.
    5. Sends appropriate response messages to the admin and the target user.
    6. Logs the process and handles any exceptions.

    Args:
        update (Update): The update object containing the message and user information.
        context (ContextTypes.DEFAULT_TYPE): The context object for the callback.

    Returns:
        None
    """
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
            await send_to_user("Hurray! Your account was activated!", target_user)
        else:
            app_logger.info(f"User: [{target_user}] will be disapproved")
            sqlite_cursor.execute("UPDATE users SET approved = False WHERE user_id = ? ", (target_user,))
            sqlite_connection.commit()
            await update.message.reply_text(f"User `{target_user}` was disapproved", parse_mode='MarkdownV2')
    else:
        app_logger.warning(f"User [{update.effective_user.id}] is not an administrator")

# Lists all users
async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Lists all registered users.

    This function handles the `/listusers` command, allowing an admin to fetch and display a list of all registered users.
    It performs the following steps:
    1. Checks if the command sender is an admin.
    2. Sends an initial message indicating that the user list is being fetched.
    3. Queries the database for all user IDs.
    4. Updates the initial message with the number of users found.
    5. Iterates through the list of user IDs, fetching and displaying each user's details.
    6. Sends a message if no users are found.
    7. Logs the process and handles any exceptions.

    Args:
        update (Update): The update object containing the message and user information.
        context (ContextTypes.DEFAULT_TYPE): The context object for the callback.

    Returns:
        None
    """
    global app_logger
    if is_admin(update.effective_user.id):
        global sqlite_cursor
        first_message = await update.message.reply_text(f"Fetching users list, please wait", parse_mode='MarkdownV2')
        id_list = sqlite_cursor.execute("SELECT user_id FROM users WHERE True").fetchall()
        if len(id_list) > 0:
            await telegram_app.bot.edit_message_text(chat_id=update.effective_user.id, message_id=first_message.id, text=f"Found {len(id_list)} users")
            for user in id_list:
                user_data = load_aprs_parameters_for_user(user[0])
                if user_data is not None:
                    await update.message.reply_text(f"User id: `{user_data.user_id}`\nCallsign: `{user_data.aprs_callsign}`\nComment: `{user_data.aprs_comment}`\nUsername: {user_data.username}\nRegistration date: `{datetime_print(user_data.registration_date)} UTC`", parse_mode='MarkdownV2')
                else:
                    await update.message.reply_text(f"Cannot load data for user id `{user[0]}`", parse_mode='MarkdownV2')
        else:
            await telegram_app.bot.edit_message_text(chat_id=update.effective_user.id, message_id=first_message.id, text="No users were found")
    else:
        app_logger.warning(f"User [{update.effective_user.id}] is not an administrator")

# Get new location and update
async def update_live_location(update: Update, context: CallbackContext) -> None:
    """
    Update stored location for a user.

    This function updates the stored live location for a user in the context's user data.

    Args:
        update (Update): The update object containing the message and user information.
        context (CallbackContext): The context object for the callback.

    Returns:
        None
    """
    user_id = update.effective_user.id
    context.user_data[f"live_location_{user_id}"] = update.message.location

# Check if some beacons have to be removed
async def stop_old_beacons() -> None:
    """
    Stop all beacons older than the sharing time.

    This function continuously checks for and stops any active beacons that have exceeded their sharing time.
    It performs the following steps:
    1. Retrieves the current time.
    2. Iterates through active sessions to find expired beacons.
    3. Stops the live tracking for expired beacons and notifies the user.
    4. Logs the process and handles any exceptions.
    5. Sleeps for 59 seconds before repeating the check.

    Returns:
        None
    """
    while True:
        try:
            current_time = datetime.now(UTC)
            for beacon in list(active_sessions.values()):  # Create a copy of values to avoid runtime modification issues
                if current_time > beacon.end_sharing:
                    # Use the existing stop_live_tracking function to properly clean up the session
                    await stop_live_tracking(beacon.user_id)
                    await telegram_app.bot.sendMessage(chat_id=beacon.user_id, text=
                        f"Live location sharing ended",
                        parse_mode='MarkdownV2'
                    )
                    app_logger.info(f"Automatically stopped expired beacon for user {beacon.user_id}")

        except Exception as ret_exc:
            app_logger.error(f"Cannot stop beaconing for user, error: {ret_exc}")
        finally:
            await asyncio.sleep(59)  # Using asyncio.sleep instead of time.sleep for async compatibility

# Start polling of the bot
def start_telegram_polling() -> None:
    """
    Starts the Telegram bot polling.

    This function initializes and starts the Telegram bot, setting up command and message handlers.
    It performs the following steps:
    1. Loads the bot token from environment variables.
    2. Builds the Telegram application.
    3. Creates command handlers for various bot commands.
    4. Adds handlers for location messages and updates.
    5. Starts polling the Telegram APIs.
    6. Initiates the task to stop old beacons.

    Returns:
        None
    """

    global app_logger
    global telegram_app

    app_logger.info("Loading token from environment and building application")
    telegram_app = ApplicationBuilder().token(load_bot_token()).build()

    app_logger.info("Creating command handlers")
    # User commands
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("setcall", cmd_setcall))
    telegram_app.add_handler(CommandHandler("setmsg", cmd_setmsg))
    telegram_app.add_handler(CommandHandler("setssid", cmd_setssid))
    telegram_app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    telegram_app.add_handler(CommandHandler("seticon", cmd_seticon))
    telegram_app.add_handler(CommandHandler("printcfg", cmd_printcfg))
    telegram_app.add_handler(CommandHandler("help", cmd_help))
    # Admin commands
    telegram_app.add_handler(CommandHandler("approve", cmd_approve))
    telegram_app.add_handler(CommandHandler("listusers", cmd_listusers))
    # Handle single location message
    telegram_app.add_handler(MessageHandler(filters.LOCATION, msg_location))
    # Add handler for location updates
    telegram_app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.LOCATION,
        lambda u, c: asyncio.create_task(update_live_location(u, c))
    ))

    # Start Telegram bot
    app_logger.info("Starting Telegram APIs polling")
    telegram_app.run_polling()
    # End live location sharing
    asyncio.create_task(stop_old_beacons())

# Escape all reserved characters
def escape_markdown_v2(text: str) -> str:
    """
    Escape special characters for Telegram MarkdownV2 format.
    Characters that need escaping: '_', '*', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!'
    
    Args:
        text (str): The text to escape
        
    Returns:
        str: The escaped text safe for MarkdownV2
    """
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    escaped_text = ''
    
    for char in text:
        if char in special_chars:
            escaped_text += f'\\{char}'
        else:
            escaped_text += char
            
    return escaped_text

# Send message to administrator
async def send_to_admin(message: str) -> None:
    """
    Sends a message to the administrator.

    This function sends a message to the admin's chat ID using the Telegram bot.

    Args:
        message (str): The message to send.

    Returns:
        None
    """
    global telegram_app
    try:
        await telegram_app.bot.sendMessage(chat_id=get_admin_id(), text=escape_markdown_v2(message), parse_mode='MarkdownV2')
    except Exception as ret_exc:
        app_logger.error(ret_exc)

# Send message to chat id
async def send_to_user(message: str, target: int) -> None:
    """
    Sends a message to a specific user.

    This function sends a message to a specified chat ID using the Telegram bot.

    Args:
        message (str): The message to send.
        target (int): The chat ID of the target user.

    Returns:
        None
    """
    global telegram_app
    try:
        await telegram_app.bot.sendMessage(chat_id=target, text=escape_markdown_v2(message), parse_mode='MarkdownV2')
    except Exception as ret_exc:
        app_logger.error(ret_exc)

if __name__ == "__main__":
    initialize_logger()
    connect_to_sqlite()
    start_telegram_polling()
