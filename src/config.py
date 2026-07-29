from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from utils.helpers import env_or_config


class ChainConfig(BaseModel):
    rpc_url: str
    poly_contracts: dict[str, str]


class PolymarketConfig(BaseModel):
    gamma_api_base: str
    clob_api_base: str
    ws_url: str
    rate_limit_rps: int = 5


class WalletConfig(BaseModel):
    private_key: str = ""
    address: str = ""

    def resolve(self) -> "WalletConfig":
        return WalletConfig(
            private_key=env_or_config(self.private_key, "POLY_PRIVATE_KEY"),
            address=env_or_config(self.address, "POLY_ADDRESS"),
        )


class TradingConfig(BaseModel):
    max_spend_per_trade_usd: float = 50
    max_slippage_pct: float = 1.0
    default_fee_pct: float = 0.5
    order_type: str = "FOK"
    partial_fill_min_pct: float = 50.0
    stale_price_max_age_ms: int = 2000
    min_price_cents: int = 90


# class ResolutionArbConfig(BaseModel):
#     enabled: bool = False
#     max_hours_since_end: int = 72
#     max_spend_per_trade_usd: float = 100.0
#     min_price_cents: int = 85
#     min_liquidity_usd: float = 100.0
#     categories: list[str] = ["Sports", "Politics"]



class Crypto5mConfig(BaseModel):
    enabled: bool = True
    execute_at_seconds: int = 20
    monitor_start_seconds: int = 60
    early_price_cents: int = 92
    final_price_cents: int = 78
    early_entry_price_cents: int = 95
    poll_interval_seconds: float = 0.5
    risk_per_trade_pct: float = 10.0
    max_bet_usd_cap: float = 6.0
    fixed_bet_usd: int = 2
    kelly_fraction: float = 0.25
    min_data_trades: int = 5
    default_win_rate: float = 0.60
    loss_size_multiplier: float = 0.50
    win_streak_restore: int = 3
    max_daily_drawdown_pct: float = 15.0
    max_consecutive_losses: int = 3
    loss_cooldown_seconds: int = 3600
    max_exposure_per_round_pct: float = 25.0
    min_resolve_buffer_seconds: int = 5
    slippage_model_pct: float = 0.5
    min_liquidity_usd_entry: float = 200.0
    liquidity_weight: float = 0.3

    # ── added: cross-round / correlation risk controls ──────────
    # Hard cap on TOTAL unresolved cost basis (all open positions, all rounds
    # combined) as a % of equity. This is what actually bounds the worst-case
    # single-shot loss from a correlated multi-coin move -- max_exposure_per_round_pct
    # alone does not, because it resets every 5-minute round while positions opened
    # in earlier rounds can still be unresolved and sitting at risk.
    max_total_exposure_pct: float = 10.0
    # Hard cap on the number of simultaneously OPEN (unresolved) positions,
    # regardless of which round opened them. BTC/ETH/SOL/XRP/DOGE 5-min direction
    # bets are highly correlated, so this is really a cap on cluster size for a
    # single correlated bet dressed up as "5 diversified coins".
    max_concurrent_open_positions: int = 4
    # Minimum required net edge in cents (after fee + slippage) that must remain
    # BEFORE a trade is allowed, and it does NOT shrink under time pressure near
    # the round deadline (the old code let this floor to 1c with 8s left, which
    # is how it ended up paying 96-99c for a coin with ~0 real edge left).
    min_net_edge_cents: int = 4
    # Extra fixed cents buffer on top of min_net_edge_cents to absorb gas / worse
    # fills than the simulated slippage model assumes when this goes live.
    gas_buffer_cents: int = 1


class KillSwitchConfig(BaseModel):
    enabled: bool = True
    lock_file_path: str = "/tmp/crypto_bot_kill.lock"
    check_interval_sec: int = 5


# class ScannerConfig(BaseModel):
#     scan_interval_sec: int = 60
#     min_liquidity_usd: float = 200
#     max_minutes_to_resolution: int = 43200
#     min_price_cents: int = 90


class PaperTradingConfig(BaseModel):
    enabled: bool = False
    initial_balance_usd: float = 100.0
    resolve_check_interval_sec: int = 60
    max_concurrent_trades: int = 10


# class WebSocketConfig(BaseModel):
#     reconnect_base_delay_sec: float = 1.0
#     reconnect_max_delay_sec: float = 30.0


# class TelegramAlertConfig(BaseModel):
#     enabled: bool = False
#     bot_token: str = ""
#     chat_id: str = ""


# class AlertsConfig(BaseModel):
#     telegram: TelegramAlertConfig = TelegramAlertConfig()
#     min_balance_usd: float = 100.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/sniper.log"


class Config(BaseModel):
    chain: ChainConfig
    polymarket: PolymarketConfig
    wallet: WalletConfig
    trading: TradingConfig
    # resolution_arb: ResolutionArbConfig = ResolutionArbConfig()
    paper_trading: PaperTradingConfig = PaperTradingConfig()
    # scanner: ScannerConfig
    crypto_5m: Crypto5mConfig = Crypto5mConfig()
    # websocket: WebSocketConfig = WebSocketConfig()
    kill_switch: KillSwitchConfig = KillSwitchConfig()
    # alerts: AlertsConfig = AlertsConfig()
    logging: LoggingConfig

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        raw: dict[str, Any] = yaml.safe_load(p.read_text())
        cfg = cls.model_validate(raw)
        cfg.wallet = cfg.wallet.resolve()
        return cfg


def load_config(path: str = "config.yaml") -> Config:
    return Config.load(path)
