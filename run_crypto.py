from __future__ import annotations

import asyncio
import signal

from src.config import load_config
from src.crypto_bot import CryptoBot
from src.logger import setup_logging, get_logger


async def main():
    cfg = load_config("config.yaml")
    setup_logging(cfg.logging.level, cfg.logging.file)
    log = get_logger("main")

    bot = CryptoBot(cfg)
    loop = asyncio.get_running_loop()

    def request_stop(sig: signal.Signals) -> None:
        log.warning("shutdown_requested", signal=sig.name)
        bot.stop()
        # A second signal restores the default handler, so a wedged bot stays killable.
        loop.remove_signal_handler(sig)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop, sig)

    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
