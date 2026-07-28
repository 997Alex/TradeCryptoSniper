import logging
from pathlib import Path

import structlog


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    timestamper = structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            format="%(message)s",
            level=getattr(logging, level.upper(), logging.INFO),
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(),
            ],
        )
    else:
        logging.basicConfig(
            format="%(message)s",
            level=getattr(logging, level.upper(), logging.INFO),
            handlers=[logging.StreamHandler()],
        )

    structlog.configure(
        processors=processors + [structlog.dev.ConsoleRenderer()],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name or __name__)
