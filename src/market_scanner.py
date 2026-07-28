"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

import httpx

from src.config import ResolutionArbConfig, ScannerConfig
from utils.helpers import now_ts
from utils.logger import get_logger

log = get_logger("scanner")


class QualifiedMarket:
    ...


class MarketScanner:
    ...
"""
