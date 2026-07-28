from __future__ import annotations

from decimal import Decimal
from typing import Any

# from src.clob_api import CLOBClient
from utils.logger import get_logger

# log = get_logger("executor")


class OrderResult:
    def __init__(
        self,
        status: str,
        filled_size: Decimal = Decimal("0"),
        target_size: Decimal = Decimal("0"),
        avg_price: Decimal = Decimal("0"),
        transaction_hash: str | None = None,
        trade_id: str | None = None,
        error: str | None = None,
    ):
        self.status = status
        self.filled_size = filled_size
        self.target_size = target_size
        self.avg_price = avg_price
        self.transaction_hash = transaction_hash
        self.trade_id = trade_id
        self.error = error

    @property
    def fill_pct(self) -> float:
        if self.target_size <= Decimal("0"):
            return 0.0
        return float(self.filled_size / self.target_size) * 100

    def __repr__(self) -> str:
        return (
            f"OrderResult(status={self.status}, "
            f"filled={self.filled_size}/{self.target_size} "
            f"({self.fill_pct:.1f}%), "
            f"avg_price={self.avg_price}, "
            f"tx={self.transaction_hash}, "
            f"trade_id={self.trade_id})"
        )


# class OrderExecutor:
#     def __init__(
#         self,
#         clob: CLOBClient,
#         default_fee_pct: float = 0.5,
#         partial_fill_min_pct: float = 50.0,
#     ):
#         self._clob = clob
#         self._default_fee_pct = Decimal(str(default_fee_pct))
#         self._partial_fill_min_pct = Decimal(str(partial_fill_min_pct))
# 
#     async def execute_fok(
#         self,
#         token_id: str,
#         side: str,
#         size: Decimal,
#         price: Decimal,
#     ) -> OrderResult:
#         order_payload = {
#             "token_id": token_id,
#             "side": side,
#             "size": str(size),
#             "price": str(price),
#             "order_type": "FOK",
#         }
#         try:
#             raw = await self._clob.place_order(order_payload)
#             return self._parse_response(raw, size, price)
#         except Exception as exc:
#             log.warning("fok_rejected", token_id=token_id, side=side, error=str(exc))
#             return OrderResult(status="rejected", error=str(exc))
# 
#     async def execute_ioc(
#         self,
#         token_id: str,
#         side: str,
#         size: Decimal,
#         price: Decimal,
#     ) -> OrderResult:
#         order_payload = {
#             "token_id": token_id,
#             "side": side,
#             "size": str(size),
#             "price": str(price),
#             "order_type": "IOC",
#         }
#         try:
#             raw = await self._clob.place_order(order_payload)
#             result = self._parse_response(raw, size, price)
# 
#             if result.status == "filled" and result.fill_pct < float(self._partial_fill_min_pct):
#                 log.warning(
#                     "partial_fill_below_threshold",
#                     fill_pct=result.fill_pct,
#                     threshold=float(self._partial_fill_min_pct),
#                 )
#                 result.status = "partial_rejected"
#                 result.error = f"partial fill {result.fill_pct:.1f}% < {float(self._partial_fill_min_pct):.1f}% threshold"
#                 return result
# 
#             return result
#         except Exception as exc:
#             log.warning("ioc_rejected", token_id=token_id, side=side, error=str(exc))
#             return OrderResult(status="rejected", error=str(exc))
# 
#     def _parse_response(
#         self,
#         raw: dict[str, Any],
#         target_size: Decimal,
#         target_price: Decimal,
#     ) -> OrderResult:
#         status = raw.get("status", "unknown")
#         filled = Decimal(str(raw.get("filled_size", raw.get("size", 0))))
#         tx_hash = raw.get("transactionHash") or raw.get("transaction_hash")
#         trade_id = raw.get("tradeId") or raw.get("trade_id")
# 
#         if status == "success" or status == "filled" or filled > Decimal("0"):
#             return OrderResult(
#                 status="filled",
#                 filled_size=filled,
#                 target_size=target_size,
#                 avg_price=target_price,
#                 transaction_hash=tx_hash,
#                 trade_id=trade_id,
#             )
#         return OrderResult(
#             status=status,
#             target_size=target_size,
#             error=raw.get("error") or raw.get("message", "unknown error"),
#         )
# 
#     async def fetch_current_fees(self) -> Decimal:
#         return self._default_fee_pct
