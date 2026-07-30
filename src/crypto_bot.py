from __future__ import annotations

import asyncio
import json
import os
import time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx

from src.paper_trader import PaperTrader
from src.config import Config, PaperTradingConfig
from utils.logger import get_logger

log = get_logger("crypto_bot")

CRYPTO_COINS = ["btc", "eth", "sol", "xrp", "doge"]
WINDOW_SECONDS = 300
HOUR_SECONDS = 3600

SEP = "=" * 68
SUB_SEP = "-" * 68

COIN_LIQUIDITY_RANK = {
    "btc": 1.0, "eth": 0.9, "sol": 0.7, "xrp": 0.5, "doge": 0.4,
}


def _window_label(ts: int) -> str:
    t = time.gmtime(ts)
    return f"{t.tm_hour:02d}:{t.tm_min:02d}"


class CryptoBot:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._gamma_base = cfg.polymarket.gamma_api_base.rstrip("/")
        self._round = 0
        self._round_trades: list[dict] = []

        self._http = httpx.AsyncClient(base_url=self._gamma_base, timeout=15)

        c5 = cfg.crypto_5m
        paper_cfg = PaperTradingConfig(
            enabled=True,
            initial_balance_usd=cfg.paper_trading.initial_balance_usd,
            max_concurrent_trades=10,
            resolve_check_interval_sec=5,
        )
        self._paper = PaperTrader(paper_cfg, self._gamma_base, slippage_pct=c5.slippage_model_pct, stats_path="data/bucket_stats.json")

        self._running = False
        self._last_window_ts: int | None = None

        self._session_start_balance: Decimal = Decimal("0")
        self._session_start_ts: float = 0.0
        self._consecutive_losses: int = 0
        self._consecutive_wins: int = 0
        self._loss_streak_until: float = 0.0
        self._post_loss_reduction: Decimal = Decimal("1")
        self._position_cost_this_round: Decimal = Decimal("0")
        self._last_fetch_time: float = 0.0
        self._price_streak: dict[str, int] = {}
        self._circuit_breaker_reason: str | None = None
        self._resolved_count: int = 0
        self._last_resolve_log: dict[str, Any] = {}
        self._trade_by_token: dict[str, dict] = {}

        # real-time breaker flag: flipped by the resolution monitor the instant a
        # circuit breaker condition is met, instead of only being checked once per
        # 5-minute round in run(). The entry loop in _process_window polls this so
        # a drawdown/loss-streak trip mid-round stops new entries immediately.
        self._halt_new_entries: bool = False

        # hourly PnL tracking, so "guadagno medio orario" is a number you can
        # actually see in the log instead of inferring from scattered round
        # summaries.
        self._hour_start_ts: float = 0.0
        self._hour_start_equity_cents: Decimal = Decimal("0")

    # ── entry points ────────────────────────────────────────────

    async def run(self):
        self._running = True
        self._session_start_balance = self._paper.balance_usd
        self._session_start_ts = time.time()
        self._hour_start_ts = self._session_start_ts
        self._hour_start_equity_cents = self._paper.equity_cents
        log.info("crypto_bot_started")

        monitor_task = asyncio.create_task(self._resolution_monitor())

        try:
            while self._running:
                now_ts_int = int(time.time())

                if now_ts_int - self._hour_start_ts >= HOUR_SECONDS:
                    self._log_hourly_pnl(now_ts_int)

                window_ts = (now_ts_int // WINDOW_SECONDS) * WINDOW_SECONDS

                if window_ts == self._last_window_ts:
                    await asyncio.sleep(2)
                    continue

                if self._check_circuit_breakers():
                    log.warning(f"  circuit breaker: {self._circuit_breaker_reason}")
                    await asyncio.sleep(30)
                    continue

                if self._check_kill_switch():
                    log.warning("  kill switch active (lock file found)")
                    await asyncio.sleep(30)
                    continue

                self._round += 1
                self._round_trades = []
                self._trade_by_token = {}
                self._position_cost_this_round = Decimal("0")
                await self._process_window(window_ts)
        except asyncio.CancelledError:
            pass
        except BaseException as exc:
            log.critical("run_loop_exception", error=str(exc), exc_info=True)
            raise
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
            await self._shutdown()

    def _log_hourly_pnl(self, now_ts_int: int) -> None:
        equity_now = self._paper.equity_cents
        pnl_cents = equity_now - self._hour_start_equity_cents
        pnl_usd = pnl_cents / Decimal("100")
        pnl_pct = (
            float(pnl_cents / self._hour_start_equity_cents * Decimal("100"))
            if self._hour_start_equity_cents > 0
            else 0.0
        )
        log.info(
            f"{SUB_SEP}\n"
            f"  HOURLY PNL: ${pnl_usd:.2f} ({pnl_pct:+.2f}%)  |  "
            f"equity: ${equity_now / Decimal('100'):.2f}  |  "
            f"open_exposure: ${self._paper.open_exposure_cents / Decimal('100'):.2f}\n"
            f"{SUB_SEP}"
        )
        self._hour_start_ts = now_ts_int
        self._hour_start_equity_cents = equity_now

    # ── circuit breakers ────────────────────────────────────────

    def _check_circuit_breakers(self) -> bool:
        c = self._cfg.crypto_5m

        if self._loss_streak_until > time.time():
            remaining = int(self._loss_streak_until - time.time())
            self._circuit_breaker_reason = f"loss_cooldown_{remaining}s"
            return True

        # Drawdown is measured against CASH, not "equity" (cash + cost basis of
        # still-open positions). The old formula used max(cash, equity), which
        # -- since equity = cash + open cost basis >= cash -- always picked
        # equity and so treated every open, unresolved bet as if it were already
        # a guaranteed win. That's precisely why the breaker in the log never
        # fired until AFTER a cluster of correlated positions had already
        # resolved as losses (19.5% overshoot vs the 15% target): the real risk
        # sitting in open positions was invisible to the check until it was too
        # late. Using cash alone assumes the conservative case (open bets could
        # still lose) and reacts before the damage lands, not after.
        cash = self._paper.balance_usd
        equity_usd = self._paper.equity_cents / Decimal("100")
        if self._session_start_balance > Decimal("0"):
            dd_pct = float((self._session_start_balance - cash) / self._session_start_balance * Decimal("100"))
            if dd_pct >= c.max_daily_drawdown_pct:
                self._circuit_breaker_reason = (
                    f"drawdown_{dd_pct:.1f}%_>={c.max_daily_drawdown_pct}%_"
                    f"(cash=${cash:.2f}_equity=${equity_usd:.2f})"
                )
                return True

        if self._consecutive_losses >= c.max_consecutive_losses:
            self._circuit_breaker_reason = (
                f"loss_streak_{self._consecutive_losses}_>={c.max_consecutive_losses}"
            )
            return True

        self._circuit_breaker_reason = None
        return False

    async def _resolution_monitor(self):
        check_interval = 3
        open_before = 0
        while self._running:
            try:
                prev_count = len(self._paper._resolved_positions)
                open_now = self._paper.total_open_positions
                if open_now != open_before:
                    log.info(f"  open positions: {open_now} | resolved: {prev_count}")
                    open_before = open_now
                await self._paper.check_resolutions()
                new_count = len(self._paper._resolved_positions)

                for i in range(prev_count, new_count):
                    pos = self._paper._resolved_positions[i]
                    coin_str = pos.question.split(" - ")[0].replace(" Up or Down", "")
                    pnl = pos.payout_cents - pos.cost_cents
                    trade = self._trade_by_token.get(pos.token_id)
                    if trade is not None:
                        trade["won"] = pos.won
                        trade["profit"] = pnl
                    if pos.won:
                        self._consecutive_wins += 1
                        self._consecutive_losses = 0
                        if self._consecutive_wins >= self._cfg.crypto_5m.win_streak_restore:
                            self._post_loss_reduction = Decimal("1")
                        log.info(f"  ✓ {coin_str} {pos.side} WON | pnl=+${pnl/Decimal('100'):.2f} | equity=${self._paper.equity_cents / Decimal('100'):.2f} | cash=${self._paper.balance_usd:.2f}")
                    else:
                        self._consecutive_losses += 1
                        self._consecutive_wins = 0
                        self._post_loss_reduction = Decimal(str(self._cfg.crypto_5m.loss_size_multiplier))
                        log.info(f"  ✗ {coin_str} {pos.side} LOST | pnl=${pnl/Decimal('100'):.2f} | equity=${self._paper.equity_cents / Decimal('100'):.2f} | cash=${self._paper.balance_usd:.2f}")
                        if self._consecutive_losses >= self._cfg.crypto_5m.max_consecutive_losses:
                            self._loss_streak_until = time.time() + self._cfg.crypto_5m.loss_cooldown_seconds
                            log.warning(f"  loss streak {self._consecutive_losses}, cooldown {self._cfg.crypto_5m.loss_cooldown_seconds}s")

                # Re-evaluate breakers immediately after processing resolutions
                # (not just once per 5-minute round loop in run()) and latch the
                # result so an in-progress round's entry loop can see it and stop
                # opening new positions right away instead of finishing the round
                # first.
                halted = self._check_circuit_breakers()
                if halted and not self._halt_new_entries:
                    log.warning(f"  circuit breaker tripped mid-session: {self._circuit_breaker_reason}")
                self._halt_new_entries = halted
            except Exception as exc:
                log.warning("resolution_monitor_error", error=str(exc))
            await asyncio.sleep(check_interval)

    def _check_kill_switch(self) -> bool:
        ks = self._cfg.kill_switch
        if not ks.enabled:
            return False
        return os.path.exists(ks.lock_file_path)

    # ── window lifecycle ────────────────────────────────────────

    async def _process_window(self, window_ts: int):
        end_ts = window_ts + WINDOW_SECONDS
        c = self._cfg.crypto_5m
        monitor_start = end_ts - c.monitor_start_seconds
        execute_at_ts = end_ts - c.execute_at_seconds
        min_entry_ts = end_ts - c.min_resolve_buffer_seconds

        now_s = int(time.time())

        if now_s >= execute_at_ts:
            log.info(f"  window {_window_label(window_ts)} → {_window_label(end_ts)} too late, skipping")
            self._last_window_ts = window_ts
            return

        if now_s >= min_entry_ts:
            log.info(f"  window {_window_label(window_ts)} too close to resolve, skipping")
            self._last_window_ts = window_ts
            return

        events = await self._fetch_events(window_ts)
        if not events:
            log.info(f"  window {_window_label(window_ts)} no events found")
            self._last_window_ts = window_ts
            return

        label = _window_label(window_ts)
        end_label = _window_label(end_ts)
        log.info(
            f"{SEP}\n"
            f"  ROUND {self._round}  |  {label} → {end_label}  |  "
            f"equity: ${self._paper.equity_cents / Decimal('100'):.2f}  |  "
            f"cash: ${self._paper.balance_usd:.2f}  |  "
            f"open_exposure: ${self._paper.open_exposure_cents / Decimal('100'):.2f}  |  "
            f"exposure_pct: {c.max_exposure_per_round_pct:.0f}%  |  "
            f"max_bet: ${float(c.max_bet_usd_cap):.0f}\n"
            f"  {SEP}"
        )
        delay = monitor_start - int(time.time())
        if delay > 0:
            log.info(f"  waiting {delay}s until monitoring starts at {_window_label(monitor_start)}")
            await asyncio.sleep(delay)

        entered_coins: set[str] = set()
        self._price_streak.clear()

        while (end_ts - int(time.time())) > 3:
            if self._halt_new_entries:
                log.warning(f"  circuit breaker active ({self._circuit_breaker_reason}), halting new entries for the rest of this round")
                break

            remaining = end_ts - int(time.time())

            fresh = await self._fetch_events(window_ts)
            if fresh:
                events.update(fresh)

            new_entries = []
            for coin in CRYPTO_COINS:
                if coin in entered_coins:
                    continue
                ev_data = events.get(coin)
                if ev_data is None:
                    continue
                market = ev_data["market"]
                if market.get("closed") or not market.get("acceptingOrders", True):
                    continue
                prices = self._parse_prices(market)
                if prices is None:
                    continue
                yes, no = prices

                if yes > no:
                    side, price_cents = "YES", yes
                elif no > yes:
                    side, price_cents = "NO", no
                else:
                    continue

                max_entry = self._win_rate_max_entry(coin, price_cents, remaining)
                if price_cents > max_entry or price_cents < c.entry_price_cents_low:
                    self._price_streak[coin] = 0
                    continue

                streak = self._price_streak.get(coin, 0) + 1
                self._price_streak[coin] = streak

                if streak < 3:
                    continue

                tid = self._pick_token(market, side)
                if tid is None:
                    continue

                liq = 0.0
                try:
                    liq = float(market.get("liquidity", 0) or 0)
                except (ValueError, TypeError):
                    pass
                slippage = self._slippage_pct(liq, remaining)
                fee_pct = Decimal(str(self._cfg.trading.default_fee_pct))
                # Fixed minimum net edge required after fees/slippage/gas. This no
                # longer relaxes as the deadline approaches -- the old ladder
                # (3c / 2c / 1c depending on time left) is exactly how the bot
                # ended up paying 96-99c for coins with essentially zero real
                # edge left once costs were accounted for. Now the floor is
                # constant, so a winning trade always nets a positive amount
                # after fees + a fixed gas/slippage-model-error buffer, and a
                # near-certain (99c) market simply never qualifies.
                min_profit = c.min_net_edge_cents + c.gas_buffer_cents
                target_payout = Decimal(str(100 - min_profit))
                max_price = int(target_payout / (Decimal("1") + (slippage + fee_pct) / Decimal("100")))

                if price_cents > max_price:
                    if streak <= 3 or streak % 10 == 0:
                        log.info(f"  {coin.upper()} {side} {price_cents}¢ > max {max_price}¢ after costs, skip")
                    continue

                new_entries.append((coin, side, tid, price_cents, market))
                entered_coins.add(coin)

            if new_entries:
                tasks = []
                for coin, side, tid, pc, mk in new_entries:
                    tasks.append(self._open_position(coin, side, tid, pc, mk, remaining))
                await asyncio.gather(*tasks)

            if remaining % 10 == 0 or remaining <= 10:
                self._log_prices(events, remaining, entered_coins)

            await asyncio.sleep(c.poll_interval_seconds)

        # ── end of window — skip-and-cooldown if no trades ──
        remaining = end_ts - int(time.time())
        if remaining > 0:
            await asyncio.sleep(remaining)

        if not entered_coins:
            log.info("  no trades this round, continuing to next window")
            self._last_window_ts = window_ts
            return

        for _ in range(30):
            await self._paper.check_resolutions()
            if not self._paper.total_open_positions:
                break
            await asyncio.sleep(3)
        await self._resolve_and_report(window_ts)

    # ── early entry (price ≥95¢ during monitoring) ─────────────

    def _find_early_entry(
        self, events: dict[str, dict[str, Any]]
    ) -> tuple[str, str, str, int] | None:
        max_p = 0
        result: tuple[str, str, str, int] | None = None
        for coin, ev_data in events.items():
            market = ev_data["market"]
            if market.get("closed") or not market.get("acceptingOrders", True):
                continue
            prices = self._parse_prices(market)
            if prices is None:
                continue
            yes, no = prices
            ee = self._cfg.crypto_5m.early_entry_price_cents
            if yes >= ee:
                tid = self._pick_token(market, "YES")
                if tid and yes > max_p:
                    max_p = yes
                    result = (coin, "YES", tid, yes)
            if no >= ee:
                tid = self._pick_token(market, "NO")
                if tid and no > max_p:
                    max_p = no
                    result = (coin, "NO", tid, no)
        return result

    async def _execute_early(
        self,
        entry: tuple[str, str, str, int],
        events: dict[str, dict[str, Any]],
        end_ts: int,
    ):
        coin, side, token_id, price_cents = entry
        remaining = end_ts - int(time.time())
        ee = self._cfg.crypto_5m.early_entry_price_cents

        log.info(
            f"  EARLY ENTRY {coin.upper()} {side}  "
            f"price={price_cents}¢  "
            f"early_entry_threshold={ee}¢  "
            f"t-{remaining}s"
        )
        market = events.get(coin, {}).get("market")
        await self._open_position(coin, side, token_id, price_cents, market)

    # ── execution (deadline, no early entry triggered) ──────────

    async def _execute_at_deadline(
        self,
        events: dict[str, dict[str, Any]],
        end_ts: int,
    ):
        remaining = end_ts - int(time.time())
        c = self._cfg.crypto_5m
        threshold = c.entry_price_cents_low

        best_qualified_price = -1
        best_overall_price = -1
        best_qualified: tuple[str, str, str, int] | None = None
        best_overall: tuple[str, str, str, int] | None = None

        for coin, ev_data in events.items():
            market = ev_data["market"]
            if market.get("closed") or not market.get("acceptingOrders", True):
                continue
            prices = self._parse_prices(market)
            if prices is None:
                continue
            yes, no = prices

            if yes > best_overall_price:
                tid = self._pick_token(market, "YES")
                if tid:
                    best_overall = (coin, "YES", tid, yes)
                    best_overall_price = yes
            if no > best_overall_price:
                tid = self._pick_token(market, "NO")
                if tid:
                    best_overall = (coin, "NO", tid, no)
                    best_overall_price = no

            if yes >= threshold:
                tid = self._pick_token(market, "YES")
                if tid and yes > best_qualified_price:
                    best_qualified = (coin, "YES", tid, yes)
                    best_qualified_price = yes
            if no >= threshold:
                tid = self._pick_token(market, "NO")
                if tid and no > best_qualified_price:
                    best_qualified = (coin, "NO", tid, no)
                    best_qualified_price = no

        if best_qualified is not None:
            coin, side, token_id, price_cents = best_qualified
        elif best_overall is not None:
            coin, side, token_id, price_cents = best_overall
            c = self._cfg.crypto_5m
            extra = max(0, (c.execute_at_seconds - remaining) * 2)
            floor = max(50, c.final_price_cents - extra)
            if price_cents < floor:
                log.info(f"  SKIP {coin.upper()} {price_cents}¢ < floor {floor}¢ (t-{remaining}s)")
                return
            log.info(f"  FALLBACK threshold={threshold}¢ -> {coin.upper()} {side} {price_cents}¢ (t-{remaining}s)")
        else:
            log.info("  no tradeable coin at deadline")
            return

        market = events.get(coin, {}).get("market")
        await self._open_position(coin, side, token_id, price_cents, market)

    def _slippage_pct(self, liquidity_usd: float, remaining_s: int | None) -> Decimal:
        base = Decimal(str(self._cfg.trading.max_slippage_pct))
        if liquidity_usd >= 10_000:
            liq_mult = Decimal("1")
        elif liquidity_usd >= 1_000:
            liq_mult = Decimal("1.5")
        elif liquidity_usd >= 200:
            liq_mult = Decimal("3")
        else:
            liq_mult = Decimal("5")
        if remaining_s is None or remaining_s > 36:
            time_mult = Decimal("1")
        elif remaining_s <= 3:
            time_mult = Decimal("3")
        elif remaining_s <= 10:
            time_mult = Decimal("2")
        else:
            time_mult = Decimal("1.5")
        return base * liq_mult * time_mult

    # ── shared position opening ─────────────────────────────────

    async def _open_position(
        self,
        coin: str,
        side: str,
        token_id: str,
        price_cents: int,
        market: dict | None,
        remaining_s: int | None = None,
    ):
        c = self._cfg.crypto_5m

        if self._last_fetch_time > 0:
            stale_ms = (time.time() - self._last_fetch_time) * 1000
            stale_max = self._cfg.trading.stale_price_max_age_ms
            if stale_ms > stale_max:
                log.warning(
                    "  stale_data",
                    age_ms=int(stale_ms),
                    max_ms=stale_max,
                    coin=coin,
                )
                return

        liquidity = 0.0
        if market is not None:
            try:
                liquidity = float(market.get("liquidity", 0) or 0)
            except (ValueError, TypeError):
                pass
            if liquidity < c.min_liquidity_usd_entry:
                log.info(f"  {coin.upper()} liquidity ${liquidity:.0f} < ${c.min_liquidity_usd_entry:.0f}")
                return

        # ── correlation / concentration guards ───────────────────
        # These run BEFORE sizing and are independent of the per-round exposure
        # cap below. They are what actually stops a single correlated crypto
        # move (BTC/ETH/SOL/XRP/DOGE tend to move together within the same
        # 5-minute window) from stacking several full-size same-direction bets
        # that all resolve against the bot at once -- which is exactly what
        # produced the 19.5% drawdown in round 24 (multiple positions opened
        # across several earlier rounds, still unresolved, all lost together).
        open_positions_count = self._paper.total_open_positions
        if open_positions_count >= c.max_concurrent_open_positions:
            log.info(
                f"  {coin.upper()} max_concurrent_positions {open_positions_count} "
                f">= {c.max_concurrent_open_positions}, skip"
            )
            return

        open_exposure_cents = self._paper.open_exposure_cents
        equity_cents = self._paper.equity_cents
        max_total_exposure_cents = equity_cents * Decimal(str(c.max_total_exposure_pct)) / Decimal("100")
        if open_exposure_cents >= max_total_exposure_cents:
            log.info(
                f"  {coin.upper()} global_exposure_cap "
                f"${float(open_exposure_cents)/100:.2f} >= ${float(max_total_exposure_cents)/100:.2f}, skip"
            )
            return

        slot_exposure = self._position_cost_this_round
        max_exposure = self._paper.balance_usd * Decimal(str(c.max_exposure_per_round_pct)) / Decimal("100")
        if slot_exposure >= max_exposure:
            log.info(f"  round_exposure_exceeded ${float(slot_exposure):.2f} >= ${float(max_exposure):.2f}")
            return

        price_dec = Decimal(str(price_cents)) / Decimal("100")

        # Fixed bet in whole dollars (decoupled from balance growth)
        invest = Decimal(str(c.fixed_bet_usd))

        if c.max_bet_usd_cap > 0:
            cap = Decimal(str(c.max_bet_usd_cap))
            invest = min(invest, cap)

        liquidity_factor = COIN_LIQUIDITY_RANK.get(coin, Decimal("0.5"))
        invest *= Decimal(str(liquidity_factor))

        invest *= self._post_loss_reduction

        # Correlation dampening: each additional concurrently-open position is
        # treated as partially the same bet rather than an independent one, so
        # size shrinks as the open cluster grows (1x, 1/2x, 1/3x, ...) instead
        # of stacking full-size correlated risk on every coin that happens to
        # be showing the same momentum in the same 5 minutes.
        correlation_factor = Decimal("1") / Decimal(str(1 + open_positions_count))
        invest *= correlation_factor

        # Round to nearest whole dollar
        invest = invest.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

        remaining_exposure = max_exposure - slot_exposure
        invest = min(invest, remaining_exposure)

        remaining_global_exposure_usd = (max_total_exposure_cents - open_exposure_cents) / Decimal("100")
        invest = min(invest, remaining_global_exposure_usd)

        min_bet = Decimal(str(c.min_bet_usd))
        if invest < min_bet:
            invest = min_bet

        size = (invest / price_dec).quantize(Decimal("0.0001"))

        slippage = self._slippage_pct(liquidity, remaining_s)
        result = await self._paper.execute_fok(
            token_id=token_id, side=side, size=size, price=price_dec, market=market, slippage_pct=slippage
        )

        if result.status == "filled":
            log.info(f"  ▶ ENTER {coin.upper()} {side} at {price_cents}¢ (streak={self._price_streak.get(coin, 0)})")
            trade = {"coin": coin, "side": side, "entry": price_cents, "won": None, "profit": Decimal("0")}
            self._round_trades.append(trade)
            self._trade_by_token[token_id] = trade
            self._position_cost_this_round += invest
            log.info(f"    filled {coin.upper()} {side} ${invest:.2f} @ {price_cents}¢ | equity=${self._paper.equity_cents / Decimal('100'):.2f} | cash=${self._paper.balance_usd:.2f}")
        else:
            log.warning(f"  EXECUTION FAILED: {result.error}")

    # ── round report (no resolution — handled by background monitor) ──

    async def _resolve_and_report(self, window_ts: int):
        s = self._paper.summary()
        log.info(SUB_SEP)
        log.info(f"  TRADES THIS ROUND ({len(self._round_trades)}):")
        for t in self._round_trades:
            c = t['coin'].upper()
            status = "pending" if t['won'] is None else ("WIN" if t['won'] else "LOSS")
            pnl_str = f"  pnl=${t['profit']:.2f}" if t['won'] is not None else ""
            log.info(f"  {c:>4}  {t['side']:>3}  entry={t['entry']}¢  {status}{pnl_str}")

        wins = int(s["wins"])
        losses = int(s["losses"])
        wr = f"{wins}/{wins + losses}" if wins + losses > 0 else "N/A"
        log.info(
            f"  ROUND {self._round} SUMMARY  |  "
            f"equity: ${self._paper.equity_cents / Decimal('100'):.2f}  |  "
            f"cash: ${s['cash_balance']}  |  "
            f"open: {s['open_positions']}  |  "
            f"resolved: {s['resolved_trades']}  |  "
            f"wins: {wins}  |  "
            f"losses: {losses}  |  "
            f"WR: {wr}  |  "
            f"invested: ${float(self._position_cost_this_round):.2f}\n{SEP}"
        )
        self._last_window_ts = window_ts

    # ── prices ──────────────────────────────────────────────────

    def _log_prices(
        self,
        events: dict[str, dict[str, Any]],
        remaining_s: int,
        entered_coins: set[str] | None = None,
    ):
        if entered_coins is None:
            entered_coins = set()
        c = self._cfg.crypto_5m
        cells = []
        for coin in CRYPTO_COINS:
            marker = "●" if coin in entered_coins else " "
            ev_data = events.get(coin)
            if ev_data is None:
                cells.append(f"{coin.upper()}>?/?" )
                continue
            prices = self._parse_prices(ev_data["market"])
            if prices is None:
                cells.append(f"{coin.upper()}>?/?" )
            else:
                y, n = prices
                sent = "YES" if y > n else "NO"
                max_entry = self._win_rate_max_entry(coin, y if y > n else n, remaining_s)
                cells.append(f"{coin.upper()}>Y{y} N{n}▶{sent}{marker}(mx{max_entry})")
        log.info("  " + "  ".join(cells) + f"  low={c.entry_price_cents_low}¢  high={c.entry_price_cents_high}¢  t-{remaining_s}s")

    # ── helpers ─────────────────────────────────────────────────

    def _win_rate_max_entry(self, coin: str, price_cents: int, remaining_s: int) -> int:
        c = self._cfg.crypto_5m
        wr = self._paper.bucket_win_rate(price_cents, default=c.default_win_rate)
        breakeven = int(wr * 100)
        if breakeven > 100:
            breakeven = 100
        max_entry = breakeven - c.safety_margin_cents
        if remaining_s <= 10:
            max_entry += 2
        elif remaining_s <= 36:
            max_entry += 1
        if max_entry > c.entry_price_cents_high:
            max_entry = c.entry_price_cents_high
        if max_entry < c.entry_price_cents_low:
            max_entry = c.entry_price_cents_low
        return max_entry

    async def _fetch_events(
        self, window_ts: int
    ) -> dict[str, dict[str, Any]]:
        async def _fetch_one(coin: str) -> tuple[str, dict[str, Any] | None]:
            slug = f"{coin}-updown-5m-{window_ts}"
            try:
                resp = await self._http.get("/events", params={"slug": slug})
                if resp.status_code != 200:
                    return coin, None
                data = resp.json()
                if not data:
                    return coin, None
                markets = data[0].get("markets", [])
                if not markets:
                    return coin, None
                return coin, {"event": data[0], "market": markets[0]}
            except Exception as exc:
                log.warning("fetch_error", coin=coin, error=str(exc))
                return coin, None

        results = await asyncio.gather(*[_fetch_one(c) for c in CRYPTO_COINS])
        result: dict[str, dict[str, Any]] = {c: ev for c, ev in results if ev is not None}
        if result:
            self._last_fetch_time = time.time()
        return result

    def _parse_prices(self, m: dict) -> tuple[int, int] | None:
        raw = m.get("outcomePrices")
        if not raw:
            return None
        try:
            prices = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(prices, (list, tuple)) or len(prices) < 2:
            return None
        try:
            yes = int(Decimal(str(prices[0])) * Decimal("100"))
            no = int(Decimal(str(prices[1])) * Decimal("100"))
        except (ValueError, TypeError):
            return None
        return yes, no

    def _pick_token(self, m: dict, side: str) -> str | None:
        tid = m.get("token_id")
        if tid:
            return str(tid)
        raw = m.get("clobTokenIds")
        ids: list[str] = []
        if isinstance(raw, list):
            ids = [str(x) for x in raw]
        elif isinstance(raw, str):
            try:
                ids = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                ids = [raw]
        if len(ids) >= 2:
            return ids[0 if side == "YES" else 1]
        if ids:
            return ids[0]
        return None

    async def _shutdown(self):
        s = self._paper.summary()
        pnl = Decimal(s["cash_balance"]) - Decimal(s["initial_balance"])
        log.info(
            f"{SEP}\n"
            f"  BOT SHUTDOWN  |  rounds: {self._round}  |  final PnL: ${pnl:.2f}\n"
            f"  cash: ${s['cash_balance']}  |  "
            f"resolved: {s['resolved_trades']}  |  "
            f"wins: {s['wins']}  |  "
            f"losses: {s['losses']}\n{SEP}"
        )
        await self._http.aclose()
        await self._paper.close()

    def stop(self):
        log.warning("stop_called")
        self._running = False
