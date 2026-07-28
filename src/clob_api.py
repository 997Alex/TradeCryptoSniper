"""
from __future__ import annotations

import time
import hmac
import hashlib
from decimal import Decimal
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data

from utils.logger import get_logger

log = get_logger("clob_api")

EIP712_DOMAIN = {
    "name": "Polymarket",
    "version": "1",
    "chainId": 137,
}

EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint256"},
    ],
    "SignedOrder": [
        {"name": "order", "type": "Order"},
        {"name": "signature", "type": "bytes"},
    ],
}


def derive_api_key(private_key_hex: str) -> dict[str, str]:
    ...


class CLOBClient:
    ...
"""
