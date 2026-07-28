from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from src.api import ApiClient, as_str_list
from src.logger import get_logger

log = get_logger("paper")

PRICE_BUCKETS = [(0, 77), (78, 89), (90, 94), (95, 99), (100, 100)]
BUCKET_LABELS = ["<78¢", "78-89¢", "90-94¢", "95-99¢", "100¢"]

# A binary contract pays exactly 100¢. Filling at or above par is a guaranteed
# loss regardless of outcome, so fills are clamped below it.
MAX_FILL_CENTS = Decimal("99")


@dataclass
class OrderResult:
    status: str
    filled_size: Decimal = Decimal("0")
    target_size: Decimal = Decimal("0")
    avg_price_cents: Decimal = Decimal("0")
    cost_cents: Decimal = Decimal("0")
    trade_id: str | None = None
    error: str | None = None


@dataclass
class PaperPosition:
    market_id: str
    token_id: str
    question: str
    side: str
    size: Decimal
    # The raw market quote the sizing decision was made on. Bucket statistics are
    # keyed on this so that what is read at entry matches what is written at
    # resolution — the fill price below has slippage baked in and would land in a
    # different bucket.
    quote_price_cents: int
    entry_price_cents: Decimal
    cost_cents: Decimal
    entry_timestamp: float = field(default_factory=time.time)
    resolved: bool = False
    won: bool | None = None
    # Written off because the market never resolved; the cash is gone, but the
    # outcome says nothing about the signal, so it is kept out of bucket stats.
    abandoned: bool = False
    resolve_timestamp: float | None = None
    payout_cents: Decimal = Decimal("0")


class PaperTrader:
    def __init__(self, gamma: ApiClient, initial_balance_usd: float, stats_path: str = "data/bucket_stats.json"):
        self._gamma = gamma
        self._lock = asyncio.Lock()
        self._stats_path = stats_path

        initial = Decimal(str(initial_balance_usd)) * Decimal("100")
        self._balance_cents: Decimal = initial.quantize(Decimal("1"), rounding=ROUND_DOWN)
        self._initial_balance_cents: Decimal = self._balance_cents
        self._open_positions: dict[str, PaperPosition] = {}
        self._resolved_positions: list[PaperPosition] = []
        self._bucket_stats: dict[str, dict[str, int | Decimal]] = defaultdict(
            lambda: {"trades": 0, "wins": 0, "losses": 0, "total_pnl_cents": Decimal("0")}
        )
        self._load_stats()

    # ── persistence ─────────────────────────────────────────────

    def _load_stats(self) -> None:
        path = Path(self._stats_path)
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            for bucket, data in raw.items():
                stats = self._bucket_stats[bucket]
                stats["trades"] = int(data["trades"])
                stats["wins"] = int(data["wins"])
                stats["losses"] = int(data["losses"])
                stats["total_pnl_cents"] = Decimal(str(data["total_pnl_cents"]))
            log.info("stats_loaded", path=self._stats_path, buckets=len(raw))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            log.warning("stats_load_failed", path=self._stats_path, error=str(exc))

    def _save_stats(self) -> None:
        raw: dict[str, dict[str, float | int]] = {}
        for bucket, data in self._bucket_stats.items():
            if int(data["trades"]) > 0:
                raw[bucket] = {
                    "trades": int(data["trades"]),
                    "wins": int(data["wins"]),
                    "losses": int(data["losses"]),
                    "total_pnl_cents": float(data["total_pnl_cents"]),
                }
        try:
            path = Path(self._stats_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(json.dumps(raw, indent=2))
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("stats_save_failed", error=str(exc))

    @staticmethod
    def _price_bucket(price_cents: int) -> str:
        clamped = max(0, min(100, int(price_cents)))
        for (lo, hi), label in zip(PRICE_BUCKETS, BUCKET_LABELS):
            if lo <= clamped <= hi:
                return label
        return BUCKET_LABELS[-1]

    # ── state ───────────────────────────────────────────────────

    @property
    def balance_usd(self) -> Decimal:
        return (self._balance_cents / Decimal("100")).quantize(Decimal("0.01"))

    @property
    def total_open_positions(self) -> int:
        return len(self._open_positions)

    @property
    def equity_cents(self) -> Decimal:
        return self._balance_cents + sum(
            (pos.cost_cents for pos in self._open_positions.values() if not pos.resolved),
            Decimal("0"),
        )

    @property
    def resolved_positions(self) -> list[PaperPosition]:
        return self._resolved_positions

    # ── execution ───────────────────────────────────────────────

    async def execute_fok(
        self,
        token_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        quote_price_cents: int,
        market: dict | None = None,
        slippage_pct: Decimal = Decimal("0"),
    ) -> OrderResult:
        effective_price_cents = self.fill_price_cents(price, slippage_pct)
        size = size.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        cost_cents = (effective_price_cents * size).quantize(Decimal("1"), rounding=ROUND_DOWN)

        if effective_price_cents <= 0 or size <= 0 or cost_cents <= 0:
            return OrderResult(status="rejected", target_size=size, error="non-positive order")

        market_id = str((market or {}).get("id", "?"))
        question = (market or {}).get("question") or (market or {}).get("title", "?")

        async with self._lock:
            # Sizing upstream already respects the cash balance; a shortfall here
            # means the risk limits were computed against stale state, so reject
            # rather than silently shrinking the order to whatever cash is left.
            if cost_cents > self._balance_cents:
                return OrderResult(
                    status="rejected",
                    target_size=size,
                    error=f"insufficient paper balance: need ${cost_cents / Decimal('100'):.2f}, have ${self.balance_usd:.2f}",
                )

            self._balance_cents -= cost_cents
            self._open_positions[token_id] = PaperPosition(
                market_id=market_id,
                token_id=token_id,
                question=question,
                side=side,
                size=size,
                quote_price_cents=quote_price_cents,
                entry_price_cents=effective_price_cents,
                cost_cents=cost_cents,
            )

        return OrderResult(
            status="filled",
            filled_size=size,
            target_size=size,
            avg_price_cents=effective_price_cents,
            cost_cents=cost_cents,
            trade_id=f"paper_{token_id}_{int(time.time())}",
        )

    @staticmethod
    def raw_fill_price_cents(price: Decimal, slippage_pct: Decimal) -> Decimal:
        """Price after slippage, in whole cents, before the par clamp. Callers use
        this to decide whether a trade is worth taking at all."""
        effective = price * (Decimal("1") + slippage_pct / Decimal("100")) * Decimal("100")
        return effective.quantize(Decimal("1"), rounding=ROUND_DOWN)

    @staticmethod
    def fill_price_cents(price: Decimal, slippage_pct: Decimal) -> Decimal:
        """Price actually paid. Clamped below par as a backstop — CryptoBot already
        rejects these entries, so on the live path the clamp never binds."""
        return min(PaperTrader.raw_fill_price_cents(price, slippage_pct), MAX_FILL_CENTS)

    # ── resolution ──────────────────────────────────────────────

    async def resolve_position(self, token_id: str, won: bool, abandoned: bool = False) -> PaperPosition | None:
        async with self._lock:
            pos = self._open_positions.pop(token_id, None)
            if pos is None:
                return None

            pos.won = won
            pos.resolved = True
            pos.abandoned = abandoned
            pos.resolve_timestamp = time.time()

            if won:
                pos.payout_cents = (Decimal("100") * pos.size).quantize(Decimal("1"), rounding=ROUND_DOWN)
                self._balance_cents += pos.payout_cents

            if not abandoned:
                stats = self._bucket_stats[self._price_bucket(pos.quote_price_cents)]
                stats["trades"] += 1
                stats["total_pnl_cents"] += pos.payout_cents - pos.cost_cents
                if won:
                    stats["wins"] += 1
                else:
                    stats["losses"] += 1

            self._resolved_positions.append(pos)

        if not abandoned:
            self._save_stats()
        return pos

    async def check_resolutions(self, max_age_seconds: int | None = None) -> None:
        now = time.time()
        for token_id, pos in list(self._open_positions.items()):
            if pos.resolved:
                continue
            outcome = await self._fetch_resolution(token_id, pos.market_id)
            if outcome is not None:
                await self.resolve_position(token_id, outcome)
            elif max_age_seconds is not None and now - pos.entry_timestamp > max_age_seconds:
                log.warning(
                    "position_written_off",
                    token_id=token_id,
                    age_s=int(now - pos.entry_timestamp),
                    cost=f"${pos.cost_cents / Decimal('100'):.2f}",
                )
                await self.resolve_position(token_id, won=False, abandoned=True)

    async def _fetch_resolution(self, token_id: str, market_id: str) -> bool | None:
        data = await self._gamma.get_json(f"/markets/{market_id}")
        if not isinstance(data, dict) or not data.get("closed", False):
            return None

        prices = as_str_list(data.get("outcomePrices"))
        ids = as_str_list(data.get("clobTokenIds"))
        if len(prices) < 2 or len(ids) < 2:
            return None

        try:
            dec0, dec1 = Decimal(prices[0]), Decimal(prices[1])
        except (ArithmeticError, ValueError):
            return None

        if dec0 >= Decimal("0.999") and dec1 <= Decimal("0.001"):
            winner = ids[0]
        elif dec1 >= Decimal("0.999") and dec0 <= Decimal("0.001"):
            winner = ids[1]
        else:
            return None

        return winner == token_id

    # ── stats lookups ───────────────────────────────────────────

    def bucket_win_rate(self, price_cents: int, default: float = 0.60) -> float:
        stats = self._bucket_stats.get(self._price_bucket(price_cents))
        if not stats or int(stats["trades"]) == 0:
            return default
        return int(stats["wins"]) / int(stats["trades"])

    def bucket_trade_count(self, price_cents: int) -> int:
        stats = self._bucket_stats.get(self._price_bucket(price_cents))
        return int(stats["trades"]) if stats else 0

    def summary(self) -> dict[str, Any]:
        zero = Decimal("0")
        total_invested = sum((p.cost_cents for p in self._resolved_positions), zero)
        total_payouts = sum((p.payout_cents for p in self._resolved_positions), zero)
        total_open_cost = sum((p.cost_cents for p in self._open_positions.values()), zero)

        return {
            "initial_balance": str(self._initial_balance_cents / Decimal("100")),
            "cash_balance": str(self.balance_usd),
            "open_positions": self.total_open_positions,
            "resolved_trades": len(self._resolved_positions),
            "wins": sum(1 for p in self._resolved_positions if p.won),
            "losses": sum(1 for p in self._resolved_positions if p.won is False),
            "total_invested": str(total_invested / Decimal("100")),
            "total_payouts": str(total_payouts / Decimal("100")),
            "open_exposure": str(total_open_cost / Decimal("100")),
            "equity": str(self.equity_cents / Decimal("100")),
        }

    async def close(self) -> None:
        if self._open_positions:
            log.warning("paper_open_positions_dropped", count=len(self._open_positions))
        self._save_stats()
