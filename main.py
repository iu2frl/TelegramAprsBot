"""
Python bot to operate APRS from Telegram
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
    user_id: int
    chat_id: int
    callsign: str
    ssid: str
    comment: str
    next_update: datetime
    end_sharing: datetime
    start_message: int

@dataclass
class AprsParameters:
    user_id: int
    user_callsign: str
    user_comment: str
    user_ssid: str
    user_symbol: str
    user_interval: int

# Global dictionary to store active live location sessions
active_sessions: Dict[int, LiveLocationSession] = {}

# Live location was sent
async def handle_live_location(update: Update, context: CallbackContext) -> None:
    """Handle incoming live location updates"""
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
        await telegram_app.bot.sendMessage(chat_id=user_id, text=
            f"Started live location tracking:\n\n" +
            f"Minimum update interval: `{db_interval}s`\n" +
            f"Sending beacons until: `{datetime_print(session.end_sharing)} UTC`\n" +
            f"Next update after: `{datetime_print(session.next_update)} UTC`",
            parse_mode='MarkdownV2'
        )
    else:
        app_logger.debug(f"Updating position for [{callsign}-{ssid}]")

    aprs_parameters = load_aprs_parameters_for_user(update.effective_user.id)
    if aprs_parameters is not None:
        send_position(aprs_parameters, update.effective_message.location.latitude, update.effective_message.location.longitude)

# Stop the process
async def stop_live_tracking(user_id: int) -> bool:
    """Stop live location tracking for a user"""
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
            "aprs_symbol TEXT DEFAULT \"$/\""
            ")")
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
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.info(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")
    if is_user_approved(update.effective_sender.id):
        # Check if the callsign was provided
        if len(update.effective_message.text.split(" ")) != 2:
            app_logger.warning(f"Invalid callsign received [{update.effective_message.text}] from [{update.effective_sender.id}]")
            await update.message.reply_text(f"Cannot detect callsign argument, syntax is: `/setcall AA0BBB`", parse_mode='MarkdownV2')
            return
        # Clean the string and check validity
        clean_message = str(update.effective_message.text).replace("/setcall ", "").strip().split(" ")[0].upper()
        if validate_callsign(clean_message):
            app_logger.info(f"User: [{update._effective_sender.username}] updated callsign to: [{clean_message}]")
            sqlite_cursor.execute("UPDATE users SET user_callsign = ? WHERE user_id = ? ", (clean_message, update.effective_sender.id))
            sqlite_connection.commit()
            await update.message.reply_text(f"Callsign was updated to `{clean_message}`", parse_mode='MarkdownV2')
        else:
            app_logger.warning(f"User: [{update._effective_sender.username}] tried to update callsign to: [{clean_message}] which is invalid")
            await update.message.reply_text(f"The requested callsign `{clean_message}` could not be recognized as valid callsign", parse_mode='MarkdownV2')
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Sets the message for the user
async def cmd_setmsg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.info(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
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
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.info(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
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

# Sets the update interval for the user
async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    global app_logger
    global sqlite_cursor
    global sqlite_connection
    # Register new users to the application
    app_logger.info(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
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
    global app_logger
    global sqlite_cursor
    global sqlite_connection

    # Register new users to the application
    app_logger.info(f"Entering method. Effective sender id: [{update.effective_sender.id}]")
    app_logger.debug(f"Message: [{update}]")

    if is_user_approved(update.effective_sender.id):
        try:
            result = load_aprs_parameters_for_user(update.effective_sender.id)
            await update.message.reply_text(
                "Current configuration:\n\n" +
                f"User ID: `{result.user_id}`\n" +
                f"Callsign: `{result.user_callsign}`\n" + 
                f"SSID: `{result.user_ssid}`\n" + 
                f"APRS callsign: `{result.user_callsign}-{result.user_ssid}`\n" +
                f"Comment: `{result.user_comment}`\n" +
                f"Symbol: `{result.user_symbol}`\n" +
                f"Beacon interval: `{result.user_interval}s`",
                parse_mode='MarkdownV2')
        except Exception as ret_exc:
            app_logger.error(ret_exc)
            await update.message.reply_text("Error while retrieving data from the database, please try again later")
    else:
        await update.message.reply_text(UNAUTHORIZED_MESSAGE)

# Parse the location and send it via APRS
async def msg_location(update: Update, context: CallbackContext) -> None:
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
                    r'Map link\: https\:\/\/aprs\.fi\/\#\!call\=a\%2F' + aprs_parameters.user_callsign + r'\-' + aprs_parameters.user_ssid + r'\&timerange\=3600\&tail\=3600',
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
def load_aprs_parameters_for_user(user_id: int) -> AprsParameters:
    try:
        line = sqlite_cursor.execute(
                "SELECT user_id, user_callsign, user_comment, user_ssid, aprs_symbol, aprs_interval FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
    except Exception as ret_exc:
        app_logger.error(f"Cannot load user data from database, error: {ret_exc}")
        return None

    if line is not None and len(line) == 6:
        return AprsParameters(
            user_id=line[0],
            user_callsign=line[1],
            user_comment=line[2],
            user_ssid=line[3],
            user_symbol=line[4],
            user_interval=line[5]
        )
    else:
        app_logger.warn("Database returned an unexpected result length for this query")
        return None

# Send the help message
async def cmd_help(update: Update, context: CallbackContext) -> None:
    await update.message.reply_text(
        r"Here are the instructions for the APRS bot, there are few simple steps to configure it" + "\n\n" +
        r"First you need to start the communication with the bot using the command `/start`, this will add you to the database\." + "\n" +
        r"The same `/start` command can also be used to check if your account was enabled by an administrator, this is a manual process and may take some time\." + "\n\n" +
        r"Once your account is enabled, you can start configure the APRS parameters as follows:" + "\n" +
        r"`/setcall AA0BBB` to set your callsign to AA0BBB" + "\n" +
        r"`/setssid 9` to set your APRS SSID to 9 \(default value for mobile stations\)" + "\n" +
        r"`/setinterval 120` to set the minimum beaconing interval to 120s" + "\n" +
        r"`/setmsg Hello` to set the APRS message to be sent" + "\n\n" +
        r"`/printcfg` can be used to validate the APRS parameters, make sure to use it before sending any position" + "\n\n" +
        r"Once everything is setup, you can just send your position and this will be sent to the APRS\-IS server"
    , parse_mode='MarkdownV2')

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
        return input_date.strftime(r"%d\/%m\/%Y %H\:%M\:%S")
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
        raise Exception("Cannot load BOT_TOKEN variable")

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

# Get the APRS IS address
def get_aprs_is() -> str:
    return os.environ.get("APRS_SERVER", "rotate.aprs2.net")

# Get the APRS port
def get_aprs_port() -> int:
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
def send_position(aprs_details: AprsParameters, latitude: float, longitude: float) -> None:
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
            'from': f"{aprs_details.user_callsign}-{aprs_details.user_ssid}",
            'to': 'APRS',
            'msg': f"@{timestamp}{aprs_lat}/{aprs_lon}{aprs_details.user_symbol}{aprs_details.user_comment}",
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
    print(aprs_packet)

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
            await send_to_user("Hurray! Your account was activated!", target_user)
        else:
            app_logger.info(f"User: [{target_user}] will be disapproved")
            sqlite_cursor.execute("UPDATE users SET approved = False WHERE user_id = ? ", (target_user,))
            sqlite_connection.commit()
            await update.message.reply_text(f"User `{target_user}` was disapproved", parse_mode='MarkdownV2')
    else:
        app_logger.warning(f"User [{update.effective_user.id}] is not an administrator")

# Get new location and update
async def update_live_location(update: Update, context: CallbackContext) -> None:
    """Update stored location for a user"""
    user_id = update.effective_user.id
    context.user_data[f"live_location_{user_id}"] = update.message.location

# Check if some beacons have to be removed
async def stop_old_beacons() -> None:
    """Stop all beacons older than the sharing time"""
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
    global app_logger
    global telegram_app
    app_logger.info("Loading token from environment and building application")
    telegram_app = ApplicationBuilder().token(load_bot_token()).build()
    app_logger.info("Creating command handlers")
    telegram_app.add_handler(CommandHandler("start", cmd_start))
    telegram_app.add_handler(CommandHandler("setcall", cmd_setcall))
    telegram_app.add_handler(CommandHandler("approve", cmd_approve))
    telegram_app.add_handler(CommandHandler("setmsg", cmd_setmsg))
    telegram_app.add_handler(CommandHandler("setssid", cmd_setssid))
    telegram_app.add_handler(CommandHandler("setinterval", cmd_setinterval))
    telegram_app.add_handler(CommandHandler("printcfg", cmd_printcfg))
    telegram_app.add_handler(CommandHandler("help", cmd_help))
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
    global telegram_app
    await telegram_app.bot.sendMessage(chat_id=get_admin_id(), text=escape_markdown_v2(message), parse_mode='MarkdownV2')

# Send message to chat id
async def send_to_user(message: str, target: int) -> None:
    global telegram_app
    await telegram_app.bot.sendMessage(chat_id=target, text=escape_markdown_v2(message), parse_mode='MarkdownV2')

if __name__ == "__main__":
    initialize_logger()
    connect_to_sqlite()
    start_telegram_polling()
