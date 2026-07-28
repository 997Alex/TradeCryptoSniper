"""
from __future__ import annotations

import asyncio
import signal
import sys

from src.config import load_config
from src.bot import SniperBot
from utils.logger import setup_logging, get_logger


async def main():
    cfg = load_config("config.yaml")
    setup_logging(cfg.logging.level, cfg.logging.file)
    log = get_logger("main")

    if not cfg.paper_trading.enabled and not cfg.wallet.private_key:
        log.error("wallet_not_configured")
        sys.exit(1)
    if cfg.paper_trading.enabled:
        log.info("paper_mode", balance=f"${cfg.paper_trading.initial_balance_usd}")

    bot = SniperBot(cfg)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.stop)

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
"""
