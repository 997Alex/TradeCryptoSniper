from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

from src.api import ApiClient, as_str_list
from src.config import Config
from src.logger import get_logger
from src.paper_trader import PaperTrader, PaperPosition

log = get_logger("crypto_bot")

CRYPTO_COINS = ["btc", "eth", "sol", "xrp", "doge"]
WINDOW_SECONDS = 300

# How close to the window close entries are still accepted. Below this the book
# is too thin and the fill too uncertain to be worth the slippage.
ENTRY_CUTOFF_SECONDS = 3
RESOLUTION_POLL_SECONDS = 3
KILL_SWITCH_POLL_SECONDS = 1
# Grace period for the background monitor to resolve a round's positions before
# the round summary is printed. Purely cosmetic — accounting is independent.
REPORT_WAIT_SECONDS = 30
# Back-off before retrying a coin whose entry attempt was rejected, so a
# persistent condition (thin liquidity, exposure cap) cannot spam the loop.
ENTRY_RETRY_SECONDS = 5.0

SEP = "=" * 68
SUB_SEP = "-" * 68

COIN_LIQUIDITY_RANK = {
    "btc": 1.0, "eth": 0.9, "sol": 0.7, "xrp": 0.5, "doge": 0.4,
}

CENTS = Decimal("100")


def _window_label(ts: int) -> str:
    t = time.gmtime(ts)
    return f"{t.tm_hour:02d}:{t.tm_min:02d}"


class CryptoBot:
    def __init__(self, cfg: Config):
        self._cfg = cfg
        rps = cfg.polymarket.rate_limit_rps
        self._gamma = ApiClient(cfg.polymarket.gamma_api_base, rps, timeout=15)
        self._clob = ApiClient(cfg.polymarket.clob_api_base, rps, timeout=10)
        self._paper = PaperTrader(self._gamma, cfg.paper_trading.initial_balance_usd)

        self._stop = asyncio.Event()
        self._round = 0
        self._round_trades: list[dict] = []
        self._trade_by_token: dict[str, dict] = {}
        self._last_window_ts: int | None = None

        self._session_start_balance: Decimal = Decimal("0")
        self._consecutive_losses = 0
        self._consecutive_wins = 0
        self._loss_streak_until = 0.0
        self._no_trade_until = 0.0
        self._post_loss_reduction = Decimal("1")
        self._position_cost_this_round = Decimal("0")
        self._resolved_count = 0
        self._coin_fetch_time: dict[str, float] = {}
        self._coin_retry_after: dict[str, float] = {}

    # ── entry point ─────────────────────────────────────────────

    async def run(self):
        self._session_start_balance = self._paper.balance_usd
        log.info("crypto_bot_started", balance=f"${self._session_start_balance:.2f}")

        tasks = [
            asyncio.create_task(self._resolution_monitor()),
            asyncio.create_task(self._kill_switch_watcher()),
        ]
        try:
            while not self._stop.is_set():
                window_ts = (int(time.time()) // WINDOW_SECONDS) * WINDOW_SECONDS
                if window_ts == self._last_window_ts:
                    await self._sleep(2)
                    continue

                blocked = self._check_circuit_breakers()
                if blocked:
                    log.warning(f"  circuit breaker: {blocked}")
                    await self._sleep(30)
                    continue

                self._round += 1
                self._round_trades = []
                self._position_cost_this_round = Decimal("0")
                self._coin_retry_after = {}
                try:
                    await self._process_window(window_ts)
                except Exception as exc:
                    # A single bad window must not take the bot down; the window is
                    # already marked processed, so the loop moves on to the next one.
                    log.warning("window_error", window=_window_label(window_ts), error=str(exc))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._shutdown()

    def stop(self):
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        """Sleep that returns immediately once a shutdown has been requested."""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # ── circuit breakers ────────────────────────────────────────

    def _check_circuit_breakers(self) -> str | None:
        c = self._cfg.crypto_5m
        now = time.time()

        if self._loss_streak_until > now:
            return f"loss_cooldown_{int(self._loss_streak_until - now)}s"
        if self._consecutive_losses >= c.max_consecutive_losses:
            # The cooldown has elapsed. Clear the streak so the breaker re-arms on
            # the next run of losses instead of latching for the process lifetime —
            # nothing else can reset it, because it blocks the trades that would.
            self._consecutive_losses = 0
            self._loss_streak_until = 0.0

        if self._no_trade_until > now:
            return f"no_trade_cooldown_{int(self._no_trade_until - now)}s"

        if self._session_start_balance > 0:
            equity_usd = self._paper.equity_cents / CENTS
            dd_pct = float((self._session_start_balance - equity_usd) / self._session_start_balance * CENTS)
            if dd_pct >= c.max_daily_drawdown_pct:
                return f"session_drawdown_{dd_pct:.1f}%_>={c.max_daily_drawdown_pct}%"

        return None

    def _kill_switch_active(self) -> bool:
        ks = self._cfg.kill_switch
        return ks.enabled and os.path.exists(ks.lock_file_path)

    async def _kill_switch_watcher(self):
        """Polled from its own task rather than inline, so the switch takes effect
        during the long waits too — not just while the entry loop happens to run."""
        if not self._cfg.kill_switch.enabled:
            return
        while not self._stop.is_set():
            if self._kill_switch_active():
                log.warning("  kill switch active (lock file found) — stopping")
                self.stop()
                return
            await self._sleep(KILL_SWITCH_POLL_SECONDS)

    # ── resolution accounting ───────────────────────────────────

    async def _resolution_monitor(self):
        """Sole owner of resolution accounting. Uses a persistent cursor rather than
        an index diff, so no resolution can slip past between polls."""
        max_age = self._cfg.crypto_5m.unresolved_max_age_seconds
        open_before = -1
        while not self._stop.is_set():
            try:
                open_now = self._paper.total_open_positions
                if open_now != open_before:
                    log.info(f"  open positions: {open_now} | resolved: {self._resolved_count}")
                    open_before = open_now

                await self._paper.check_resolutions(max_age_seconds=max_age)

                resolved = self._paper.resolved_positions
                while self._resolved_count < len(resolved):
                    self._on_resolved(resolved[self._resolved_count])
                    self._resolved_count += 1
            except Exception as exc:
                log.warning("resolution_monitor_error", error=str(exc))
            await self._sleep(RESOLUTION_POLL_SECONDS)

    def _on_resolved(self, pos: PaperPosition) -> None:
        c = self._cfg.crypto_5m
        coin = pos.question.split(" - ")[0].replace(" Up or Down", "")
        pnl = pos.payout_cents - pos.cost_cents
        state = f"equity=${self._paper.equity_cents / CENTS:.2f} | cash=${self._paper.balance_usd:.2f}"

        trade = self._trade_by_token.pop(pos.token_id, None)
        if trade is not None:
            trade["won"] = pos.won
            trade["profit"] = pnl

        if pos.abandoned:
            # Capital is gone, but an unresolvable market says nothing about the
            # signal — keep it out of the streak counters and the win-rate model.
            log.warning(f"  ⊘ {coin} {pos.side} WRITTEN OFF | pnl=${pnl / CENTS:.2f} | {state}")
            return

        if pos.won:
            self._consecutive_wins += 1
            self._consecutive_losses = 0
            if self._consecutive_wins >= c.win_streak_restore:
                self._post_loss_reduction = Decimal("1")
            log.info(f"  ✓ {coin} {pos.side} WON | pnl=+${pnl / CENTS:.2f} | {state}")
        else:
            self._consecutive_losses += 1
            self._consecutive_wins = 0
            self._post_loss_reduction = Decimal(str(c.loss_size_multiplier))
            log.info(f"  ✗ {coin} {pos.side} LOST | pnl=${pnl / CENTS:.2f} | {state}")
            if self._consecutive_losses >= c.max_consecutive_losses:
                self._loss_streak_until = time.time() + c.loss_cooldown_seconds
                log.warning(f"  loss streak {self._consecutive_losses}, cooldown {c.loss_cooldown_seconds}s")

    # ── window lifecycle ────────────────────────────────────────

    async def _process_window(self, window_ts: int):
        # Claimed up front: every exit path, including an exception, must leave the
        # window marked processed or the main loop will spin on it.
        self._last_window_ts = window_ts

        end_ts = window_ts + WINDOW_SECONDS
        c = self._cfg.crypto_5m
        monitor_start = end_ts - c.monitor_start_seconds

        if int(time.time()) >= end_ts - c.execute_at_seconds:
            log.info(f"  window {_window_label(window_ts)} → {_window_label(end_ts)} too late, skipping")
            return

        # Confirm the markets exist before committing to the wait below. No book
        # enrichment here — nothing is traded on these prices.
        if not await self._fetch_events(window_ts):
            log.info(f"  window {_window_label(window_ts)} no events found")
            return

        log.info(
            f"{SEP}\n"
            f"  ROUND {self._round}  |  {_window_label(window_ts)} → {_window_label(end_ts)}  |  "
            f"equity: ${self._paper.equity_cents / CENTS:.2f}  |  "
            f"cash: ${self._paper.balance_usd:.2f}  |  "
            f"risk: {c.risk_per_trade_pct:.0f}%  |  "
            f"max_bet: ${self._paper.balance_usd * Decimal(str(c.risk_per_trade_pct)) / CENTS:.2f}\n"
            f"  {SEP}"
        )

        delay = monitor_start - int(time.time())
        if delay > 0:
            log.info(f"  waiting {delay}s until monitoring starts at {_window_label(monitor_start)}")
            await self._sleep(delay)

        entered_coins = await self._monitor_and_enter(window_ts, end_ts)

        if self._stop.is_set():
            return

        remaining = end_ts - int(time.time())
        if remaining > 0:
            await self._sleep(remaining)

        if not entered_coins:
            self._no_trade_until = time.time() + c.no_trade_cooldown_seconds
            log.warning(f"  no trades this round, cooldown {c.no_trade_cooldown_seconds}s")
            return

        await self._report_round()

    async def _monitor_and_enter(self, window_ts: int, end_ts: int) -> set[str]:
        c = self._cfg.crypto_5m
        events: dict[str, dict[str, Any]] = {}
        entered_coins: set[str] = set()
        last_logged_at = -1

        while not self._stop.is_set() and (end_ts - int(time.time())) > ENTRY_CUTOFF_SECONDS:
            remaining = end_ts - int(time.time())
            threshold = self._threshold(remaining)

            fresh = await self._fetch_events(window_ts, enrich_floor_cents=threshold - c.book_enrich_margin_cents)
            if fresh:
                events.update(fresh)

            candidates = self._find_candidates(events, threshold, entered_coins)
            if candidates:
                filled = await asyncio.gather(
                    *[self._open_position(coin, side, tid, pc, mk, remaining) for coin, side, tid, pc, mk in candidates]
                )
                now = time.time()
                for (coin, *_), ok in zip(candidates, filled):
                    if ok:
                        entered_coins.add(coin)
                    else:
                        self._coin_retry_after[coin] = now + ENTRY_RETRY_SECONDS

            # Polling is sub-second, so guard against logging the same second twice.
            if remaining != last_logged_at and (remaining % 10 == 0 or remaining <= 10):
                self._log_prices(events, threshold, remaining, entered_coins)
                last_logged_at = remaining

            await self._sleep(c.poll_interval_seconds)

        return entered_coins

    def _find_candidates(
        self, events: dict[str, dict[str, Any]], threshold: int, entered_coins: set[str]
    ) -> list[tuple[str, str, str, int, dict]]:
        now = time.time()
        max_age_s = self._cfg.trading.stale_price_max_age_ms / 1000
        candidates: list[tuple[str, str, str, int, dict]] = []

        for coin in CRYPTO_COINS:
            if coin in entered_coins or self._coin_retry_after.get(coin, 0.0) > now:
                continue
            ev_data = events.get(coin)
            if ev_data is None:
                continue
            # Per-coin staleness: a coin whose own fetches are failing keeps its
            # last-known prices in `events`, and a global timestamp would not catch it.
            if now - self._coin_fetch_time.get(coin, 0.0) > max_age_s:
                continue

            market = ev_data["market"]
            if market.get("closed") or not market.get("acceptingOrders", True):
                continue
            prices = self._parse_prices(market)
            if prices is None:
                continue
            yes, no = prices

            if yes > no and yes >= threshold:
                side, price_cents = "YES", yes
            elif no > yes and no >= threshold:
                side, price_cents = "NO", no
            else:
                continue

            token_id = self._pick_token(market, side)
            if token_id:
                candidates.append((coin, side, token_id, price_cents, market))

        return candidates

    # ── position opening ────────────────────────────────────────

    async def _open_position(
        self,
        coin: str,
        side: str,
        token_id: str,
        price_cents: int,
        market: dict | None,
        remaining_s: int,
    ) -> bool:
        c = self._cfg.crypto_5m
        tag = coin.upper()

        liquidity = 0.0
        if market is not None:
            try:
                liquidity = float(market.get("liquidity", 0) or 0)
            except (ValueError, TypeError):
                liquidity = 0.0
            if liquidity < c.min_liquidity_usd_entry:
                log.info(f"  {tag} skip: liquidity ${liquidity:.0f} < ${c.min_liquidity_usd_entry:.0f}")
                return False

        slippage = self._slippage_pct(liquidity, remaining_s)
        price_dec = Decimal(price_cents) / CENTS
        fill_cents = PaperTrader.raw_fill_price_cents(price_dec, slippage)

        # A contract pays exactly 100¢. If slippage carries the fill to par there is
        # no outcome that profits, so there is no reason to take the trade.
        if fill_cents >= CENTS:
            log.info(f"  {tag} skip: {price_cents}¢ +{slippage}% slippage fills at {fill_cents}¢ >= par")
            return False

        max_exposure = self._paper.balance_usd * Decimal(str(c.max_exposure_per_round_pct)) / CENTS
        remaining_exposure = max_exposure - self._position_cost_this_round
        if remaining_exposure <= 0:
            log.info(f"  {tag} skip: round exposure ${self._position_cost_this_round:.2f} >= ${max_exposure:.2f}")
            return False

        invest = self._size_position(coin, price_cents, fill_cents, remaining_exposure)
        if invest is None:
            return False

        # Size against the price actually paid, so the cash spent matches `invest`.
        size = (invest * CENTS / fill_cents).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)

        result = await self._paper.execute_fok(
            token_id=token_id,
            side=side,
            size=size,
            price=price_dec,
            quote_price_cents=price_cents,
            market=market,
            slippage_pct=slippage,
        )
        if result.status != "filled":
            log.warning(f"  {tag} order rejected: {result.error}")
            return False

        # Track the cash actually debited, not the pre-slippage notional.
        self._position_cost_this_round += result.cost_cents / CENTS

        trade = {"coin": coin, "side": side, "entry": price_cents, "won": None, "profit": Decimal("0")}
        self._round_trades.append(trade)
        self._trade_by_token[token_id] = trade
        log.info(
            f"  ▶ ENTER {tag} {side} ${result.cost_cents / CENTS:.2f} @ {price_cents}¢ "
            f"(fill {result.avg_price_cents}¢, t-{remaining_s}s) | "
            f"equity=${self._paper.equity_cents / CENTS:.2f} | cash=${self._paper.balance_usd:.2f}"
        )
        return True

    def _size_position(
        self, coin: str, quote_cents: int, fill_cents: Decimal, remaining_exposure: Decimal
    ) -> Decimal | None:
        """Fractional Kelly on the price actually paid. Returns None if the trade
        should be skipped."""
        c = self._cfg.crypto_5m

        # Bucket history is keyed on the quote, matching how it is recorded at
        # resolution — see PaperPosition.quote_price_cents.
        if self._paper.bucket_trade_count(quote_cents) < c.min_data_trades:
            kelly_pct = Decimal(str(c.risk_per_trade_pct)) / Decimal("200")
        else:
            win_rate = Decimal(str(self._paper.bucket_win_rate(quote_cents, default=c.default_win_rate)))
            if win_rate * CENTS <= fill_cents:
                log.info(f"  {coin.upper()} skip: win rate {win_rate:.0%} <= fill {fill_cents}¢ (non-positive EV)")
                return None
            b = (CENTS - fill_cents) / fill_cents
            kelly_pct = win_rate - (Decimal("1") - win_rate) / b
            kelly_pct = max(Decimal("0"), kelly_pct) * Decimal(str(c.kelly_fraction))
            kelly_pct = min(kelly_pct, Decimal(str(c.risk_per_trade_pct)) / CENTS)

        invest = self._paper.balance_usd * kelly_pct
        if c.max_bet_usd_cap > 0:
            invest = min(invest, Decimal(str(c.max_bet_usd_cap)))
        invest *= Decimal(str(COIN_LIQUIDITY_RANK.get(coin, 0.5)))
        invest *= self._post_loss_reduction
        invest = min(invest, remaining_exposure).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

        if invest < Decimal("1"):
            log.info(f"  {coin.upper()} skip: investment ${invest:.2f} < $1")
            return None
        return invest

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
        elif remaining_s <= ENTRY_CUTOFF_SECONDS:
            time_mult = Decimal("3")
        elif remaining_s <= 10:
            time_mult = Decimal("2")
        else:
            time_mult = Decimal("1.5")

        return base * liq_mult * time_mult

    def _threshold(self, remaining_s: int) -> int:
        if remaining_s <= ENTRY_CUTOFF_SECONDS:
            return 80
        if remaining_s <= 36:
            progress = (36 - remaining_s) / (36 - ENTRY_CUTOFF_SECONDS)
            return int(90 - progress * 10)
        return 90

    # ── reporting ───────────────────────────────────────────────

    async def _report_round(self):
        # The background monitor owns resolution; give it a moment to catch up so the
        # summary shows outcomes rather than "pending".
        deadline = time.time() + REPORT_WAIT_SECONDS
        while time.time() < deadline and any(t["won"] is None for t in self._round_trades):
            await self._sleep(1)
            if self._stop.is_set():
                break

        s = self._paper.summary()
        log.info(SUB_SEP)
        log.info(f"  TRADES THIS ROUND ({len(self._round_trades)}):")
        for t in self._round_trades:
            status = "pending" if t["won"] is None else ("WIN" if t["won"] else "LOSS")
            pnl_str = f"  pnl=${t['profit'] / CENTS:.2f}" if t["won"] is not None else ""
            log.info(f"  {t['coin'].upper():>4}  {t['side']:>3}  entry={t['entry']}¢  {status}{pnl_str}")

        wins, losses = int(s["wins"]), int(s["losses"])
        wr = f"{wins}/{wins + losses}" if wins + losses > 0 else "N/A"
        log.info(
            f"  ROUND {self._round} SUMMARY  |  "
            f"equity: ${self._paper.equity_cents / CENTS:.2f}  |  "
            f"cash: ${s['cash_balance']}  |  "
            f"trades: {s['resolved_trades']}  |  "
            f"wins: {wins}  |  losses: {losses}  |  WR: {wr}  |  "
            f"invested: ${self._position_cost_this_round:.2f}\n{SEP}"
        )

    def _log_prices(
        self,
        events: dict[str, dict[str, Any]],
        threshold: int,
        remaining_s: int,
        entered_coins: set[str],
    ):
        cells = []
        for coin in CRYPTO_COINS:
            ev_data = events.get(coin)
            prices = self._parse_prices(ev_data["market"]) if ev_data else None
            if prices is None:
                cells.append(f"{coin.upper()}>?/?")
                continue
            y, n = prices
            marker = "●" if coin in entered_coins else " "
            cells.append(f"{coin.upper()}>Y{y} N{n}▶{'YES' if y > n else 'NO'}{marker}")
        log.info("  " + "  ".join(cells) + f"  thr={threshold}¢  t-{remaining_s}s")

    # ── market data ─────────────────────────────────────────────

    async def _fetch_events(
        self, window_ts: int, enrich_floor_cents: int | None = None
    ) -> dict[str, dict[str, Any]]:
        async def _fetch_one(coin: str) -> tuple[str, dict[str, Any] | None]:
            data = await self._gamma.get_json("/events", params={"slug": f"{coin}-updown-5m-{window_ts}"})
            if not isinstance(data, list) or not data:
                return coin, None
            markets = data[0].get("markets") if isinstance(data[0], dict) else None
            if not isinstance(markets, list) or not markets:
                return coin, None
            return coin, {"event": data[0], "market": markets[0]}

        results = await asyncio.gather(*[_fetch_one(c) for c in CRYPTO_COINS])
        events = {coin: ev for coin, ev in results if ev is not None}

        if events and enrich_floor_cents is not None:
            await self._enrich_with_book_prices(events, enrich_floor_cents)

        # Stamped after enrichment: the guard measures the age of the prices an
        # entry would actually be placed on.
        now = time.time()
        for coin in events:
            self._coin_fetch_time[coin] = now
        return events

    async def _enrich_with_book_prices(
        self, events: dict[str, dict[str, Any]], floor_cents: int
    ) -> None:
        """Replace Gamma's cached prices with live CLOB mid-prices. Only coins that
        could plausibly cross the threshold are enriched — book precision is wasted
        on a coin trading 30¢ away from it, and it is 2 requests per coin per tick."""

        async def _mid_price(token_id: str) -> Decimal | None:
            data = await self._clob.get_json("/book", params={"token_id": token_id})
            if not isinstance(data, dict):
                return None
            bids = data.get("bids") or []
            asks = data.get("asks") or []
            try:
                best_bid = Decimal(str(bids[0]["price"])) if bids else None
                best_ask = Decimal(str(asks[0]["price"])) if asks else None
            except (ArithmeticError, KeyError, TypeError, ValueError):
                return None
            if best_bid is not None and best_ask is not None:
                return (best_bid + best_ask) / Decimal("2")
            return best_bid or best_ask

        async def _enrich_market(market: dict) -> None:
            ids = as_str_list(market.get("clobTokenIds"))
            if len(ids) < 2:
                return
            cached = self._parse_prices(market)
            if cached is not None and max(cached) < floor_cents:
                return
            yes_p, no_p = await asyncio.gather(_mid_price(ids[0]), _mid_price(ids[1]))
            if yes_p is not None and no_p is not None:
                market["outcomePrices"] = [float(yes_p), float(no_p)]

        await asyncio.gather(*[_enrich_market(ev["market"]) for ev in events.values()])

    def _parse_prices(self, m: dict) -> tuple[int, int] | None:
        prices = as_str_list(m.get("outcomePrices"))
        if len(prices) < 2:
            return None
        try:
            # Round rather than truncate: against a cent-granular threshold, int()
            # would push every 0.899 mid down to 89¢ and miss a 90¢ entry.
            yes = int((Decimal(prices[0]) * CENTS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            no = int((Decimal(prices[1]) * CENTS).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (ArithmeticError, ValueError):
            return None
        return yes, no

    def _pick_token(self, m: dict, side: str) -> str | None:
        tid = m.get("token_id")
        if tid:
            return str(tid)
        ids = as_str_list(m.get("clobTokenIds"))
        if len(ids) >= 2:
            return ids[0 if side == "YES" else 1]
        return ids[0] if ids else None

    # ── shutdown ────────────────────────────────────────────────

    async def _shutdown(self):
        s = self._paper.summary()
        pnl = Decimal(s["cash_balance"]) - Decimal(s["initial_balance"])
        log.info(
            f"{SEP}\n"
            f"  BOT SHUTDOWN  |  rounds: {self._round}  |  final PnL: ${pnl:.2f}\n"
            f"  balance: ${s['cash_balance']}  |  "
            f"trades: {s['resolved_trades']}  |  "
            f"wins: {s['wins']}  |  losses: {s['losses']}\n{SEP}"
        )
        await self._paper.close()
        await self._gamma.aclose()
        await self._clob.aclose()
