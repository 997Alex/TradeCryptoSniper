"""Regression tests for the defects fixed in the audit pass.

No network and no extra plugins — the few coroutines under test are driven with
asyncio.run, and every trader is built against a tmp_path so a real
data/bucket_stats.json can never leak into a test.
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import pytest

from src.config import Config
from src.crypto_bot import CryptoBot, ENTRY_CUTOFF_SECONDS
from src.paper_trader import BUCKET_LABELS, MAX_FILL_CENTS, PaperTrader

RAW_CONFIG = {
    "polymarket": {
        "gamma_api_base": "https://gamma-api.polymarket.com",
        "clob_api_base": "https://clob.polymarket.com",
    }
}

# Every (liquidity, remaining) tier reachable from the live entry loop.
# Sub-$200 liquidity is rejected before sizing, hence the split.
TRADEABLE_LIQUIDITY = [50_000.0, 5_000.0, 500.0]
ALL_LIQUIDITY = TRADEABLE_LIQUIDITY + [100.0]
TIME_TIERS = [60, 30, 8, 4]

run = asyncio.run


@pytest.fixture
def trader(tmp_path) -> PaperTrader:
    return PaperTrader(gamma=None, initial_balance_usd=100.0, stats_path=str(tmp_path / "stats.json"))


@pytest.fixture
def bot(trader) -> CryptoBot:
    b = CryptoBot(Config.model_validate(RAW_CONFIG))
    b._paper = trader
    return b


# ── entry ladder and slippage ───────────────────────────────────


def test_threshold_ladder(bot):
    assert bot._threshold(60) == 90
    assert bot._threshold(36) == 90
    assert bot._threshold(ENTRY_CUTOFF_SECONDS) == 80

    ladder = [bot._threshold(r) for r in range(60, ENTRY_CUTOFF_SECONDS - 1, -1)]
    assert ladder == sorted(ladder, reverse=True), "threshold must never rise as time runs out"
    assert (min(ladder), max(ladder)) == (80, 90)


def test_slippage_matrix(bot):
    everything = {bot._slippage_pct(liq, rem) for liq in ALL_LIQUIDITY for rem in TIME_TIERS}
    assert max(everything) == Decimal("10")  # 5x liquidity * 2x time * 1% base

    tradeable = {bot._slippage_pct(liq, rem) for liq in TRADEABLE_LIQUIDITY for rem in TIME_TIERS}
    assert max(tradeable) == Decimal("6")
    assert min(tradeable) == Decimal("1")


# ── A4: a fill must never reach par ─────────────────────────────


def test_fill_price_never_reaches_par(bot):
    for quote in range(50, 100):
        for liq in ALL_LIQUIDITY:
            for rem in TIME_TIERS:
                fill = PaperTrader.fill_price_cents(Decimal(quote) / Decimal("100"), bot._slippage_pct(liq, rem))
                assert fill <= MAX_FILL_CENTS < Decimal("100")


def test_losing_fills_are_recognised_before_entry(bot):
    """The entry gate looks at the raw price; the clamp is only a backstop."""
    slippage = bot._slippage_pct(500.0, 8)
    assert slippage == Decimal("6")
    assert PaperTrader.raw_fill_price_cents(Decimal("0.97"), slippage) >= Decimal("100")
    assert PaperTrader.raw_fill_price_cents(Decimal("0.90"), slippage) < Decimal("100")


# ── A5: sizing uses the price actually paid ─────────────────────


def _seed_bucket(trader: PaperTrader, price_cents: int, trades: int, wins: int) -> None:
    trader._bucket_stats[PaperTrader._price_bucket(price_cents)] = {
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "total_pnl_cents": Decimal("0"),
    }


def test_kelly_requires_positive_ev(bot, trader):
    _seed_bucket(trader, 95, trades=15, wins=5)  # 33% win rate
    assert bot._size_position("btc", 95, Decimal("96"), Decimal("100")) is None


def test_kelly_sizes_on_fill_not_quote(bot, trader):
    _seed_bucket(trader, 90, trades=15, wins=14)  # 93% win rate
    clean = bot._size_position("btc", 90, Decimal("90"), Decimal("100"))
    slipped = bot._size_position("btc", 90, Decimal("92"), Decimal("100"))
    assert clean is not None and slipped is not None
    assert slipped < clean, "a worse fill must produce a smaller position"


def test_sizing_respects_remaining_exposure(bot, trader):
    _seed_bucket(trader, 90, trades=15, wins=14)
    capped = bot._size_position("btc", 90, Decimal("90"), Decimal("2.50"))
    assert capped == Decimal("2.50")
    assert bot._size_position("btc", 90, Decimal("90"), Decimal("0.99")) is None  # below the $1 floor


def test_post_loss_reduction_shrinks_size(bot, trader):
    _seed_bucket(trader, 90, trades=15, wins=14)
    full = bot._size_position("btc", 90, Decimal("90"), Decimal("100"))
    bot._post_loss_reduction = Decimal("0.5")
    reduced = bot._size_position("btc", 90, Decimal("90"), Decimal("100"))
    assert reduced == (full * Decimal("0.5")).quantize(Decimal("0.01"))


# ── A6: bucket statistics round-trip ────────────────────────────


def test_bucket_round_trip(tmp_path, bot):
    """The bucket sizing READS must be the bucket resolution WRITES, for every
    reachable (quote, slippage) pair. Keying the write on the slipped fill price
    was the bug: a 94¢ quote at 3% used to be filed under 95-99¢."""
    for quote in (78, 85, 89, 90, 94, 95, 99):
        for liq in TRADEABLE_LIQUIDITY:
            for rem in TIME_TIERS:
                slippage = bot._slippage_pct(liq, rem)
                t = PaperTrader(None, 100.0, stats_path=str(tmp_path / f"s{quote}{liq}{rem}.json"))
                result = run(
                    t.execute_fok(
                        token_id="tok",
                        side="YES",
                        size=Decimal("1"),
                        price=Decimal(quote) / Decimal("100"),
                        quote_price_cents=quote,
                        slippage_pct=slippage,
                    )
                )
                assert result.status == "filled"
                run(t.resolve_position("tok", won=True))

                written = [b for b, s in t._bucket_stats.items() if int(s["trades"]) > 0]
                assert written == [PaperTrader._price_bucket(quote)], (
                    f"quote={quote} slippage={slippage} filed under {written}"
                )


def test_price_bucket_never_returns_unknown():
    for cents in range(-50, 250):
        assert PaperTrader._price_bucket(cents) in BUCKET_LABELS


def test_abandoned_positions_stay_out_of_bucket_stats(trader):
    run(trader.execute_fok("tok", "YES", Decimal("10"), Decimal("0.90"), quote_price_cents=90))
    pos = run(trader.resolve_position("tok", won=False, abandoned=True))
    assert pos is not None and pos.abandoned
    assert not any(int(s["trades"]) > 0 for s in trader._bucket_stats.values())
    assert trader.balance_usd == Decimal("91.00"), "the cash is still gone"


# ── A7: no all-in fallback ──────────────────────────────────────


def test_insufficient_balance_rejects(trader):
    result = run(
        trader.execute_fok("tok", "YES", Decimal("500"), Decimal("0.90"), quote_price_cents=90)
    )
    assert result.status == "rejected"
    assert "insufficient" in (result.error or "")
    assert trader.balance_usd == Decimal("100.00"), "a rejected order must not move cash"
    assert trader.total_open_positions == 0


def test_fill_debits_exactly_the_cost(trader):
    result = run(trader.execute_fok("tok", "YES", Decimal("10"), Decimal("0.90"), quote_price_cents=90))
    assert result.status == "filled"
    assert result.cost_cents == Decimal("900")
    assert trader.balance_usd == Decimal("91.00")
    assert trader.equity_cents == Decimal("10000"), "opening a position must not change equity"


def test_winning_position_pays_par(trader):
    run(trader.execute_fok("tok", "YES", Decimal("10"), Decimal("0.90"), quote_price_cents=90))
    run(trader.resolve_position("tok", won=True))
    assert trader.balance_usd == Decimal("101.00")  # 91 + 10 shares * 100c


def test_resolving_an_unknown_token_is_a_noop(trader):
    assert run(trader.resolve_position("ghost", won=True)) is None
    assert trader.balance_usd == Decimal("100.00")


# ── A1 / A8: circuit breaker state machine ──────────────────────


def test_circuit_breaker_recovers_after_cooldown(bot):
    c = bot._cfg.crypto_5m
    bot._session_start_balance = Decimal("100")

    bot._consecutive_losses = c.max_consecutive_losses
    bot._loss_streak_until = time.time() + c.loss_cooldown_seconds
    assert (bot._check_circuit_breakers() or "").startswith("loss_cooldown")

    # Cooldown elapses -> the breaker must release rather than latch forever.
    bot._loss_streak_until = time.time() - 1
    assert bot._check_circuit_breakers() is None
    assert bot._consecutive_losses == 0
    assert bot._check_circuit_breakers() is None


def test_no_trade_cooldown_does_not_truncate_loss_cooldown(bot):
    c = bot._cfg.crypto_5m
    bot._session_start_balance = Decimal("100")

    bot._consecutive_losses = c.max_consecutive_losses
    bot._loss_streak_until = time.time() + c.loss_cooldown_seconds
    bot._no_trade_until = time.time() + c.no_trade_cooldown_seconds

    reason = bot._check_circuit_breakers() or ""
    assert reason.startswith("loss_cooldown")
    remaining = int(reason.rsplit("_", 1)[-1].rstrip("s"))
    assert remaining > c.no_trade_cooldown_seconds, "a quiet window must not shorten the 1h halt"


def test_kill_switch_detects_lock_file(bot, tmp_path):
    lock = tmp_path / "kill.lock"
    bot._cfg.kill_switch.lock_file_path = str(lock)
    assert bot._kill_switch_active() is False
    lock.touch()
    assert bot._kill_switch_active() is True
    bot._cfg.kill_switch.enabled = False
    assert bot._kill_switch_active() is False


def test_stop_releases_pending_sleeps(bot):
    async def scenario():
        bot.stop()
        start = time.monotonic()
        await bot._sleep(30)
        return time.monotonic() - start

    assert run(scenario()) < 1.0, "a requested shutdown must not wait out a long sleep"


def test_drawdown_breaker_fires(bot, trader):
    bot._session_start_balance = Decimal("100")
    assert bot._check_circuit_breakers() is None
    trader._balance_cents = Decimal("8000")  # -20%
    assert (bot._check_circuit_breakers() or "").startswith("session_drawdown")


def test_open_positions_do_not_register_as_drawdown(bot, trader):
    bot._session_start_balance = Decimal("100")
    run(trader.execute_fok("tok", "YES", Decimal("50"), Decimal("0.90"), quote_price_cents=90))
    assert trader.balance_usd < Decimal("60")
    assert bot._check_circuit_breakers() is None, "cost-basis equity must absorb an open position"


# ── A14: price parsing ──────────────────────────────────────────


def test_parse_prices_rounds_not_truncates(bot):
    assert bot._parse_prices({"outcomePrices": ["0.899", "0.101"]}) == (90, 10)
    assert bot._parse_prices({"outcomePrices": '["0.9499", "0.0501"]'}) == (95, 5)
    assert bot._parse_prices({"outcomePrices": ["0.5", "0.5"]}) == (50, 50)


def test_parse_prices_rejects_junk(bot):
    for raw in (None, "", "not json", ["0.9"], {"a": 1}, ["x", "y"], 42):
        assert bot._parse_prices({"outcomePrices": raw}) is None


def test_pick_token_handles_both_encodings(bot):
    assert bot._pick_token({"clobTokenIds": '["yes-id", "no-id"]'}, "YES") == "yes-id"
    assert bot._pick_token({"clobTokenIds": '["yes-id", "no-id"]'}, "NO") == "no-id"
    assert bot._pick_token({"clobTokenIds": ["a", "b"]}, "NO") == "b"
    assert bot._pick_token({"clobTokenIds": "garbage"}, "YES") is None
    assert bot._pick_token({}, "YES") is None
