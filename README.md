# APRS Telegram Bot

A Python bot that allows users to send their location to APRS (Automatic Packet Reporting System) through Telegram. Users can share their location (live location is supported too!) via Telegram, and the bot will automatically format and forward it to the APRS-IS network.

## Features

- User registration and approval system
- Configurable APRS parameters (callsign, SSID, comment)
- Real-time location sharing to APRS-IS
- Support for custom APRS-IS servers
- Logging system with rotation
- Admin commands for user management

## Prerequisites

- Python 3.x
- Telegram Bot Token (obtain from [@BotFather](https://t.me/botfather))
- APRS-IS account (optional, but recommended)

## Installation

1. Clone this repository:
```bash
git clone <repository-url>
cd TelegramAprsBot
```

2. Install required dependencies:
```bash
pip install -r ./requirements.txt
```

## Configuration

The bot uses environment variables for configuration. Create a `.env` file or set the following environment variables:

```bash
# Required
BOT_TOKEN="your_telegram_bot_token"
BOT_ADMIN="your_telegram_user_id"

# Optional
APRS_USER="your_aprs_callsign"    # Defaults to N0CALL (read-only mode)
APRS_SERVER="rotate.aprs2.net"    # APRS-IS server address
APRS_PORT="14580"                 # APRS-IS server port
```

## Usage

1. Start the bot:
```bash
python main.py
```

2. User Registration Process:
   - Users start the bot with `/start` command
   - Admin receives notification about new registration
   - Admin approves user with `/approve <user_id>` command
   - User receives confirmation of approval

3. Configure APRS Settings:
   - Set callsign: `/setcall AA0BBB`
   - Set SSID (default is 9): `/setssid 9`
   - Set message: `/setmsg Sent from Telegram APRS bot`
   - Set minimum interval: `/setinterval 30`
   - Set icon: `/seticon $/`
   - Verify settings: `/printcfg`

4. Send Location:
   - Use Telegram's location sharing feature
   - Bot will automatically format and send to APRS-IS

## Available Commands

- `/start` - Register or check registration status
- `/setcall <callsign>` - Set your amateur radio callsign
- `/setssid <ssid>` - Set your APRS SSID (default: 9)
- `/setmsg <message>` - Set your APRS comment/message
- `/setinterval <interval>` - Set the minimum delay between packets
- `/seticon <icon>` - Set your APRS icon
- `/printcfg` - Display current configuration
- `/help` - Show help message
- `/approve <user_id>` - (Admin only) Approve a user

## Directory Structure

```
TelegramAprsBot/
├── main.py
├── logs/           # Log files directory (created automatically)
│   └── bot_output.log
└── db/            # SQLite database directory (created automatically)
    └── database.sqlite
```

## Logging

The bot maintains rotating log files in the `logs` directory:
- Maximum file size: 5MB
- Keeps 2 backup files
- Logs both to file and console
- Includes timestamps, log levels, and function names

## Security Features

- User registration requires admin approval
- Admin commands are restricted to configured admin user ID
- SQLite database for persistent user data
- APRS-IS authentication with proper passcode generation

## Error Handling

- Validates callsign format
- Checks for valid SSID values
- Verifies user approval status before operations
- Handles APRS-IS connection issues
- Prevents concurrent APRS socket operations

## Running on Docker

An image is built and published at every release with name `iu2frl/telegram-aprs`. The following docker-compose can be used to run it:

```yaml
services:
  telegram-aprs:
    container_name: telegram-aprs
    image: iu2frl/telegram-aprs
    environment:
      - "BOT_TOKEN=00000:AAAAAAAAAAAAAAAA"
      - "BOT_ADMIN=000000"
      - "APRS_USER=MY0CALL"
    restart: unless-stopped
    volumes:
      - database:/home/frlbot/db
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 100M
volumes:
  database:
```

## Contributing

Feel free to submit issues, fork the repository, and create pull requests for any improvements.

## License

Released with GNU GPL V3 License, see [LICENSE](./LICENSE)

## Acknowledgments

- Uses [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) for Telegram integration
- Uses [aprslib](https://github.com/rossengeorgiev/aprs-python) for APRS-IS connectivity
