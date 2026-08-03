"""Real order placement. Trimmed port of PolyML's `live/execute.py`.

Presents the SAME `execute_fok(...)` signature as `PaperTrader`, so the strategy
calls it through the identical call site and nothing in `crypto_bot.py` needs to
know which one it holds. `PaperTrader` remains the position ledger and the
resolution engine in both modes -- only the source of the fill changes.

Three deliberate deviations from PolyML, each load-bearing:

1. **FOK, not GTC.** PolyML's markets run 15 minutes, so a resting order is
   reasonable. These resolve in under two. A GTC order that rests becomes either
   a stranded position or -- worse -- one that PolyML's `live` branch would book
   as filled. `config.yaml` has said `order_type: "FOK"` all along.
2. **A `live` (resting) response is a rejection, not a fill.** Under FOK it
   should be unreachable; treating it as a fill is the failure this guards.
3. **`order_type` passed BY KEYWORD.** The signature is
   `create_and_post_order(order_args, options=None, order_type="GTC", ...)`, so
   passing it positionally lands it in `options`, where the SDK reads
   `.tick_size` off a plain str -> `AttributeError` on the first real order.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from decimal import Decimal

from src.order_executor import OrderResult
from utils.logger import get_logger

log = get_logger("live")

SIG_TYPE_MEANING = {
    0: "EOA -- the signer IS the funder",
    1: "POLY_PROXY -- the signer owns a Polymarket proxy wallet",
    2: "POLY_GNOSIS_SAFE -- the signer owns a Polymarket Gnosis safe",
    3: "POLY_1271 -- the signer owns an ERC-1271 DepositWallet (what sign-up creates now)",
}


@dataclass
class Preflight:
    ok: bool
    reason: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class Fill:
    status: str
    shares: int
    price: float
    book: bool
    why: str = ""


def fill_from_response(resp, *, limit_price: float, shares_asked: int,
                       side: str = "BUY") -> Fill:
    """Read the venue's own answer instead of assuming the order filled at our limit.

    For a BUY, `makingAmount` is the fee-exclusive dollars paid and `takingAmount`
    the shares received, so their ratio is the executed VWAP and partial fills come
    out for free. The sense inverts on SELL, which is why `side` is required.

    PolyML recorded the limit price instead and that put its ledger $1.4846 below
    the chain over twelve real fills.
    """
    if not isinstance(resp, dict):
        return Fill("none", 0, float("nan"), False, f"no_response:{type(resp).__name__}")
    status = str(resp.get("status") or "").lower()
    if status == "matched":
        taking, making = resp.get("takingAmount"), resp.get("makingAmount")
        try:
            shares_s, dollars_s = ((taking, making) if side.upper() == "BUY"
                                   else (making, taking))
            shares, dollars = int(round(float(shares_s))), float(dollars_s)
        except (TypeError, ValueError):
            return Fill(status, 0, float("nan"), False,
                        f"matched_but_unparseable:{taking!r}/{making!r}")
        if shares <= 0 or not dollars > 0:
            return Fill(status, 0, float("nan"), False, "matched_but_zero_size")
        return Fill(status, shares, dollars / shares, True,
                    "" if shares == int(shares_asked) else f"partial:{shares}/{shares_asked}")
    if status == "live":
        # Deviation 2. Under FOK this should be unreachable.
        return Fill(status, 0, float("nan"), False, "rested_under_fok")
    return Fill(status or "missing", 0, float("nan"), False, f"order_{status or 'missing'}")


def _client(signature_type: int, funder: str, creds=None):
    from py_clob_client_v2.client import ClobClient
    return ClobClient(os.environ.get("POLY_CLOB_HOST", "https://clob.polymarket.com"),
                      chain_id=int(os.environ.get("POLY_CHAIN_ID", "137")),
                      key=os.environ["POLY_PRIVATE_KEY"],
                      signature_type=int(signature_type), funder=funder, creds=creds)


def _collateral_balance(client) -> float:
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams
    b = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    return int(b.get("balance", 0)) / 1e6


def preflight() -> Preflight:
    """Ask the VENUE whether this key, wallet and signature type can trade.

    A signature type is a claim about which wallet the venue will debit, and the
    venue is the only authority on that -- so it is asked by reading the balance.
    If the configured type sees nothing, every type is tried and the working one
    is named, because an operator with a wrong .env needs the answer.
    """
    try:
        import py_clob_client_v2  # noqa: F401
    except ImportError as e:
        return Preflight(False, f"py-clob-client-v2 is not installed ({e}). Note the name: "
                                f"`py-clob-client` (no -v2) is a DIFFERENT, archived package "
                                f"that cannot sign for this venue.")

    funder = os.environ.get("POLY_FUNDER_ADDRESS", "").strip()
    if not funder:
        return Preflight(False, "POLY_FUNDER_ADDRESS is unset -- there is no wallet to spend from")
    if not os.environ.get("POLY_PRIVATE_KEY", "").strip():
        return Preflight(False, "POLY_PRIVATE_KEY is unset")
    try:
        sig = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
    except ValueError:
        return Preflight(False, "POLY_SIGNATURE_TYPE is not an integer")

    d = {"funder": funder, "signature_type": sig,
         "signature_type_means": SIG_TYPE_MEANING.get(sig, "unknown")}
    try:
        base = _client(sig, funder)
        d["signer"] = base.get_address()
        d["clob_version"] = base.get_version()
        base.set_api_creds(base.create_or_derive_api_key())
    except Exception as e:
        return Preflight(False, f"could not reach the CLOB or derive API credentials "
                                f"({type(e).__name__}: {str(e)[:160]})", d)

    if str(d["clob_version"]) != "2":
        return Preflight(False, f"the CLOB reports version {d['clob_version']}, but this client "
                                f"builds v2 orders -- every order would be rejected terminally", d)
    try:
        bal = _collateral_balance(base)
    except Exception as e:
        return Preflight(False, f"could not read the collateral balance "
                                f"({type(e).__name__}: {str(e)[:160]})", d)
    d["collateral_usd"] = bal
    if bal > 0:
        return Preflight(True, "", d)

    working = []
    for t in (0, 1, 2, 3):
        if t == sig:
            continue
        try:
            c = _client(t, funder)
            c.set_api_creds(c.create_or_derive_api_key())
            if _collateral_balance(c) > 0:
                working.append(t)
        except Exception:
            pass
    hint = (f" Signature type {working[0]} DOES see collateral -- set "
            f"POLY_SIGNATURE_TYPE={working[0]} ({SIG_TYPE_MEANING.get(working[0])})."
            if working else " No signature type sees any collateral in this wallet.")
    return Preflight(False, f"signature type {sig} sees $0.00 collateral for {funder}.{hint}", d)


class LiveExecutor:
    """Places real FOK orders and books the fill into the shared ledger."""

    def __init__(self, ledger, client, check: Preflight) -> None:
        self._ledger = ledger
        self._client = client
        self.check = check

    @staticmethod
    def round_to_tick(price: float, tick: float) -> float:
        """Snap to the market's own tick. `tick` comes from the market, never a constant --
        it varies between assets at the same instant."""
        if not tick or tick <= 0:
            raise ValueError("tick must be positive; it comes from the market object")
        return round(round(float(price) / float(tick)) * float(tick), 10)

    def _place(self, token_id: str, price: float, shares: int, tick: float,
               side: str, neg_risk: bool):
        from py_clob_client_v2 import Side
        from py_clob_client_v2.clob_types import (OrderArgs, OrderType,
                                                  PartialCreateOrderOptions)
        px = self.round_to_tick(price, tick)
        args = OrderArgs(token_id=str(token_id), price=px, size=int(shares),
                         side=Side.BUY if side.upper() == "BUY" else Side.SELL)
        opts = PartialCreateOrderOptions(tick_size=str(tick), neg_risk=bool(neg_risk))
        # Deviation 3: order_type BY KEYWORD, and deviation 1: FOK not GTC.
        return self._client.create_and_post_order(args, opts, order_type=OrderType.FOK)

    async def execute_fok(
        self,
        token_id: str,
        side: str,
        size: Decimal,
        price: Decimal,
        market: dict | None = None,
        slippage_pct: Decimal | None = None,
    ) -> OrderResult:
        m = market or {}
        try:
            tick = float(m.get("orderPriceMinTickSize") or 0.01)
            min_sz = int(float(m.get("orderMinSize") or 5))
        except (TypeError, ValueError):
            tick, min_sz = 0.01, 5
        neg_risk = bool(m.get("negRisk", False))

        # The venue wants whole shares. 5/0.88 = 5.68 -> 5 shares = $4.40.
        shares = int(size)
        if shares < min_sz:
            log.warning("live_size_below_min", token_id=token_id, shares=shares, min=min_sz)
            return OrderResult(status="rejected", target_size=size,
                               error=f"size {shares} < orderMinSize {min_sz}")

        px = self.round_to_tick(float(price), tick)
        try:
            # The SDK is synchronous; keep it off the event loop so the other
            # coins' polling does not stall behind a network round trip.
            resp = await asyncio.to_thread(
                self._place, token_id, float(price), shares, tick, side, neg_risk
            )
        except Exception as exc:
            log.error("live_order_failed", token_id=token_id, error=f"{type(exc).__name__}: {exc}")
            return OrderResult(status="rejected", target_size=size,
                               error=f"{type(exc).__name__}: {str(exc)[:200]}")

        fill = fill_from_response(resp, limit_price=px, shares_asked=shares, side=side)
        if not fill.book:
            log.warning("live_order_not_filled", token_id=token_id,
                        status=fill.status, why=fill.why)
            return OrderResult(status="rejected", target_size=size,
                               error=f"{fill.status}:{fill.why}")

        await self._ledger.book_fill(
            token_id=token_id, side=side,
            size=Decimal(str(fill.shares)),
            fill_price=Decimal(str(fill.price)),
            market=m,
        )
        log.info(f"  LIVE FILL {side} {fill.shares} sh @ {fill.price*100:.1f}¢ "
                 f"order={resp.get('orderID')}")
        return OrderResult(
            status="filled",
            filled_size=Decimal(str(fill.shares)),
            target_size=size,
            avg_price=Decimal(str(fill.price)),
            trade_id=str(resp.get("orderID") or ""),
        )


def build_executor(ledger) -> tuple[LiveExecutor | None, str]:
    """Gate 6. Returns (executor, reason). A None executor must ABORT, not downgrade."""
    check = preflight()
    if not check.ok:
        return None, check.reason
    funder = os.environ["POLY_FUNDER_ADDRESS"].strip()
    sig = int(os.environ.get("POLY_SIGNATURE_TYPE", "3"))
    client = _client(sig, funder)
    client.set_api_creds(client.create_or_derive_api_key())
    return LiveExecutor(ledger, client, check), ""
