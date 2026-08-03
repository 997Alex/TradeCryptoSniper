from __future__ import annotations

import asyncio
import os
import signal
import sys

from src.config import load_config
from src.crypto_bot import CryptoBot
from utils.helpers import load_env
from utils.logger import setup_logging


async def main():
    load_env()
    cfg = load_config(os.environ.get("CRYPTOSNIPER_CONFIG", "config_conservativa_100wr.yaml"))
    setup_logging(cfg.logging.level, "logs/crypto_bot.log")

    # `live` and `--arm` are gates 1 and 2. Absent -> paper, which is the default
    # the systemd unit ships with.
    mode = "live" if "live" in sys.argv[1:] else "paper"
    bot = CryptoBot(cfg, mode=mode, arm_flag="--arm" in sys.argv[1:])

    loop = asyncio.get_running_loop()
    # SIGTERM as well as SIGINT: `systemctl stop` sends SIGTERM, and without a
    # handler it hard-kills mid-window, dropping open positions and leaving the
    # persisted balance debited for trades that never resolve.
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.stop)

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
