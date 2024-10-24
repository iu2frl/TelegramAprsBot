"""
Python bot to operate APRS from Telegram
"""

import os
import logging
from logging.handlers import RotatingFileHandler
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Global logger object
log_handler = RotatingFileHandler("bot_output.log", 
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
logger = logging.getLogger(__name__)
logger.addHandler(log_handler)
logger.setLevel(logging.INFO)
# Reduce logging for http requests
logging.getLogger('httpx').setLevel(logging.WARNING)

# Starts a conversation with the user
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info(f"Entering method. Message: [{update}]")
    await update.message.reply_text(f'Welcome {update.effective_user.first_name},\nYou just got access to the IU2FRL APRS bot')

def load_bot_token() -> str:
    """
    Loads the BOT token from local environment

    Returns:
        str: Token of the bot
    """
    bot_token = os.environ.get("BOT_TOKEN", None)
    if bot_token is not None:
        return bot_token
    else:
        logger.error(f"Cannot load environment variables")
        Exception("Cannot load BOT_TOKEN variable")

if __name__ == "__main__":
    logger.info(f"Starting bot")
    app = ApplicationBuilder().token(load_bot_token()).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling()