from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class PolymarketConfig(BaseModel):
    gamma_api_base: str
    clob_api_base: str
    rate_limit_rps: float = 20.0


class TradingConfig(BaseModel):
    max_slippage_pct: float = 1.0
    stale_price_max_age_ms: int = 2000


class Crypto5mConfig(BaseModel):
    execute_at_seconds: int = 20
    monitor_start_seconds: int = 60
    poll_interval_seconds: float = 0.5
    book_enrich_margin_cents: int = 10
    risk_per_trade_pct: float = 10.0
    max_bet_usd_cap: float = 50.0
    kelly_fraction: float = 0.25
    min_data_trades: int = 5
    default_win_rate: float = 0.60
    loss_size_multiplier: float = 0.50
    win_streak_restore: int = 3
    max_daily_drawdown_pct: float = 15.0
    max_consecutive_losses: int = 3
    loss_cooldown_seconds: int = 3600
    no_trade_cooldown_seconds: int = 300
    max_exposure_per_round_pct: float = 25.0
    min_liquidity_usd_entry: float = 200.0
    unresolved_max_age_seconds: int = 900


class KillSwitchConfig(BaseModel):
    enabled: bool = True
    lock_file_path: str = "/tmp/crypto_bot_kill.lock"


class PaperTradingConfig(BaseModel):
    initial_balance_usd: float = 100.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/crypto_bot.log"


class Config(BaseModel):
    polymarket: PolymarketConfig
    trading: TradingConfig = TradingConfig()
    paper_trading: PaperTradingConfig = PaperTradingConfig()
    crypto_5m: Crypto5mConfig = Crypto5mConfig()
    kill_switch: KillSwitchConfig = KillSwitchConfig()
    logging: LoggingConfig = LoggingConfig()

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        raw: dict[str, Any] = yaml.safe_load(p.read_text())
        return cls.model_validate(raw)


def load_config(path: str = "config.yaml") -> Config:
    return Config.load(path)
