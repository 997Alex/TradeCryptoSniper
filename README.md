# TradeCryptoSniper

Bot di trading automatizzato per i mercati Polymarket **"Up or Down"** con finestra di 5 minuti su
BTC, ETH, SOL, XRP e DOGE.

Il bot compra il lato favorito di un mercato binario quando il suo prezzo entra in una banda
ristretta, tiene la posizione fino alla risoluzione (non esiste alcuna logica di uscita anticipata)
e incassa $1.00 per share se vince, $0 se perde.

> **Stato attuale: paper trading.** Il percorso di esecuzione live esiste ed è completo, ma è
> disarmato tramite cinque cancelli indipendenti. Nessun ordine reale può partire senza un'azione
> esplicita da shell. Vedi [Modalità live](#modalità-live-armamento).

---

## Indice

1. [Come funziona la strategia](#come-funziona-la-strategia)
2. [Il problema della cache CDN](#il-problema-della-cache-cdn-leggere-prima-di-tutto)
3. [Meccanismi di sicurezza](#meccanismi-di-sicurezza)
4. [Architettura dei file](#architettura-dei-file)
5. [Installazione](#installazione)
6. [Deploy con systemd](#deploy-con-systemd)
7. [Operatività quotidiana](#operatività-quotidiana)
8. [Modalità live (armamento)](#modalità-live-armamento)
9. [Configurazione completa](#configurazione-completa)
10. [Strumenti di analisi](#strumenti-di-analisi)
11. [Risultati misurati e limiti](#risultati-misurati-e-limiti)

---

## Come funziona la strategia

### Il ciclo della finestra

Ogni finestra dura 300 secondi. Lo slug del mercato è `{coin}-updown-5m-{window_ts}`, dove
`window_ts` è l'unix timestamp arrotondato per difetto a multipli di 300.

| Momento | Cosa succede |
|---|---|
| inizio finestra | `run()` rileva il nuovo confine, verifica i circuit breaker e il kill switch |
| `t-20s` o oltre | finestra scartata: "too late, skipping" (`execute_at_seconds`) |
| fino a `t-90s` | attesa (`monitor_start_seconds`) |
| `t-90s` → `t-15s` | **ciclo di ingresso**, polling ogni 0.5 s |
| `t-15s` → `t-3s` | nessun ingresso nuovo (guardia `min_entry_seconds_left`), solo telemetria |
| dopo la chiusura | fino a 30 tentativi di risoluzione ogni 3 s, poi report di round |

### Il segnale

Il prezzo viene letto dal campo `outcomePrices` dell'API Gamma e convertito in centesimi interi
(troncamento: `0.889` → `88`). Il lato con il prezzo più alto è il favorito; in caso di parità
esatta la coin viene saltata.

**Non c'è alcun modello predittivo.** Il bot non stima la direzione: compra il favorito così come
lo prezza il mercato. L'unica "previsione" è implicita nel prezzo stesso.

### Il cancello di ingresso basato sul win rate

```
wr        = bucket_win_rate(prezzo)        # storico persistito, default 0.60
breakeven = int(wr * 100)
max_entry = breakeven - safety_margin_cents
if remaining_s <= 10:  max_entry += 2      # irraggiungibile con min_entry_seconds_left: 15
elif remaining_s <= 36: max_entry += 1     # attivo per ogni ingresso fra t-36s e t-15s
max_entry = clamp(max_entry, entry_price_cents_low, entry_price_cents_high)
```

**Attenzione — questo è il comportamento più frainteso del bot.** Il clamp porta `max_entry`
**verso l'alto** fino a `entry_price_cents_low`. Poiché la condizione di ingresso è
`entry_price_cents_low <= prezzo <= max_entry`, con un win rate basso la banda ammissibile è un
**singolo valore**, non un intervallo:

| win rate del bucket | banda (t > 36 s) | banda (t-36s → t-15s, bonus +1) |
|---|---|---|
| 0.60 (default, nessuno storico) | **85¢** soltanto | **85¢** soltanto |
| 0.71 | **85¢** soltanto | **85¢** soltanto |
| 0.90 | **85¢** soltanto | 85-86¢ |
| 0.91 | 85-86¢ | 85-87¢ |
| 0.92 | 85-87¢ | 85-88¢ |
| 0.94 | 85-89¢ | 85-90¢ |
| 1.00 | 85-95¢ | 85-95¢ |

Serve un win rate **≥ 0.91** perché la banda si apra (**≥ 0.90** nel tratto t-36s → t-15s, dove scatta il bonus +1). Di conseguenza il bot alterna due regimi: o
compra a un solo prezzo, oppure — dopo una serie perfetta — compra su tutta la banda ai prezzi
peggiori. Una singola perdita richiude tutto. Il file di config si chiama `100wr` proprio per
questo: opera in ampiezza solo finché il record è perfetto.

> `min_data_trades` è presente in `config.py` e nei file YAML ma **non è letto da nessuna riga di
> codice**. Era la protezione destinata a impedire che un solo trade vinto aprisse la banda, e non
> è mai stata collegata.

### Conferma a 3 poll

Il prezzo deve restare in banda per 3 poll consecutivi. **Questo filtro è molto più debole di
quanto sembri**: l'origine Gamma aggiorna `outcomePrices` circa ogni 15 secondi, mentre il ciclo
interroga ogni 0.5 s. Le tre "conferme" sono quindi la stessa identica osservazione contata tre
volte, a 1.5 secondi di distanza. Da qui la necessità del guard anti-spike (vedi sotto).

### Sizing

Puntata fissa, non Kelly:

```
invest = fixed_bet_usd
invest = min(invest, max_bet_usd_cap)
invest *= COIN_LIQUIDITY_RANK[coin]          # btc 1.0, eth 0.9, sol 0.7, xrp 0.5, doge 0.4
invest *= post_loss_reduction                # 0.50 dopo una perdita
invest /= (1 + posizioni_aperte)             # smorzamento di correlazione
invest = round_half_up(invest, 0)            # dollari interi
invest = min(invest, esposizione_residua_round, esposizione_residua_globale)
invest = max(invest, min_bet_usd)            # <-- applicato PER ULTIMO
size   = invest / prezzo
```

> **Il floor `min_bet_usd` è applicato dopo ogni cap**, quindi li scavalca tutti. Con
> `fixed_bet_usd: 5` e `min_bet_usd: 5` ogni moltiplicatore sopra è matematicamente inerte: ogni
> puntata vale esattamente $5.

### Modello di slippage

`slippage = max_slippage_pct × moltiplicatore_liquidità × moltiplicatore_tempo`

| Liquidità | × | | Tempo residuo | × |
|---|---|---|---|---|
| ≥ $10.000 | 1.0 | | > 36 s | 1.0 |
| ≥ $1.000 | 1.5 | | 11-36 s | 1.5 |
| ≥ $200 | 3.0 | | 4-10 s | 2.0 |
| < $200 | 5.0 | | ≤ 3 s | 3.0 |

Con `min_entry_seconds_left: 15` i moltiplicatori 2.0 e 3.0 **non sono raggiungibili** da un
ingresso: restano visibili solo nella telemetria di fine finestra.

Esempio reale misurato: SOL con book sottile a `t-6s` → `1.0 × 3 × 2 = 6%` → una decisione presa a
88¢ è stata registrata a **93¢**.

### Uscita

**Non esiste.** Nessuno stop loss, nessun take profit, nessuna vendita. Ogni posizione è tenuta
fino alla risoluzione. Una risoluzione è riconosciuta solo se il mercato è `closed == true` **e**
un esito è ≥ 0.999 mentre l'altro è ≤ 0.001. In pratica ciò avviene **2-4 minuti dopo** la fine
della finestra, quindi il riepilogo di round mostra quasi sempre posizioni non risolte, e il
monitor in background le contabilizza poco dopo.

### Circuit breaker

- **Drawdown**: misurato sulla **cassa**, che è già decurtata dalle posizioni aperte. Con
  l'esposizione al 10% questo consuma da solo ~10 dei 15 punti disponibili, quindi ~2 perdite nette
  fanno scattare la sospensione. Sembra un blocco, non lo è.
- **Perdite consecutive**: 3 perdite → `loss_cooldown_seconds` (1 ora di stop).
- **Kill switch**: se il file indicato da `kill_switch.lock_file_path` esiste, il bot non apre nuove
  finestre.

---

## Il problema della cache CDN (leggere prima di tutto)

L'API Gamma restituisce `/events` con l'header:

```
cache-control: public, max-age=300
```

**Un TTL di 300 secondi su un mercato che dura 300 secondi.** Senza contromisure il bot interroga
~150-175 volte per coin in una finestra (~800 richieste sulle 5 coin) e riceve sempre lo stesso
identico snapshot, congelato all'inizio della finestra.

Misurazione effettuata: tutte e 5 le coin con `outcomePrices` **identici byte per byte per 115
secondi consecutivi** (BTC fermo a 68¢, XRP e DOGE a 51¢) mentre l'order book CLOB reale si muoveva
`0.36 → 0.28 → 0.22` in 12 secondi. Zero campioni su 115 entravano nella banda di ingresso: il bot
non avrebbe eseguito **nessun trade**.

**Soluzione applicata**: un parametro variabile `_=<time.time_ns()>` sulle due GET verso Gamma
(`/events` in `crypto_bot.py` e `/markets/{id}` in `paper_trader.py` — anch'esso `max-age=300`,
altrimenti le risoluzioni restano invisibili fino a 5 minuti).

- `cf-cache-status: MISS` a ogni chiamata, prezzi che si muovono di nuovo (BTC attraversa 8 valori
  distinti per finestra).
- L'header `Cache-Control: no-cache` **non funziona**: la risposta resta `HIT`.
- Nessun rate limit osservato alla cadenza del bot: 150 richieste a 7.4 req/s → 150 × HTTP 200.

---

## Meccanismi di sicurezza

Tre guardie additive, tutte disattivabili impostando il valore a `0`. Ogni blocco viene loggato con
la sua motivazione.

### 1. `max_effective_entry_cents` — limite sul prezzo realmente pagato

Il filtro preesistente limita il prezzo **quotato**; nulla limitava quello **effettivamente
pagato**. Questa guardia calcola il prezzo dopo slippage — con la stessa aritmetica esatta di
`PaperTrader._paper_execute`, verificata su 42 combinazioni prezzo × slippage — e rifiuta se supera
la soglia.

> **Nota importante sull'accoppiamento**: questa soglia agisce sul prezzo *slippato*, mentre
> `entry_price_cents_low` agisce su quello *quotato*. Con il pavimento a 88¢ e il cap a 88¢ passa
> **solo il caso di slippage minimo** (1.0%: book ≥ $10.000 e più di 36 s residui), che arriva
> esattamente a 88¢: ogni altro scenario supera il cap. In pratica gli ingressi si riducono a
> pochissimi fill nel migliore dei casi. È per questo che il pavimento è stato abbassato a 85¢. Cambiando uno dei due valori, ricontrollare l'altro.

### 2. `max_price_jump_cents` — guard anti-spike

Rifiuta l'ingresso se il prezzo è **balzato** in banda invece di stabilizzarsi, confrontandolo con
la precedente osservazione **distinta** (non con il campione precedente, che sarebbe identico a
causa del refresh a ~15 s). Rifiuta anche un ribaltamento del lato favorito a metà finestra.

`price_settle_seconds` (20 s, più di un ciclo di refresh) impedisce che la guardia si **incastri**:
un prezzo che regge invariato per quel tempo diventa il proprio riferimento e torna negoziabile.
Senza questo parametro un prezzo stabile da 60 secondi restava rifiutato all'infinito, perché
"stabilizzarsi" non produce alcun campione nuovo.

### 3. `min_entry_seconds_left` — divieto di ingresso a fine finestra

Con un refresh dell'origine a ~15 s, una quotazione negli ultimi 15 secondi può essere **più vecchia
del tempo che manca alla risoluzione**. Elimina inoltre i moltiplicatori di slippage 2× e 3×, che
sono ciò che ha trasformato una decisione a 88¢ in un riempimento a 93¢.

---

## Architettura dei file

```
TradeCryptoSniper/
├── run_crypto.py                    # entry point
├── config_conservativa_100wr.yaml   # config di default
├── config.yaml                      # config alternativa
├── requirements.txt
├── .env                             # credenziali live (NON committato)
├── src/
│   ├── crypto_bot.py                # strategia completa: finestre, cancello, sizing, guardie
│   ├── paper_trader.py              # ledger posizioni, fill simulati, risoluzioni, statistiche
│   ├── config.py                    # modelli Pydantic
│   ├── order_executor.py            # DTO OrderResult
│   ├── arming.py                    # i 5 cancelli fra il bot e un ordine reale
│   └── live_executor.py             # esecuzione reale via py-clob-client-v2
├── utils/
│   ├── logger.py                    # structlog su file + stdout
│   └── helpers.py                   # load_env, atomic_write_text
└── tools/
    └── approach.py                  # analisi offline dei log (non usato dal bot)
```

I file `bot.py`, `main.py`, `clob_api.py`, `market_scanner.py`, `notifier.py`, `price_monitor.py`,
`risk_manager.py` sono **interamente racchiusi in stringhe triple**: non definiscono nulla e non
vengono mai importati. Appartengono a una versione precedente.

### Stato persistito

| File | Contenuto | Chi scrive |
|---|---|---|
| `data/bucket_stats.json` | win rate per bucket di prezzo — **unico input del cancello di ingresso** | `PaperTrader` a ogni risoluzione |
| `data/bot_state.json` | cassa, perdite consecutive, cooldown | `PaperTrader` e `CryptoBot` |
| `logs/crypto_bot.log` | log strutturato (percorso fisso in `run_crypto.py`) | `structlog` |

Entrambi i file di stato sono scritti in modo **atomico** (file temporaneo + `fsync` +
`os.replace`). Una scrittura interrotta produrrebbe JSON non valido, che i loader interpretano come
"nessuno stato" — azzerando silenziosamente il saldo.

> **Se si cambiano i parametri del cancello, cancellare `data/bucket_stats.json`.** È l'unica
> memoria di lungo periodo del bot e statistiche raccolte sotto regole diverse restano valide come
> JSON pur essendo prive di significato.

---

## Installazione

Requisiti: **Python 3.11**, `git`, e (consigliato) [`uv`](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:997Alex/TradeCryptoSniper.git
cd TradeCryptoSniper
git checkout ls_dev

uv venv --python 3.11 venv
uv pip install --python venv/bin/python -r requirements.txt

mkdir -p logs data
```

Senza `uv`:

```bash
python3.11 -m venv venv
venv/bin/pip install -r requirements.txt
```

Verifica che la configurazione sia caricabile:

```bash
venv/bin/python -c "from src.config import load_config; \
  print(load_config('config_conservativa_100wr.yaml').crypto_5m)"
```

> Lo script `start_cryptosniper.sh` è **obsoleto**: punta a `/home/alex/cryptosnipervenv`, un
> percorso che non esiste. Usare systemd (sotto) oppure `venv/bin/python run_crypto.py` dalla
> radice del repository.

---

## Deploy con systemd

Il bot va eseguito come **unità utente systemd**: sopravvive alla disconnessione SSH, riparte da
solo in caso di crash e scrive i log su file.

Creare `~/.config/systemd/user/cryptosniper.service`:

```ini
[Unit]
Description=TradeCryptoSniper 5m crypto up/down (paper salvo armamento)

[Service]
Type=simple
WorkingDirectory=/home/ubuntu/projects/TradeCryptoSniper
# %t = XDG_RUNTIME_DIR, che è tmpfs. File assente = paper mode.
# Un riavvio della macchina disarma quindi automaticamente il bot.
EnvironmentFile=-%t/cryptosniper-live.env
ExecStart=/home/ubuntu/projects/TradeCryptoSniper/venv/bin/python run_crypto.py
Restart=always
RestartSec=30
# SIGTERM -> stop pulito. Il ciclo può impiegare fino a ~6 minuti per uscire
# da una finestra: un timeout più corto ucciderebbe il processo a metà,
# lasciando la cassa decurtata per posizioni che non si risolveranno mai.
KillSignal=SIGTERM
TimeoutStopSec=600
StandardOutput=append:/home/ubuntu/projects/TradeCryptoSniper/logs/service.log
StandardError=append:/home/ubuntu/projects/TradeCryptoSniper/logs/service.log

[Install]
WantedBy=default.target
```

Adattare i tre percorsi assoluti se il repository si trova altrove.

```bash
systemctl --user daemon-reload
systemctl --user start cryptosniper
systemctl --user status cryptosniper
```

**Non usare `systemctl --user enable`** per una sessione di test: un riavvio non deve far ripartire
silenziosamente un bot che nessuno sta guardando. `Restart=always` copre comunque i crash.

Per sopravvivere alla disconnessione SSH serve il linger:

```bash
loginctl enable-linger "$USER"
```

### Arresto pulito

L'ordine conta. Il kill switch viene controllato **fra una finestra e l'altra**, quindi la finestra
in corso viene completata:

```bash
touch /home/ubuntu/projects/TradeCryptoSniper/KILL
# attendere che non compaiano più righe ▶ ENTER / filled e che l'ultimo
# conteggio "open positions: N" sia 0 (di norma < 5 minuti)
systemctl --user stop cryptosniper
rm /home/ubuntu/projects/TradeCryptoSniper/KILL     # altrimenti non riparte
```

Fermare il servizio con posizioni ancora aperte **gonfia il saldo**: la cassa è decurtata
all'ingresso ma persistita solo alla risoluzione.

---

## Operatività quotidiana

```bash
# log in tempo reale
tail -f logs/crypto_bot.log

# il servizio è ripartito da solo? (deve restare 0)
systemctl --user show cryptosniper -p NRestarts -p SubState --value

# conteggio ingressi e risultati
grep -c '▶ ENTER' logs/crypto_bot.log
grep -E '✓|✗' logs/crypto_bot.log | tail -20

# quali guardie hanno bloccato cosa
grep 'blocked:' logs/crypto_bot.log | tail -20

# statistiche persistite
python3 -m json.tool data/bucket_stats.json
```

**Attenzione ai fusi orari**: `utils/logger.py` usa l'ora **locale**, mentre le etichette di
finestra usano **UTC**. Un round loggato alle `10:30` mostrerà `ROUND n | 08:30 → 08:35`. Non è un
bug.

### Cosa controllare

| Segnale | Significato |
|---|---|
| prezzi che **non cambiano** dentro una finestra | la cache CDN è tornata: il cache-buster non funziona più |
| `fetch_non_200` frequenti | rate limiting dell'origine; il fallimento parziale è insidioso perché `events.update(fresh)` conserva il dato vecchio della coin fallita |
| `circuit breaker: drawdown_...` | atteso, non è un guasto (vedi la nota sul drawdown misurato sulla cassa) |
| posizioni aperte da > 10 minuti | risoluzione bloccata: il cap di esposizione impedirà ogni nuovo trade, in silenzio |
| `NRestarts` > 0 | il baseline del drawdown si è riazzerato al riavvio; i numeri successivi vanno interpretati con cautela |

---

## Modalità live (armamento)

> **Il paper trading scrive le stesse identiche righe di log, le stesse statistiche e lo stesso
> file di stato di una sessione reale.** All'avvio la modalità è dichiarata dalla riga
> `execution_mode`; a regime l'unica differenza osservabile è la riga `LIVE FILL … order=…`, che il
> paper non emette mai. Righe ENTER/filled, statistiche e file di stato sono invece identici.

### Prerequisiti

`.env` nella radice del repository (già in `.gitignore`, permessi `600`):

```bash
POLY_PRIVATE_KEY=<64 caratteri esadecimali, senza prefisso 0x>
POLY_FUNDER_ADDRESS=0x<indirizzo del wallet che detiene il collaterale>
POLY_SIGNATURE_TYPE=3
```

`POLY_SIGNATURE_TYPE` non va copiato a caso: **è un'affermazione su quale wallet il venue
addebiterà**. `3` = POLY_1271, il DepositWallet ERC-1271 creato dalla registrazione attuale. Il
preflight interroga il venue, e se il tipo configurato vede saldo zero prova gli altri e indica
quello giusto.

SDK di esecuzione:

```bash
uv pip install --python venv/bin/python py-clob-client-v2
```

> Il nome è **`py-clob-client-v2`**, non `py-clob-client`: sono due pacchetti PyPI diversi. La v1 è
> archiviata e punta al vecchio exchange USDC.e.

### I cinque cancelli

Devono essere **tutti** aperti. Se uno manca, il bot resta in paper e dichiara quale ha rifiutato.

| # | Cancello | Come si apre |
|---|---|---|
| 1 | `mode_live` | argomento `live` sulla riga di comando |
| 2 | `arm_flag` | argomento `--arm` |
| 3 | `private_key` | `POLY_PRIVATE_KEY` presente in `.env` |
| 4 | `confirm` | `CRYPTOSNIPER_CONFIRM_LIVE=yes` — **solo da shell, mai da `.env`** |
| 5 | `sdk` | `py-clob-client-v2` installato |
| 6 | preflight | il venue conferma collaterale sotto la firma configurata — **se fallisce esce con errore, non ripiega su paper** |

Il cancello 4 è deliberatamente non caricabile da file: una conferma lasciata in un dotfile
armerebbe per sempre ogni esecuzione futura su quella macchina.

### Procedura di armamento

```bash
# 1. modificare ExecStart nell'unit per aggiungere gli argomenti:
#    ExecStart=/.../venv/bin/python run_crypto.py live --arm
systemctl --user daemon-reload

# 2. scrivere la conferma su tmpfs (sparisce al riavvio della macchina)
umask 077
printf 'CRYPTOSNIPER_CONFIRM_LIVE=yes\n' > "$XDG_RUNTIME_DIR/cryptosniper-live.env"

# 3. avviare e VERIFICARE la riga di modalità
systemctl --user restart cryptosniper
grep execution_mode logs/crypto_bot.log | tail -1
#   deve riportare: mode='LIVE (all gates open)'
```

### Disarmo

```bash
touch KILL
# attendere il drenaggio delle posizioni
systemctl --user stop cryptosniper
rm -f "$XDG_RUNTIME_DIR/cryptosniper-live.env"
rm -f KILL
```

### Differenze in live

- Ordini **FOK** (fill-or-kill), non GTC: questi mercati si risolvono in meno di 2 minuti e un
  ordine appoggiato sul book diventerebbe una posizione orfana.
- Il venue richiede **share intere** e un minimo di 5 (`orderMinSize`). A 85¢: `5 / 0.85 = 5.88 →
  5 share = $4.25`, quindi il nozionale reale è inferiore all'intenzione.
- Il prezzo di riempimento viene letto da `makingAmount / takingAmount` nella risposta del venue,
  **non** assunto pari al limite.
- Commissioni reali: `taker_base_fee = 1000` bps con formula `baseRate × min(p, 1−p) × share`, cioè
  ~1.2¢/share a 88¢. **Il ledger di paper non le addebita**, quindi i risultati simulati sono
  ottimistici.

---

## Configurazione completa

Il file caricato di default è `config_conservativa_100wr.yaml`, sovrascrivibile con la variabile
d'ambiente `CRYPTOSNIPER_CONFIG`.

```yaml
paper_trading:
  initial_balance_usd: 228        # letto SOLO se data/bot_state.json è assente:
                                  # per riallineare il bankroll va cancellato anche quello

trading:
  max_slippage_pct: 1.0           # base del modello di slippage
  default_fee_pct: 0.5            # usato SOLO nel filtro di costo, non addebitato

crypto_5m:
  monitor_start_seconds: 90       # inizio monitoraggio; la finestra di ingresso reale è
                                  # 90 − min_entry_seconds_left = 75 s
  execute_at_seconds: 20
  poll_interval_seconds: 0.5      # accoppiato alla conferma a 3 poll: non toccare isolatamente
  min_resolve_buffer_seconds: 5

  fixed_bet_usd: 5
  min_bet_usd: 5                  # applicato PER ULTIMO: scavalca ogni cap
  max_bet_usd_cap: 12

  default_win_rate: 0.60          # prior a freddo: determina la banda finché non c'è storico
  safety_margin_cents: 5
  entry_price_cents_low: 85       # con clamp verso l'alto -> banda a singolo valore
  entry_price_cents_high: 95

  max_effective_entry_cents: 88   # guardia 1 — prezzo realmente pagato
  max_price_jump_cents: 5         # guardia 2 — anti-spike
  price_settle_seconds: 20        # guardia 2 — soglia di "stabilizzato"
  min_entry_seconds_left: 15      # guardia 3 — divieto di fine finestra

  max_daily_drawdown_pct: 15.0    # misurato sulla CASSA, non sull'equity
  max_consecutive_losses: 3
  loss_cooldown_seconds: 3600
  loss_size_multiplier: 0.50
  win_streak_restore: 3
  max_total_exposure_pct: 10.0    # % dell'equity, su tutti i round
  max_exposure_per_round_pct: 10.0 # % della CASSA, per singolo round
  max_concurrent_open_positions: 4
  min_liquidity_usd_entry: 200.0
  min_net_edge_cents: 4
  gas_buffer_cents: 1

kill_switch:
  enabled: true
  lock_file_path: "/home/ubuntu/projects/TradeCryptoSniper/KILL"   # NON in /tmp
```

Impostare a `0` una qualsiasi delle guardie ripristina il comportamento originale dell'autore.

---

## Strumenti di analisi

### `tools/approach.py`

Ricostruisce ogni trade dai log e mette alla prova un filtro d'ingresso contro lo storico reale.
Non tocca né il bot né il suo stato: legge solo `logs/*.log`, quindi può girare anche a bot acceso.

```bash
python3 tools/approach.py                  # solo il run corrente
python3 tools/approach.py logs/*.log       # anche i run archiviati
```

Produce tre blocchi:

1. **Forma di avvicinamento** — come il prezzo del lato comprato è arrivato in banda: salendo,
   scendendo, o fermo. Poi quanto sarebbe costato, in volume e in PnL, un filtro su quella forma.
2. **Drawdown post-ingresso** — la quotazione minima del lato detenuto finché restavano ≥15 s, e il
   PnL controfattuale di uno stop-loss a varie soglie. **Ipotetico**: nel bot non esiste alcuna
   uscita anticipata, e la quotazione è un mid, non un bid.
3. **Totali** — W/L, win rate e intervallo di Wilson al 95%.

### Due trappole nei dati, entrambe già gestite dallo script

Chi riscrive questa analisi da zero ci cade quasi sempre. Sono documentate nel codice:

- **Non tutte le risoluzioni scrivono una riga `✓`/`✗`.** Due percorsi di codice chiudono una
  posizione e solo `_resolution_monitor` logga. Appaiare i risultati agli ingressi in ordine FIFO per
  `(coin, side)` è quindi **sbagliato**: una riga mancante sposta di uno tutti gli esiti successivi
  di quella coin. Lo script àncora invece ogni risultato alla **finestra** di 5 minuti in cui la
  posizione è stata aperta, e se più di un ingresso risulta compatibile **rifiuta di scegliere**,
  lasciando `UNKNOWN`: tirare a indovinare è esattamente ciò che produce l'errore. La latenza di
  risoluzione osservata è 82–263 s, ma nulla nel bot la limita, quindi la finestra di accettazione
  resta larga (900 s) e l'ambiguità viene gestita anziché ignorata.
  `data/bucket_stats.json` resta la fonte autorevole per i conteggi aggregati; le righe di log ne
  sono un sottoinsieme, e lo scarto è riportato come `unresolved/unlogged`.
- **I campioni di prezzo vanno delimitati alla finestra.** Uno scan non delimitato dopo l'ingresso
  prosegue nel round successivo, dove la stessa `(coin, side)` è un mercato diverso e quota
  regolarmente 0¢: senza il limite ogni posizione sembra scesa a zero.

### L'intervallo di confidenza va calcolato con Wilson

Con win rate vicini al 100% l'approssimazione normale collassa e mente. A W27/L1 dichiarava
`[90%, 100%]` — sopra il breakeven — mentre il limite inferiore reale era **82.3%**. Lo script usa
Wilson. **Il numero che decide il go-live è il limite inferiore, non la stima puntuale.**

---

## Risultati misurati e limiti

### L'aritmetica del pareggio

A 88¢ una vittoria guadagna 12¢ e una sconfitta costa 88¢. Il **breakeven è quindi ~88%**, e ~89%
includendo commissioni e slippage. Con un fill a 93¢ sale a ~94%.

**Un win rate del 70% qui perde denaro**: `0.70 × 12 − 0.30 × 88 = −18¢ per trade`. È un ottimo
numero su un mercato equilibrato e pessimo su un favorito a 88¢. Valutare la strategia sul **PnL**,
non sul win rate.

### Misurazioni su sessione paper

| | senza guardie | con guardie |
|---|---|---|
| trade | 9 | 8 (7 risolti) |
| win rate | 67% | 100% |
| PnL | **−$12.03** | **+$5.46** |
| markup medio di slippage | +1.9¢ | +0.9¢ |
| fill sopra 88¢ | 6 su 9 | **0 su 8** |
| bucket `90-94¢` | 6 trade, **−$8.24** | vuoto |

### Sessione paper del 2026-08-03 (snapshot alle 15:51 CEST)

Bankroll iniziale $228, bet fissa $5, config `config_conservativa_100wr.yaml`.

| | valore |
|---|---|
| round (finestre elaborate) | 68 |
| finestre con almeno un ingresso | 37 (**54%**; il bot passa sul 46% delle finestre) |
| ingressi | 45 |
| risolti | 41 |
| record | **W38 / L3** |
| win rate | **92.7%**, Wilson 95% **[80.6%, 97.5%]** |
| PnL | **+$13.44** (cassa $241.44) |
| interventi del circuit breaker | **0** |
| riavvii | **1**, alle 10:56:41 |

⚠️ **Il riavvio va contato.** Il processo è partito alle 10:11:41, è stato fermato alle 10:56:17 e
ripartito alle 10:56:41, **scartando 1 posizione aperta** (`paper_open_positions_dropped count=1`,
un ETH YES 87¢ che non si è mai risolto e non compare in nessuna statistica). Lo span 10:11→15:51
è quindi di 5h 39m ma **non è continuo**; il processo corrente ha 4h 54m di uptime ininterrotto.

`systemctl show -p NRestarts` riporta **0** e non contraddice quanto sopra: conta solo i riavvii
*automatici* decisi da systemd dopo un fallimento, non un `systemctl restart` manuale. Per l'uptime
reale usare `ActiveEnterTimestamp`, o le righe `crypto_bot_started` nel log. Un monitor che riporta
solo `NRestarts` dichiara "0 riavvii" attraverso un riavvio manuale che ha perso una posizione.

Il PnL è positivo e il win rate è sopra il breakeven, ma **il limite inferiore a 80.6% è ancora sotto
il breakeven di ~89%**: con 3 sconfitte l'intervallo contiene ancora scenari in perdita. Non è un
risultato, è un campione ancora troppo piccolo.

### Nessun ulteriore filtro d'ingresso è giustificato dai dati

Dopo la terza sconfitta sono state cercate sistematicamente altre guardie d'ingresso: cinque ipotesi
indipendenti (accordo fra le coin, volatilità del percorso pre-ingresso, comportamento del cancello
win-rate ai confini dei bucket, orario d'ingresso, coerenza delle quotazioni `Y+N`), ognuna misurata
in volume e PnL e poi sottoposta a confutazione avversariale. **Nessuna è sopravvissuta.** Ognuna
bloccava fra il 20% e il 57% del volume, oppure invertiva di segno passando da un log all'altro,
oppure cambiava segno togliendo una sola sconfitta dal campione.

Le due sconfitte più istruttive hanno forme **opposte**:

| | ingresso | percorso | esito |
|---|---|---|---|
| DOGE NO 86¢ (14:08) | in banda **scendendo** da 91¢ | 86 → 67 → 54 → 11¢ | persa |
| BTC YES 85¢ (14:29) | in banda **salendo** da 71¢ | 85 → 85 → 85 → 55¢ | persa |

Qualunque filtro sulla direzione di avvicinamento ne avrebbe bloccata una e lasciata passare
l'altra. Il percorso pre-ingresso del DOGE era `91-91`: escursione zero, zero cambi di direzione,
la lettura **più tranquilla** dell'intero dataset. All'ingresso è indistinguibile da 21 trade vinti.

**L'aritmetica della potenza statistica è il vero vincolo.** Con un tasso di sconfitta base del 12%
e un sottogruppo che copra un quarto dei trade:

| effetto reale | n=44 | n=200 | n=400 | n=800 |
|---|---|---|---|---|
| tasso di perdita ×2 | 0.11 | 0.46 | 0.74 | 0.95 |
| tasso di perdita ×3 | 0.23 | 0.83 | 0.98 | 1.00 |

A n=44 la potenza è dell'11–23% proprio per gli effetti che si vogliono misurare: quasi nove effetti
reali su dieci resterebbero invisibili, e a quella potenza un risultato "significativo" è più
probabilmente rumore che segnale. **Servono ~400–800 trade risolti** (≈2–4 giorni di esercizio
continuo) prima che queste ipotesi possano essere verificate anziché interpolate. Fino ad allora la
mossa corretta è raccogliere dati, non filtrare: un quinto filtro tarato su 3 sconfitte ridurrebbe
il throughput e degraderebbe proprio la raccolta che potrà rispondere alla domanda.

### Il sizing non può sostituire il segnale

Il rapporto di payoff è **invariante di scala**: una bet da $2.50 a 86¢ vince ~$0.39 e perde ~$2.56,
lo stesso 6.7:1 e lo stesso breakeven del 13.1% sul tasso di sconfitta. Ridurre la size su un
sottoinsieme "sospetto" migliora il valore atteso solo se quel flag marca davvero un win rate
inferiore — l'unica cosa che i dati non riescono a dimostrare. Compra tranquillità, non edge.

⚠️ **Trappola meccanica.** `fixed_bet_usd: 5` e `min_bet_usd: 5` sono uguali, quindi oggi **tre
moltiplicatori sono inerti**: `COIN_LIQUIDITY_RANK` (BTC 1.0 … DOGE 0.4), `loss_size_multiplier: 0.50`
e lo smorzatore di correlazione `1/(1+posizioni_aperte)`. Abbassare `min_bet_usd` per far mordere una
regola di sizing li accenderebbe **tutti e tre insieme**: un ingresso su DOGE con tre posizioni
aperte diventerebbe `5 × 0.4 × 0.25 = $0.50`, arrotondato a `$1` — un taglio dell'80% su gran parte
del book, non la modifica mirata che si intendeva. L'ordine in `_open_position` è
`fixed_bet → max_bet_usd_cap → liquidity_factor → post_loss_reduction → correlazione →
arrotondamento → cap di esposizione → floor min_bet_usd`: il floor è **l'ultimo**, e per questo oggi
sovrascrive tutto il resto. Un moltiplicatore mirato va quindi applicato **dopo** il floor, con una
chiave propria che vale 1.0 di default.

### L'unica direzione non ancora confutata (e perché non è pronta)

Tutti i filtri sopra agiscono all'ingresso. La perdita però si realizza **dopo**, e i log contengono
già il percorso post-ingresso. Uno stop-loss — uscire se il lato detenuto scende sotto X con ≥15 s
residui — **non blocca alcun ingresso**, quindi non riduce il volume. Misurato con
`tools/approach.py` su 37 trade risolti del run corrente:

| soglia | scatta su | sconfitte intercettate | PnL (slippage 0¢ → 10¢) |
|---|---|---|---|
| 70¢ | 2/38 | 2 di 3 | +$11.62 → **+$15.33 … +$14.40** |
| 75¢ | 3/38 | 2 di 3 (e 1 vincente) | +$11.62 → +$13.69 … +$12.17 |
| 80¢ | 7/38 | 2 di 3 (e 5 vincenti) | +$11.62 → +$9.10 … +$5.24 |

**Non è raccomandato**, per tre motivi concreti. Intercetta 2 sconfitte su 3: il BTC del 14:29 non è
mai sceso sotto 85¢ finché restavano 15 s — è collassato a 55¢ **dentro** gli ultimi 15 s, e uno stop
lo avrebbe mancato. La quotazione è un **mid, non un bid**: tutto il guadagno sta dentro un'ipotesi
di esecuzione non misurata, e a 80¢ la regola perde denaro. E non è una chiave di config:
`resolve_position()` è binaria vinta/persa, non esiste una chiusura intermedia né nel ledger paper né
nell'executor live, e un trade uscito anticipatamente non ha etichetta W/L — corromperebbe proprio
le `bucket_stats` che alimentano il cancello d'ingresso. Va rimisurato a 400 trade, offline, dai log.

### Onestà statistica

- **7 vittorie consecutive è un evento 1-su-128.** L'intervallo di confidenza al 95% su 7/7 va da
  ~59% a 100%: non distingue una strategia vincente da una mediocre.
- Riproducendo il tape di riferimento, le guardie spostano il breakeven da ~90.3% a ~87.3%: circa
  **3 punti su un deficit di ~19**, al costo di circa il **57% del volume**. Le guardie eliminano
  una categoria di fill strutturalmente sfavorevoli; **non rendono la strategia profittevole**.
- Il divario va chiuso dal lato del **segnale**. Nessun filtro d'ingresso recupera 16 punti di win
  rate.

### Difetti noti e non corretti

1. **Il ciclo di feedback del win rate è disallineato.** Il cancello interroga il bucket del prezzo
   *quotato*, ma le statistiche vengono scritte nel bucket del prezzo *riempito*. Oggi il difetto è
   **mascherato dalla granularità dei bucket**: con `max_effective_entry_cents: 88` entrambi i
   prezzi cadono sempre in `78-89¢`. Tornerebbe a manifestarsi alzando il cap sopra 89¢, come
   accadeva nel baseline, dove 6 fill su 9 finivano in `90-94¢` mentre il cancello continuava a
   leggere `78-89¢`.
2. **La banda si apre con un solo trade.** Una vittoria porta il bucket a 100% e spalanca la banda;
   servono poi ~10 vittorie consecutive per riaprirla dopo una sconfitta. `min_data_trades` esiste
   ma non è collegato.
3. **Una guardia che rifiuta una coin può liberare uno slot per un'altra**, perché i cap di
   concorrenza ed esposizione in `_open_position` dipendono dall'ordine. Comportamento riprodotto e
   documentato: le guardie non sono puramente sottrattive.
4. **Il PnL di sessione non è quello di vita.** `PaperTrader` riallinea il saldo iniziale a quello
   caricato all'avvio, quindi dopo un riavvio la cifra è relativa alla sessione corrente.
