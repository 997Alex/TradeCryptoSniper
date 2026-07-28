# TradeCryptoSniper

Bot di trading automatizzato per mercati Polymarket "Up or Down" a finestra di 5 minuti, con strategia basata su segnale di prezzo dal CLOB order book, sizing Kelly, risk management e paper trading integrato.

## Architettura

```
TradeCryptoSniper/
├── run_crypto.py            # Entry point (avvio del bot 5m)
├── config.yaml              # Configurazione (chain, wallet, trading, risk)
├── requirements.txt         # Dipendenze Python
├── start_cryptosniper.sh    # Script di avvio
└── src/
    ├── crypto_bot.py        # Core: logica di trading 5m, ciclo finestre, soglie, posizioni
    ├── paper_trader.py      # Paper trading engine con persistenza statistiche
    ├── config.py            # Modelli Pydantic per la configurazione
    ├── order_executor.py    # DTO OrderResult
    ├── bot.py               # (commentato) Sniping classico — non usato per 5m
    ├── main.py              # (commentato) Entry point alternativo
    ├── clob_api.py          # (commentato) Client CLOB reale
    ├── market_scanner.py    # (commentato) Scanner mercati generali
    ├── notifier.py          # (commentato) Notifiche Telegram
    ├── price_monitor.py     # (commentato) Monitor WebSocket prezzi
    └── risk_manager.py      # (commentato) Risk management classico
└── utils/
    ├── logger.py            # Logging strutturato con structlog
    └── helpers.py           # Utility varie
```

## Strategia

### Ciclo di trading (finestre da 5 minuti)

Il bot opera su mercati Polymarket del tipo `{coin}-updown-5m-{timestamp}`, dove ogni finestra di 5 minuti ha due esiti: YES (il prezzo spot di una crypto sale) o NO (scende).

Ogni finestra segue questo ciclo:

1. **Fetch eventi** — richieste parallele all'API Gamma per ottenere i mercati delle 5 coin (BTC, ETH, SOL, XRP, DOGE)
2. **Arricchimento prezzi real-time** — per ogni mercato, fetch parallelo del CLOB order book (mid-price bid/ask) per entrambi i token YES e NO, sostituendo i cached `outcomePrices` di Gamma
3. **Monitoraggio (da t-60s a t-3s)** — polling ogni 500ms, la soglia di ingresso scende gradualmente da 90¢ a 80¢
4. **Skip-and-cooldown** — se a fine finestra nessuna coin ha superato la soglia, si applica un cooldown di 300s invece di forzare un trade a 50¢
5. **Risoluzione** — attesa risoluzione dei mercati (max 30 tentativi ogni 3s) e report di round

### Segnale di ingresso

Il bot confronta i mid-price (media bid/ask) del book YES e NO per determinare quale lato sta vincendo. Se il prezzo del lato dominante supera la soglia corrente, viene aperta una posizione su quel lato.

La soglia è dinamica in base al tempo rimanente nella finestra:
- Oltre 36s: 90¢
- 36s — 3s: scalare lineare 90¢ → 80¢
- Sotto 3s: 80¢ (non scende più sotto)

### Slippage model

Lo slippage applicato agli ordini è dinamico in base a due fattori:

**Liquidità del mercato** (dal campo `liquidity` dell'API Gamma):
| Liquidità | Multiplicatore |
|-----------|---------------|
| ≥ $10k    | 1× |
| ≥ $1k     | 1.5× |
| ≥ $200    | 3× |
| < $200    | 5× |

**Tempo rimanente** nella finestra:
| Tempo | Multiplicatore |
|-------|---------------|
| > 36s | 1× |
| 10-36s | 1.5× |
| 3-10s | 2× |
| < 3s | 3× |

Esempi: BTC con $50k liquidità a t-60s → 1.0%. DOGE con $500 liquidità a t-2s → 3 × 3 × 1.0% = 9.0%.

### Position sizing (Kelly)

Il bot usa il Criterion di Kelly frazionario per determinare la dimensione di ogni posizione:

- **Con dati sufficienti** (≥ 5 trade storici nel bucket di prezzo): calcola Kelly completo usando win rate storico e margine
- **Senza dati**: usa un fallback del 50% del `risk_per_trade_pct`
- Applica `kelly_fraction` (default 0.25) per sizing conservativo
- Applica `max_bet_usd_cap`, `max_exposure_per_round_pct`, e `COIN_LIQUIDITY_RANK`
- Riduce la size dopo una perdita (`loss_size_multiplier`) e la recupera dopo `win_streak_restore` vincite consecutive

### Statistiche persistenti

I win rate per bucket di prezzo sono salvati su disco (`data/bucket_stats.json`) e caricati a ogni avvio. I bucket sono:
- < 78¢ | 78-89¢ | 90-94¢ | 95-99¢ | 100¢

Senza dati storici, usa `default_win_rate=0.60` dal config.

## Configurazione

### Wallet

In `config.yaml`:

```yaml
wallet:
  private_key: "0x..."
  address: "0x..."
```

Oppure tramite variabili d'ambiente `POLY_PRIVATE_KEY` e `POLY_ADDRESS`.

### Paper trading (default)

```yaml
paper_trading:
  enabled: true           # sempre true per paper trading
  initial_balance_usd: 100
```

### Parametri strategia 5m

```yaml
crypto_5m:
  enabled: true
  execute_at_seconds: 20
  monitor_start_seconds: 60
  poll_interval_seconds: 0.5
  risk_per_trade_pct: 10
  max_bet_usd_cap: 50
  kelly_fraction: 0.25
  max_daily_drawdown_pct: 15.0
  max_consecutive_losses: 3
  loss_cooldown_seconds: 3600
```

### Circuit breaker

- **Loss streak**: dopo 3 perdite consecutive, cooldown di 1 ora
- **Drawdown**: se il drawdown giornaliero supera il 15%, stop
- **Kill switch**: file lock `/tmp/crypto_bot_kill.lock` — se presente, il bot si ferma
- **No-trade cooldown**: se nessuna coin supera la soglia in una finestra, cooldown di 300s

## Installazione

```bash
git clone https://github.com/997Alex/TradeCryptoSniper.git
cd TradeCryptoSniper
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Avvio

```bash
./start_cryptosniper.sh
# oppure manualmente:
source venv/bin/activate
cd TradeCryptoSniper
python3 run_crypto.py
```

## Requisiti

- Python 3.12+
- Connessione Internet (API Polymarket Gamma + CLOB)

## File non utilizzati per la strategia 5m

I file `bot.py`, `main.py`, `clob_api.py`, `market_scanner.py`, `notifier.py`, `price_monitor.py`, `risk_manager.py` sono commentati e non attivi. Appartengono a una versione precedente del bot con strategia di sniping generico su tutti i mercati Polymarket (incluso resolution arb). Sono mantenuti per reference.

## Dipendenze

- `pyyaml` — parsing config
- `httpx` — API HTTP asincrone
- `pydantic` — validazione configurazione
- `structlog` — logging strutturato
- `eth-account`, `web3` — firma transazioni (per modalità live)
- `tenacity` — retry logic
