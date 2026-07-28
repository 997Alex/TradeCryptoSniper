import os
import time
import hashlib
from decimal import Decimal, ROUND_DOWN
from typing import Any


def env_or_config(config_value: str, env_key: str) -> str:
    return os.environ.get(env_key, config_value)


# def quantize_decimal(value: float | str, precision: int = 6) -> Decimal:
#     d = Decimal(str(value))
#     return d.quantize(Decimal(10) ** -precision, rounding=ROUND_DOWN)


def now_ts() -> int:
    return int(time.time())


# def short_id(obj: Any) -> str:
#     raw = str(obj)
#     return hashlib.md5(raw.encode()).hexdigest()[:8]
