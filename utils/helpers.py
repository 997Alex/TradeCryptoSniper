import os
import time
import hashlib
import tempfile
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

# A confirmation is not a credential. A line left in a dotfile arms every future
# run on this machine forever, so this name is readable from the shell only.
SHELL_ONLY = ("CRYPTOSNIPER_CONFIRM_LIVE",)


def env_or_config(config_value: str, env_key: str) -> str:
    return os.environ.get(env_key, config_value)


def load_env(path: str | Path | None = None, *, override: bool = False) -> int:
    """Populate os.environ from a .env file. Returns how many names were set.

    Stdlib only -- python-dotenv is a dependency for ~15 lines. The existing
    environment WINS by default, so a shell export or a systemd EnvironmentFile
    always overrides the file rather than the other way round.
    """
    p = Path(path) if path else Path(__file__).resolve().parents[1] / ".env"
    try:
        text = p.read_text()
    except OSError:
        return 0
    n = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if not key or key in SHELL_ONLY:
            continue
        if override or key not in os.environ:
            os.environ[key] = val
            n += 1
    return n


def atomic_write_text(path: str | Path, text: str) -> None:
    """Write via a temp file in the SAME directory, then os.replace.

    The state files are read-modify-write and are rewritten on every resolution.
    A plain write_text that is interrupted leaves invalid JSON, which the loaders
    catch and treat as "no state" -- silently resetting the balance to the
    configured starting bankroll.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# def quantize_decimal(value: float | str, precision: int = 6) -> Decimal:
#     d = Decimal(str(value))
#     return d.quantize(Decimal(10) ** -precision, rounding=ROUND_DOWN)


def now_ts() -> int:
    return int(time.time())


# def short_id(obj: Any) -> str:
#     raw = str(obj)
#     return hashlib.md5(raw.encode()).hexdigest()[:8]
