from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import httpx

from src.order_executor import OrderResult
from src.config import PaperTradingConfig
from utils.helpers import now_ts
from utils.logger import get_logger

log = get_logger("paper")

PRICE_BUCKETS = [(0, 77), (78, 89), (90, 94), (95, 99), (100, 100)]
BUCKET_LABELS = ["<78¢", "78-89¢", "90-94¢", "95-99¢", "100¢"]


@dataclass
class PaperPosition:
    market_id: str
    token_id: str
    question: str
    side: str
    size: Decimal
    entry_price_cents: Decimal
    cost_cents: Decimal
    entry_timestamp: float = field(default_factory=time.time)
    resolved: bool = False
    won: bool | None = None
    resolve_timestamp: float | None = None
    payout_cents: Decimal = Decimal("0")


class PaperTrader:
    def __init__(self, cfg: PaperTradingConfig, gamma_base: str, slippage_pct: float = 0.0, stats_path: str = "bucket_stats.json"):
        self._cfg = cfg
        self._gamma_base = gamma_base.rstrip("/")
        self._http = httpx.AsyncClient(base_url=self._gamma_base, timeout=15)
        self._slippage_pct = Decimal(str(slippage_pct))
        self._lock = asyncio.Lock()
        self._stats_path = stats_path

        initial = Decimal(str(cfg.initial_balance_usd)) * Decimal("100")
        self._balance_cents: Decimal = initial.quantize(Decimal("1"), rounding=ROUND_DOWN)
        self._initial_balance_cents: Decimal = self._balance_cents
        self._open_positions: dict[str, PaperPosition] = {}
        self._resolved_positions: list[PaperPosition] = []
        self._bucket_stats: dict[str, dict[str, int | Decimal]] = defaultdict(
            lambda: {"trades": 0, "wins": 0, "losses": 0, "total_pnl_cents": Decimal("0")}
        )
        self._load_stats()

    def _load_stats(self) -> None:
        try:
            raw = json.loads(Path(self._stats_path).read_text())
            for bucket, data in raw.items():
                stats = self._bucket_stats[bucket]
                stats["trades"] = data["trades"]
                stats["wins"] = data["wins"]
                stats["losses"] = data["losses"]
                stats["total_pnl_cents"] = Decimal(str(data["total_pnl_cents"]))
            log.info("stats_loaded", path=self._stats_path, buckets=len(raw))
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass

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
            Path(self._stats_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self._stats_path).write_text(json.dumps(raw, indent=2))
        except Exception as exc:
            log.warning("stats_save_failed", error=str(exc))

    @staticmethod
    def _price_bucket(price_cents: int) -> str:
        for (lo, hi), label in zip(PRICE_BUCKETS, BUCKET_LABELS):
            if lo <= price_cents <= hi:
                return label
        return "???"

    @property
    def balance_usd(self) -> Decimal:
        return (self._balance_cents / Decimal("100")).quantize(Decimal("0.01"))

    @property
    def total_open_positions(self) -> int:
        return len(self._open_positions)

    @property
    def equity_cents(self) -> Decimal:
        return self._balance_cents + sum(
            pos.cost_cents for pos in self._open_positions.values()
            if not pos.resolved
        )

    async def execute_fok(
        self,
        token_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        market: dict | None = None,
        slippage_pct: Decimal | None = None,
    ) -> OrderResult:
        return await self._paper_execute(token_id, side, size, price, market, "FOK", slippage_pct=slippage_pct)

    # async def execute_ioc(
    #     self,
    #     token_id: str,
    #     side: str,
    #     size: Decimal,
    #     price: Decimal,
    #     market: dict | None = None,
    # ) -> OrderResult:
    #     return await self._paper_execute(token_id, side, size, price, market, "IOC")

    async def _paper_execute(
        self,
        token_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        market: dict | None,
        order_type: str,
        slippage_pct: Decimal | None = None,
    ) -> OrderResult:
        base_price_cents = (price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN)

        sp = slippage_pct if slippage_pct is not None else self._slippage_pct
        slippage_factor = Decimal("1") + sp / Decimal("100")
        effective_price = price * slippage_factor
        effective_price_cents = (effective_price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN)
        effective_size = (size * Decimal("1")).quantize(Decimal("0.0001"))
        cost_cents = (effective_price_cents * effective_size).quantize(Decimal("1"), rounding=ROUND_DOWN)

        market_id = str((market or {}).get("id", "?"))
        question = (market or {}).get("question") or (market or {}).get("title", "?")

        async with self._lock:
            if cost_cents > self._balance_cents:
                effective_size = self._balance_cents // effective_price_cents
                if effective_size <= Decimal("0"):
                    log.warning(f"  insufficient paper balance: need ${cost_cents/Decimal('100'):.2f}, have ${self.balance_usd:.2f}")
                    return OrderResult(
                        status="rejected",
                        error=f"insufficient paper balance: need ${cost_cents/Decimal('100'):.2f}, have ${self.balance_usd:.2f}",
                    )
                cost_cents = (effective_price_cents * effective_size).quantize(Decimal("1"), rounding=ROUND_DOWN)

            self._balance_cents -= cost_cents

            pos = PaperPosition(
                market_id=market_id,
                token_id=token_id,
                question=question,
                side=side,
                size=effective_size,
                entry_price_cents=effective_price_cents,
                cost_cents=cost_cents,
            )
            self._open_positions[token_id] = pos

        return OrderResult(
            status="filled",
            filled_size=effective_size,
            target_size=size,
            avg_price=effective_price,
            trade_id=f"paper_{token_id}_{now_ts()}",
        )

    async def resolve_position(self, token_id: str, won: bool):
        async with self._lock:
            pos = self._open_positions.get(token_id)
            if pos is None:
                log.warning("resolve_position_not_found", token_id=token_id)
                return

            pos.won = won
            pos.resolved = True
            pos.resolve_timestamp = time.time()

            if won:
                payout = Decimal("100") * pos.size
                pos.payout_cents = payout.quantize(Decimal("1"), rounding=ROUND_DOWN)
                self._balance_cents += pos.payout_cents

            entry_cents = int(pos.entry_price_cents)
            bucket = self._price_bucket(entry_cents)
            stats = self._bucket_stats[bucket]
            stats["trades"] += 1
            pnl = pos.payout_cents - pos.cost_cents
            stats["total_pnl_cents"] += pnl
            if won:
                stats["wins"] += 1
            else:
                stats["losses"] += 1

            self._resolved_positions.append(pos)
            del self._open_positions[token_id]
        self._save_stats()
        self._log_portfolio()

    async def check_resolutions(self):
        if not self._open_positions:
            return

        resolved_any = False
        for token_id, pos in list(self._open_positions.items()):
            if pos.resolved:
                continue
            try:
                outcome = await self._fetch_resolution(token_id, pos.market_id)
                if outcome is None:
                    await asyncio.sleep(0.3)
                    continue
                await self.resolve_position(token_id, outcome)
                resolved_any = True
            except Exception as exc:
                log.warning("resolution_check_failed", token_id=token_id, error=str(exc))
            await asyncio.sleep(0.3)

        if resolved_any:
            self._log_portfolio()

    async def _fetch_resolution(self, token_id: str, market_id: str) -> bool | None:
        try:
            resp = await self._http.get(f"/markets/{market_id}", timeout=10)
            if resp.status_code != 200:
                log.warning("fetch_resolution_http_error", market_id=market_id, status=resp.status_code)
                return None
            data = resp.json()

            closed = data.get("closed", False)
            if not closed:
                return None

            raw_prices = data.get("outcomePrices")
            if not raw_prices:
                return None

            prices: list[str] = []
            if isinstance(raw_prices, str):
                try:
                    prices = json.loads(raw_prices)
                except (json.JSONDecodeError, TypeError):
                    return None
            elif isinstance(raw_prices, (list, tuple)):
                prices = [str(p) for p in raw_prices]

            if len(prices) < 2:
                return None

            dec0 = Decimal(prices[0])
            dec1 = Decimal(prices[1])

            raw_ids = data.get("clobTokenIds")
            ids: list[str] = []
            if isinstance(raw_ids, list):
                ids = [str(x) for x in raw_ids]
            elif isinstance(raw_ids, str):
                try:
                    ids = json.loads(raw_ids)
                except (json.JSONDecodeError, TypeError):
                    ids = [raw_ids]

            if dec0 >= Decimal("0.999") and dec1 <= Decimal("0.001"):
                winner = ids[0] if len(ids) >= 2 else None
            elif dec1 >= Decimal("0.999") and dec0 <= Decimal("0.001"):
                winner = ids[1] if len(ids) >= 2 else None
            else:
                return None

            return winner == token_id if winner is not None else None

        except Exception as exc:
            log.warning("fetch_resolution_error", market_id=market_id, error=str(exc))
            return None

    # def allocate_for_trade(self, price: Decimal) -> tuple[Decimal, Decimal]:
    #     max_concurrent = self._cfg.max_concurrent_trades
    #     open_count = self.total_open_positions
    #     available_slots = max_concurrent - open_count
    #     if available_slots <= 0:
    #         return Decimal("0"), Decimal("0")
    #     alloc_cents = self._balance_cents // Decimal(str(available_slots))
    #     price_cents = (price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_DOWN)
    #     if price_cents <= 0:
    #         return Decimal("0"), Decimal("0")
    #     size = (alloc_cents / price_cents).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    #     log.info(
    #         "paper_allocate",
    #         cash_balance=str(self.balance_usd),
    #         open_positions=open_count,
    #         available_slots=available_slots,
    #         alloc=f"${alloc_cents/Decimal('100'):.2f}",
    #         size=str(size),
    #     )
    #     return alloc_cents, size

    def summary(self) -> dict[str, Any]:
        total_invested = sum(p.cost_cents for p in self._resolved_positions)
        total_payouts = sum(p.payout_cents for p in self._resolved_positions)
        total_open_cost = sum(p.cost_cents for p in self._open_positions.values())

        wins = sum(1 for p in self._resolved_positions if p.won)
        losses = sum(1 for p in self._resolved_positions if p.won is False)

        return {
            "initial_balance": str(self._initial_balance_cents / Decimal("100")),
            "cash_balance": str(self.balance_usd),
            "open_positions": self.total_open_positions,
            "resolved_trades": len(self._resolved_positions),
            "wins": wins,
            "losses": losses,
            "total_invested": str(total_invested / Decimal("100")),
            "total_payouts": str(total_payouts / Decimal("100")),
            "open_exposure": str(total_open_cost / Decimal("100")),
            "equity": str((self._balance_cents + total_open_cost) / Decimal("100")),
        }

    # def _log_portfolio(self):
    #     s = self.summary()

    def bucket_win_rate(self, price_cents: int, default: float = 0.60) -> float:
        bucket = self._price_bucket(price_cents)
        stats = self._bucket_stats.get(bucket)
        if stats is None or stats["trades"] == 0:
            return default
        wins = int(stats["wins"])
        total = int(stats["trades"])
        return wins / total if total > 0 else default

    def bucket_trade_count(self, price_cents: int) -> int:
        bucket = self._price_bucket(price_cents)
        stats = self._bucket_stats.get(bucket)
        return int(stats["trades"]) if stats else 0

    # def bucket_breakdown_str(self) -> str:
    #     lines = []
    #     for label in BUCKET_LABELS:
    #         s = self._bucket_stats.get(label)
    #         if s and int(s["trades"]) > 0:
    #             t = int(s["trades"])
    #             w = int(s["wins"])
    #             l_ = int(s["losses"])
    #             wr = f"{w/t*100:.0f}%" if t > 0 else "N/A"
    #             avg_pnl = float(s["total_pnl_cents"]) / t / 100 if t > 0 else 0
    #             lines.append(f"{label}: {t} trades, {w}W/{l_}L, wr={wr}, avg_pnl=${avg_pnl:.2f}")
    #     return " | ".join(lines) if lines else "no data"

    async def close(self):
        if self._open_positions:
            log.warning(
                "paper_open_positions_dropped",
                count=len(self._open_positions),
            )
        self._save_stats()
        await self._http.aclose()
