from __future__ import annotations

import asyncio
import signal
import sys

from src.config import load_config
from src.crypto_bot import CryptoBot
from utils.logger import setup_logging


async def main():
    cfg = load_config("config.yaml")
    setup_logging(cfg.logging.level, "logs/crypto_bot.log")

    bot = CryptoBot(cfg)

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, bot.stop)

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
