"""The gates between `run_crypto.py` and an order that spends real money.

Ported from PolyML's `live/arming.py`. The ladder is deliberately boring: every
gate is a boolean, all of them are evaluated, and the result names which one
refused. Gates 1-5 fall back to paper; gate 6 (the venue preflight) refuses and
exits instead.

That asymmetry matters more here than it does in PolyML. This bot's paper mode
writes the *same* `data/bucket_stats.json`, the *same* `data/bot_state.json` and
the *same* `▶ ENTER ... filled` log lines as a live run would. A silent
fall-back to paper under a live banner would be indistinguishable from real
trading -- so once the operator has cleared every deliberate gate, a broken
credential is an error, not a downgrade.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from utils.helpers import load_env


@dataclass
class Arming:
    live: bool
    gates: dict[str, bool]

    @property
    def refused_by(self) -> list[str]:
        return [k for k, v in self.gates.items() if not v]

    def __str__(self) -> str:
        if self.live:
            return "LIVE (all gates open)"
        return "PAPER (refused by: " + ", ".join(self.refused_by) + ")"


def _sdk_available() -> bool:
    try:
        import py_clob_client_v2  # noqa: F401
        return True
    except ImportError:
        return False


def check_arming(mode: str = "paper", arm: bool = False,
                 require_sdk: bool = True) -> Arming:
    """Five construction-time gates. Every one must be open to place an order."""
    load_env()
    gates = {
        "mode_live": mode == "live",
        "arm_flag": bool(arm),
        "private_key": bool(os.environ.get("POLY_PRIVATE_KEY", "").strip()),
        # os.environ ONLY -- never from .env. `load_env` refuses to load this name
        # (utils.helpers.SHELL_ONLY) because a confirmation left in a dotfile arms
        # every future run on this machine forever.
        "confirm": os.environ.get("CRYPTOSNIPER_CONFIRM_LIVE", "").strip().lower() == "yes",
        "sdk": (not require_sdk) or _sdk_available(),
    }
    return Arming(live=all(gates.values()), gates=gates)
