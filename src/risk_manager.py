"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

import httpx

from src.config import ResolutionArbConfig, TradingConfig
from utils.logger import get_logger

log = get_logger("risk")

CENTS = Decimal("100")


class RiskManager:
    ...
"""
