# TradeCryptoSniper

Bot di **paper trading** automatizzato per i mercati Polymarket "Up or Down" a finestra di 5 minuti. Strategia basata sul segnale di prezzo dal CLOB order book, sizing Kelly frazionario, risk management con circuit breaker e statistiche persistenti.

> Il bot è **solo paper trading**: non firma transazioni, non serve un wallet e non muove fondi reali. Effettua unicamente richieste in lettura alle API pubbliche Polymarket.

## Architettura

```
TradeCryptoSniper/
├── run_crypto.py            # Entry point + gestione segnali
├── config.yaml              # Configurazione
├── requirements.txt
├── start_cryptosniper.sh    # Script di avvio
├── src/
│   ├── crypto_bot.py        # Core: ciclo finestre, soglie, sizing, circuit breaker
│   ├── paper_trader.py      # Motore paper trading + statistiche per bucket
│   ├── api.py               # Client HTTP JSON con rate limiting e retry sui 429
│   ├── config.py            # Modelli Pydantic
│   └── logger.py            # Logging strutturato (structlog)
└── tests/
    └── test_logic.py        # Test di regressione sulla logica pura
```

## Strategia

### Ciclo di trading (finestre da 5 minuti)

Il bot opera su mercati del tipo `{coin}-updown-5m-{timestamp}`, dove ogni finestra di 5 minuti ha due esiti: YES (il prezzo spot sale) o NO (scende). Le coin monitorate sono BTC, ETH, SOL, XRP, DOGE.

1. **Fetch eventi** — richieste parallele all'API Gamma per i mercati delle 5 coin.
2. **Arricchimento prezzi** — mid-price (media bid/ask) dal CLOB order book, che sostituisce gli `outcomePrices` cached di Gamma. Viene richiesto **solo per le coin entro `book_enrich_margin_cents` dalla soglia**: la precisione del book non serve su una coin che quota 30¢ sotto soglia, e costa 2 richieste per coin per tick.
3. **Monitoraggio (da t-60s a t-3s)** — polling ogni 500ms, soglia di ingresso decrescente.
4. **Ingresso** — su ogni coin che supera la soglia, al massimo una posizione per coin per finestra.
5. **Skip-and-cooldown** — se nessuna coin supera la soglia, cooldown di `no_trade_cooldown_seconds` invece di forzare un trade.
6. **Risoluzione** — gestita da un task in background indipendente dal ciclo delle finestre.

### Segnale di ingresso

Il bot confronta i mid-price del book YES e NO per determinare quale lato sta vincendo. Se il lato dominante supera la soglia corrente, viene aperta una posizione su quel lato.

La soglia scala con il tempo rimanente (`CryptoBot._threshold`):

| Tempo rimanente | Soglia |
|---|---|
| > 36s | 90¢ |
| 36s → 3s | scala lineare 90¢ → 80¢ |
| ≤ 3s | nessun ingresso (`ENTRY_CUTOFF_SECONDS`) |

### Slippage model

Lo slippage è dinamico su due fattori. Base: `trading.max_slippage_pct` (1.0%).

| Liquidità | Moltiplicatore | | Tempo rimanente | Moltiplicatore |
|---|---|---|---|---|
| ≥ $10k | 1× | | > 36s | 1× |
| ≥ $1k | 1.5× | | 11–36s | 1.5× |
| ≥ $200 | 3× | | 4–10s | 2× |
| < $200 | *(rifiutato)* | | ≤ 3s | *(nessun ingresso)* |

Slippage massimo effettivamente raggiungibile: **6%** (3× × 2× × 1.0%). I livelli 5× e 3× sono guardie difensive: `min_liquidity_usd_entry` scarta la liquidità sotto $200 e il loop di ingresso si ferma a t-3s.

**Un contratto binario paga esattamente 100¢.** Se lo slippage porta il prezzo di riempimento a 100¢ o oltre, l'ingresso viene rifiutato: non esiste esito che generi profitto.

### Position sizing (Kelly frazionario)

- **Con dati sufficienti** (≥ `min_data_trades` nel bucket di prezzo): Kelly calcolato sul **prezzo di riempimento** (post-slippage), non sulla quotazione — è il prezzo che si paga davvero. Se il win rate storico non supera il prezzo di riempimento, il trade viene saltato per EV non positivo.
- **Senza dati**: fallback al 50% di `risk_per_trade_pct`.
- Si applicano poi `kelly_fraction`, `max_bet_usd_cap`, `COIN_LIQUIDITY_RANK` e `max_exposure_per_round_pct`.
- Dopo una perdita la size è ridotta di `loss_size_multiplier`, ripristinata dopo `win_streak_restore` vittorie consecutive.

### Statistiche persistenti

I win rate per bucket di prezzo sono salvati su `data/bucket_stats.json` (scrittura atomica) e ricaricati a ogni avvio. Bucket: `<78¢ | 78-89¢ | 90-94¢ | 95-99¢ | 100¢`.

Le statistiche sono indicizzate sulla **quotazione di mercato**, la stessa usata al momento del sizing — non sul prezzo di riempimento, che con lo slippage finirebbe in un bucket diverso da quello letto.

Le posizioni cancellate d'ufficio (mercato mai risolto, vedi `unresolved_max_age_seconds`) impattano il capitale ma sono escluse dalle statistiche e dai contatori di serie: dicono qualcosa sull'API, non sul segnale.

## Circuit breaker

- **Loss streak** — dopo `max_consecutive_losses` perdite consecutive: cooldown di `loss_cooldown_seconds`. Allo scadere il contatore si azzera e il bot riprende.
- **Drawdown di sessione** — se l'equity scende di `max_daily_drawdown_pct` rispetto al saldo di inizio sessione, il bot si ferma. (È un drawdown *di sessione*, non giornaliero: il riferimento è fissato all'avvio.)
- **No-trade cooldown** — nessuna coin sopra soglia in una finestra: pausa di `no_trade_cooldown_seconds`. Non accorcia mai un cooldown da loss streak già attivo.
- **Kill switch** — se esiste il file `kill_switch.lock_file_path`, il bot termina in modo pulito. Verificato sia tra le finestre sia durante il polling.

## Configurazione

Tutte le chiavi in `config.yaml` sono lette dal codice; non ci sono parametri inerti. Vedi `src/config.py` per default e tipi.

Il ladder delle soglie di ingresso (90¢ → 80¢) è in `CryptoBot._threshold`, non in configurazione.

## Installazione e avvio

```bash
git clone https://github.com/997Alex/TradeCryptoSniper.git
cd TradeCryptoSniper
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

./start_cryptosniper.sh      # oppure: python3 run_crypto.py
```

`Ctrl-C` avvia uno shutdown pulito entro pochi secondi; un secondo `Ctrl-C` termina immediatamente il processo.

## Test

```bash
python3 -m pytest tests/ -q
```

## Requisiti

- Python 3.12+
- Connessione Internet (API Polymarket Gamma + CLOB)
- Dipendenze: `httpx`, `pydantic`, `pyyaml`, `structlog` (`pytest` per i test)
