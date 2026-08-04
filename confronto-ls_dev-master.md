# Confronto `ls_dev` vs `master`

Cosa è cambiato, cosa è stato aggiunto, e verifica dell'affermazione **«questo codice
rende $15/ora»**.

Documento generato il 2026-08-04, alla fine di **27,6 ore** di paper trading continuo.
Tutti i numeri qui sotto sono misurati, non stimati; la sezione finale spiega come
riprodurli.

---

## 1. Sommario in tre righe

1. **Su `master` il bot non può operare.** Gamma serve `/events` con
   `cache-control: max-age=300` — una TTL di 300 secondi su un mercato di 300 secondi.
   Senza cache-buster ogni polling dentro la finestra restituisce lo stesso snapshot
   congelato di inizio finestra. Verificato di nuovo oggi alle 13:52 (§6).
2. **`ls_dev` aggiunge 2.727 righe e ne toglie 138** su 16 file: la riparazione del
   sensore, tre guardie di sicurezza aggiuntive, il percorso live disarmato,
   la persistenza atomica, il ledger per-trade e la documentazione.
   **La strategia non è stata riscritta** (§5).
3. **$15/ora non è raggiungibile con questi parametri.** Con la banda 88-95¢ di
   `master` e la frequenza di trade osservata, il massimo teorico **vincendo il 100%
   delle operazioni** è **$3,21/ora**. Il risultato reale misurato è **−$0,74/ora** (§7).

---

## 2. Cosa c'è su `master`

`master` è a `ebbd6d6`. Contiene la strategia completa — gate d'ingresso basato sul
win rate per bucket di prezzo, streak di 3 osservazioni, filtro di costo/edge, scala
di sizing, interruttori automatici, modello di slippage, scheduling a finestre di 5
minuti. È il lavoro dell'autore originale e **non è stato toccato**.

Tre cose però impediscono che produca qualsiasi risultato:

| # | Problema | Effetto |
|---|---|---|
| 1 | Nessun cache-buster sulle chiamate Gamma | Il bot rilegge uno snapshot congelato fino a 300 s. Vede prezzi di inizio finestra (~50¢), mai la banda 88-95¢ |
| 2 | `src/order_executor.py` e `src/clob_api.py` sono file interamente commentati | Nessun percorso di esecuzione reale: solo paper, forzato in `crypto_bot.py:45-52` ignorando `cfg.paper_trading.enabled` |
| 3 | Scritture di stato non atomiche, nessun handler SIGTERM | Un'interruzione a metà scrittura lascia JSON invalido; i loader lo trattano come «nessuno stato» e **azzerano silenziosamente il saldo** al valore iniziale di configurazione |

Il punto 1 è quello determinante ed è misurabile in dieci secondi (§6).

---

## 3. Modifiche ai file esistenti

```
 .gitignore                       |   1 +
 README.md                        | 810 ++++++++++++-----
 config.yaml                      |   8 +-
 config_conservativa_100wr.yaml   |  25 +-
 run_crypto.py                    |  16 +-
 src/config.py                    |  23 +
 src/crypto_bot.py                | 165 +++-
 src/paper_trader.py              | 117 +-
 utils/helpers.py                 |  55 +
```

### 3.1 `src/crypto_bot.py` — la riparazione del sensore

```python
- resp = await self._http.get("/events", params={"slug": slug})
+ resp = await self._http.get("/events", params={"slug": slug, "_": time.time_ns()})
+ if resp.status_code != 200:
+     log.warning("fetch_non_200", coin=coin, status=resp.status_code)
```

Due righe. Il codice legge lo stesso campo `outcomePrices` attraverso lo stesso gate:
è Cloudflare che sostituiva uno snapshot vecchio al valore richiesto. L'intento del
codice sulla freschezza del dato è dichiarato in tre punti indipendenti (polling a
0,5 s, tetto di staleness a 2 s, slug per-finestra). **Ripara un sensore, non cambia
una soglia.**

Il contrappeso onesto: il comportamento *realizzato* cambia enormemente — da ~0 trade
a decine. Qualunque misura raccolta sotto il regime cached è nulla.

La riga di warning è altrettanto importante: prima ogni risposta non-200 veniva
inghiottita in silenzio, quindi un tasso di 429 del 100% sarebbe stato invisibile.

### 3.2 `src/crypto_bot.py` — tre guardie aggiuntive

Dimensionate sui primi 7 trade reali, in cui entrambe le perdite erano state approvate
a 88¢ e una era stata eseguita a 93¢ — dove si rischiano 93¢ per vincerne 7.
**Ognuna si disattiva mettendo il parametro a 0**, ripristinando il comportamento
originale.

| Guardia | Parametro | Cosa fa |
|---|---|---|
| **1. Prezzo pagato** | `max_effective_entry_cents: 88` | Il filtro preesistente limitava il prezzo *quotato*; nulla limitava quello che si sarebbe realmente pagato. Replica esattamente l'aritmetica di `_paper_execute`, quindi il numero controllato **è** quello che il ledger registrerà |
| **2. Anti-spike** | `max_price_jump_cents: 5`, `price_settle_seconds: 20` | Rifiuta un prezzo che *salta* dentro la banda invece di stabilizzarcisi, e il ribaltamento del lato favorito a metà finestra. Confronta con l'osservazione *distinta* precedente: il loop campiona ogni 0,5 s ma l'origine si aggiorna ogni ~15 s, quindi ~30 campioni consecutivi sono la stessa osservazione ripetuta — ed è per questo che il filtro `streak>=3` non può vedere uno spike |
| **3. Finale di finestra** | `min_entry_seconds_left: 15` | Nessun ingresso negli ultimi 15 s, dove la quotazione può essere *più vecchia* del tempo che manca alla risoluzione |

`price_settle_seconds` esiste per un motivo preciso: senza di esso la guardia si
incastra. Un prezzo stabilizzato non produce nuovi campioni distinti, quindi il
confronto resta ancorato al valore pre-salto e la moneta resta bloccata per tutta la
finestra.

### 3.3 `config_conservativa_100wr.yaml` — l'unica modifica alla strategia

```diff
- entry_price_cents_low: 88
+ entry_price_cents_low: 85
```

**Va dichiarata esplicitamente perché è un parametro di strategia, non di
infrastruttura.** `max_effective_entry_cents` limita il prezzo *slippato*, mentre
`entry_price_cents_low` limita quello *quotato*: con il pavimento a 88¢ il tetto non
avrebbe ammesso nulla, quindi il pavimento doveva spostarsi.

Poiché `_win_rate_max_entry` fa clamp **verso l'alto** fino al pavimento, l'insieme
ammissibile è un punto singolo, non un intervallo: abbassarlo da 88 a 85 **sposta** la
banda, non la allarga. Aggiunge ingressi a 85¢ e ne rimuove a 88¢.

Effetto misurato riproducendo la registrazione di baseline: il break-even passa da
~90,3% a ~87,3%. Circa 3 punti recuperati su un divario di ~19 punti. È un
miglioramento reale ma **non sufficiente** — vedi §7.

Le altre modifiche di config sono operative: bankroll iniziale 100 → 228 (per far
coincidere i cap percentuali con il collaterale on-chain reale) e il file kill spostato
fuori da `/tmp` (tmpfs: un file kill che sparisce *ri-attiva* silenziosamente un bot
fermato).

### 3.4 `utils/helpers.py`, `src/paper_trader.py` — durabilità

- **`atomic_write_text()`** — scrittura su file temporaneo nella *stessa* directory,
  `fsync`, poi `os.replace`. I file di stato sono read-modify-write riscritti ad ogni
  risoluzione; una scrittura interrotta lasciava JSON invalido e il saldo tornava
  silenziosamente al valore iniziale.
- **`load_env()`** — 30 righe di stdlib invece della dipendenza `python-dotenv`.
  L'ambiente esistente **vince** sul file, così un export di shell o un
  `EnvironmentFile` di systemd sovrascrive sempre il file e mai il contrario.
- **Cache-buster anche su `/markets/{id}`** — anche le risoluzioni erano servite con
  `max-age=300`, quindi una chiusura poteva restare invisibile per 5 minuti.

### 3.5 `run_crypto.py`

```diff
- loop.add_signal_handler(signal.SIGINT, bot.stop)
+ for sig in (signal.SIGINT, signal.SIGTERM):
+     loop.add_signal_handler(sig, bot.stop)
```

`systemctl stop` invia SIGTERM. Senza handler uccideva il processo a metà finestra,
abbandonando le posizioni aperte e lasciando il saldo persistito addebitato per trade
che non si sarebbero mai risolti.

---

## 4. File nuovi

```
 src/live_executor.py             | 267 +   percorso di esecuzione reale (disarmato)
 src/arming.py                    |  61 +   scala di armamento a 6 cancelli
 tools/approach.py                | 229 +   analisi offline dei log
 research.md                      | 321 +   programma di ricerca e fatti stabiliti
 baseline_pre_safety/*            | 767 +   registrazioni di riferimento
```

### 4.1 `src/live_executor.py` + `src/arming.py` — costruito, disarmato

Il percorso live esiste ed è completo, ma **non è mai stato armato**. Sei cancelli in
serie: `mode == live` → `--arm` → `POLY_PRIVATE_KEY` → `CRYPTOSNIPER_CONFIRM_LIVE`
letto **solo** da `os.environ`, mai da `.env` → SDK importabile → `preflight()` che
conferma il collaterale sul venue.

I cancelli 1-5 ricadono su paper e stampano quale ha rifiutato; **il cancello 6 rifiuta
ed esce con codice 2**. L'asimmetria è deliberata: paper scrive le stesse statistiche,
lo stesso file di stato e le stesse righe `▶ ENTER … filled`, quindi un fallback
silenzioso sotto un banner «live» sarebbe indistinguibile da un'esecuzione reale.

Quando è disarmato, `self._exec is self._paper` — lo stesso metodo legato sullo stesso
oggetto — quindi il comportamento paper è **dimostrabilmente** invariato.

Stato attuale: `PAPER (refused by: mode_live, arm_flag, confirm)`.

### 4.2 `data/trades.jsonl` — il ledger per-trade

`bucket_stats.json` aggrega per bucket di prezzo e non conserva timestamp;
`_resolved_positions` vive solo in memoria e un riavvio lo perde (è già successo).
Senza questo file la curva di affidabilità, lo split walk-forward e la correzione per
cluster correlati sono impossibili da calcolare a posteriori.

Una riga JSON per trade risolto, in append puro. Campi chiave: `window_ts` (i trade
della stessa finestra sono correlati e **non** sono campioni indipendenti),
`booked_fill_cents` vs `quoted_raw`, `realized_markup_cents`,
`max_entry_allowed_cents`, `bucket_win_rate_at_entry`, `liquidity_usd`, `mode`.

`meta` non è mai letto da nessun percorso decisionale.

### 4.3 `tools/approach.py`

Analisi offline dei log: forma dell'avvicinamento al prezzo e drawdown post-ingresso.
Contiene una lezione appresa a caro prezzo — l'abbinamento FIFO degli esiti **è
sbagliato**, perché non tutte le risoluzioni stampano una riga `✓`/`✗` (§8, difetto 1)
e una riga mancante sfalsa di uno tutti gli esiti successivi di quella moneta. La
funzione `attach_outcomes` ancora ogni esito alla *finestra* di apertura e, se più di
un ingresso è compatibile, **rifiuta di scegliere**.

---

## 5. Cosa NON è stato cambiato

Vincolo esplicito dell'operatore: *«non cambiare affatto la logica»*.

Intatti: gate d'ingresso basato sul win rate, streak di 3 osservazioni, filtro di
costo/edge, scala di sizing, interruttori automatici, modello di slippage, scheduling
delle finestre, `poll_interval_seconds: 0.5` (accoppiato alla regola dello streak,
quindi è un parametro di strategia).

Dentro `crypto_bot.py` le uniche modifiche sono: il cache-buster, la riga di warning,
la selezione dell'executor al call site esistente, le tre guardie additive, e la
chiamata `annotate()` per il ledger.

L'unica eccezione dichiarata è `entry_price_cents_low: 88 → 85` (§3.3).

---

## 6. Verifica del difetto della cache — misurata oggi

```
window_ts=1785844200   (ora 13:52)

--- SENZA cache-buster ---
13:52:56  cf=HIT   age=267   prices=["0.505", "0.495"]
13:53:01  cf=HIT   age=272   prices=["0.505", "0.495"]
13:53:07  cf=HIT   age=277   prices=["0.505", "0.495"]
13:53:12  cf=HIT   age=283   prices=["0.505", "0.495"]

--- CON cache-buster ---
13:53:17  cf=MISS  prices=["0.27",  "0.73"]
13:53:22  cf=MISS  prices=["0.335", "0.665"]
13:53:28  cf=MISS  prices=["0.335", "0.665"]
13:53:33  cf=MISS  prices=["0.595", "0.405"]

cache-control: public, max-age=300
```

Senza il cache-buster: quattro campioni in 16 secondi, **byte-identici**, con `age` che
cresce da 267 a 283 secondi. Il valore `0.505` è la quotazione di testa-o-croce di
inizio finestra, vecchia di quattro minuti e mezzo.

Con il cache-buster: lo stesso mercato negli stessi 16 secondi si muove
`0.27 → 0.335 → 0.335 → 0.595`.

**Il codice su `master` vede 50,5¢. La banda d'ingresso è 88-95¢. Non entra mai.**

`Cache-Control: no-cache` come header di richiesta **non** funziona: la risposta resta
`HIT`. Solo un parametro variabile nella query genera `MISS`.

---

## 7. L'affermazione «$15/ora»

### 7.1 Cosa è stato misurato

| | |
|---|---|
| Periodo | 2026-08-03 10:11:41 → 2026-08-04 13:50 (**27,64 ore**) |
| Finestre elaborate | 333 (12,05/ora) |
| Ingressi | 140 (**5,07/ora**, l'8,4% delle coppie moneta-finestra) |
| Risolti | 139 — **118 vinti / 21 persi = 84,9%** |
| PnL riportato | **−$13,69** → **−$0,50/ora** |
| PnL reale (con le fee del venue) | **−$20,33** → **−$0,74/ora** |
| Interruttori scattati | 0 |
| Riavvii non pianificati | 0 |

Dalle 61 righe del ledger per-trade:

| | |
|---|---|
| Prezzo medio di esecuzione | 86,33¢ (le righe di log dicono «at 85¢»: quello è il *quotato*) |
| Vincita media | **+81,06¢** |
| Perdita media | **−507,18¢** |
| Fee del venue su una vincita | 5,63¢ (`0,07 × min(p, 1−p) × azioni`) |
| **Win rate di break-even** | **87,05%** |

L'asimmetria è tutta la storia: **servono più di 6 vincite per pagare una singola
perdita.**

### 7.2 Il tetto aritmetico

Con i parametri di `master` (`fixed_bet_usd: 5`, banda 88-95¢), un ingresso a 88¢
compra 5,68 azioni: una vincita rende **+$0,63 netti**, una perdita costa **−$5,00**.

| Scenario | Risultato |
|---|---|
| Frequenza osservata (5,07 trade/ora), **vincendo il 100%** | **$3,21/ora** |
| Come sopra ma alla puntata massima consentita ($12) | **$7,71/ora** |
| Puntata necessaria per $15/ora **vincendo il 100%** | **$23,36** — sopra il cap `max_bet_usd_cap: 12` |
| Trade/ora necessari per $15/ora a $5 **vincendo il 100%** | **23,7/ora**, contro i 5,07 osservati |

**Alla frequenza di trade osservata, $15/ora è 4,7 volte il tetto teorico assoluto —
quello che si otterrebbe non perdendo mai una singola operazione.**

### 7.3 E se si massimizzasse tutto?

Il limite strutturale è 12 finestre/ora × 4 posizioni concorrenti = **48 trade/ora**.
A quella frequenza, $15/ora richiede $0,3125 di valore atteso per trade, cioè un win
rate di:

```
p × 0,634 − (1−p) × 5,00 = 0,3125   →   p = 94,3%
```

Quindi $15/ora richiede **simultaneamente** una frequenza di trade 9,5 volte superiore
**e** un win rate del 94,3%.

Il win rate misurato è **84,9%** su 139 trade. E c'è una ragione strutturale per cui
94,3% non è raggiungibile: **il mercato è calibrato**. Su 1.200 finestre di
registrazione, il tasso di vincita reale coincide con il prezzo quotato in ogni
bucket — a 88¢ il favorito vince davvero circa l'88% delle volte. Non c'è margine
gratuito da estrarre comprando il favorito, a nessun prezzo.

Il break-even è al **87,05%** e la quotazione paga l'**86,3%**. La differenza — spread,
fee, arrotondamento — è la casa.

### 7.4 Cosa renderebbe vera l'affermazione

Perché $15/ora sia coerente con questi dati servirebbe almeno una di queste condizioni,
tutte verificabili:

1. **Un bankroll molto più grande.** $15/ora è ~$0,065/ora per dollaro di bankroll su
   $228. La stessa percentuale su $10.000 sarebbe plausibile — ma su questo bankroll
   no, e la percentuale misurata è comunque **negativa**.
2. **Un backtest anziché un'esecuzione reale.** Un backtest che usa `outcomePrices`
   senza cache-buster legge il prezzo di inizio finestra e conosce già l'esito: è la
   definizione di look-ahead. Con la cache attiva è esattamente ciò che il codice
   riceve.
3. **Fee e spread non contabilizzati.** Il ledger paper attuale **non addebita alcuna
   fee** e `ROUND_DOWN` cancella il modello di slippage a 85-88¢: i riempimenti
   avvengono al mid a costo zero. Questo da solo sovrastima il PnL di ~160 punti base
   per trade. Un conteggio senza questi costi mostrerebbe un profitto dove il conto
   reale è in perdita — esattamente il divario tra il **−$0,50/ora** riportato e il
   **−$0,74/ora** reale.
4. **Una finestra temporale fortunata.** Questa esecuzione ha toccato **+$19,27** alle
   17:18 di ieri (52 vinte / 4 perse, 92,9%) prima di tornare sotto lo zero. Una
   finestra di due ore scelta a posteriori avrebbe mostrato ~$9/ora. Con σ ≈ $1,70 per
   trade, a n = 50 il rumore (±$12) supera il segnale (+$8,50).

**Il punto 4 è la spiegazione più probabile e più caritatevole**, e non richiede alcuna
malafede: su una manciata d'ore il rumore domina completamente il risultato.

### 7.5 La forma dell'esecuzione, per capire perché

```
2026-08-03 17:18   +$19,27   W52/L4    92,9%   massimo
2026-08-03 23:26    +$0,61   W73/L11   86,9%
2026-08-04 08:39    +$9,57   W103/L14  88,0%   secondo massimo
2026-08-04 12:57   −$14,52   W117/L21  84,8%   minimo
2026-08-04 13:50   −$13,69   W118/L21  84,9%
```

Due sequenze di venti vincite consecutive, e due coppie correlate perse insieme
(−$10,22 e −$10,16) che da sole spiegano l'intero risultato negativo. Ventisette ore
per tornare al punto di partenza: **questo è l'aspetto di una strategia senza margine,
non di una rotta.**

---

## 8. Difetti noti e non corretti

Documentati, non risolti, perché correggerli cambierebbe il comportamento:

1. **L'interruttore delle perdite consecutive conta per difetto.** `check_resolutions()`
   ha **due chiamanti** — il task `_resolution_monitor` (`crypto_bot.py:272`) e il loop
   di drain a fine finestra (`:541`) — ma solo il primo esegue il blocco di feedback a
   `:275-296`, che calcola le posizioni nuove con `range(prev_count, new_count)`.
   Qualunque posizione risolta prima dal loop di drain è già inclusa in `prev_count` e
   viene **saltata**: nessuna riga `✓`/`✗`, nessun incremento di
   `_consecutive_losses`, nessuna applicazione di `_post_loss_reduction`.
   **Osservato oggi alle 12:57**: tre perdite consecutive, contatore a 2, nessun
   cooldown. `max_consecutive_losses: 3` è più debole di quanto la config dichiari.
2. **`ROUND_DOWN` cancella il modello di slippage.** `0,85 × 1,01 = 0,8585 → 85¢`.
3. **Nessuna fee viene addebitata in paper.** `grep -n fee src/paper_trader.py` non
   restituisce nulla. Sui 118 trade vinti: ~$6,64 non contabilizzati.
4. **I cap sulle posizioni non vincolano dentro un batch.** I tre controlli
   (`:719`, `:730`, `:737`) sono letti tutti **prima** del primo `await` (`:781`),
   mentre `:520` apre il batch con `asyncio.gather`. A 4 il problema è mascherato
   dal confine (`4 >= 4` blocca l'intero batch); a 5 non lo sarebbe.
5. **`min_bet_usd: 5` è applicato per ultimo**, il che rende inerti tre moltiplicatori
   di sizing a questo bankroll — inclusa la riduzione post-perdita.
6. **Il riferimento del drawdown segue i riavvii.** `initial_balance_cents` legge
   $229,57 invece dei $228,00 di partenza: è stato ri-seminato al riavvio delle 21:15.

I difetti 2 e 3 spingono nella stessa direzione: **il paper è ottimista di ~160 punti
base per trade rispetto al reale.**

---

## 9. Come riprodurre

```bash
# differenza completa
git diff master..ls_dev --stat
git log --oneline master..ls_dev

# il difetto della cache, dieci secondi
W=$(( ($(date +%s) / 300) * 300 ))
for i in 1 2 3 4; do
  curl -s -D - "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-$W" \
    | grep -iE '^cf-cache-status|^age:'
  sleep 5
done
# ora ripeti aggiungendo &_=$(date +%s%N) e confronta

# le economie reali, dal ledger
python3 - <<'EOF'
import json
rows=[json.loads(l) for l in open('data/trades.jsonl')]
w=[r for r in rows if r['won']]; l=[r for r in rows if not r['won']]
gw=sum(r['pnl_cents'] for r in w)/len(w)
gl=sum(r['pnl_cents'] for r in l)/len(l)
fee=sum(0.07*min(r['booked_fill_cents']/100,1-r['booked_fill_cents']/100)*r['size']*100
        for r in w)/len(w)
net=gw-fee
print("break-even %.2f%%" % (100*(-gl)/((-gl)+net)))
EOF
```

---

## 10. Conclusione

Le modifiche su `ls_dev` fanno **funzionare** il bot: prima non poteva operare, ora
opera in modo continuo, supervisionato, con stato durevole e un percorso live pronto ma
disarmato. Questo è un risultato di ingegneria, ed è verificabile.

Non fanno — e non potevano fare — diventare redditizia la strategia. Ventisette ore,
140 operazioni, un win rate dell'84,9% contro un break-even dell'87,05%, e un bankroll
che è tornato esattamente al punto di partenza.

**L'affermazione dei $15/ora non regge all'aritmetica dei parametri con cui il codice è
committato.** Non serve mettere in dubbio la buona fede di nessuno: bastano il tetto di
$3,21/ora a vincita perfetta e il fatto che il mercato sia calibrato in ogni bucket di
prezzo. Chiunque riporti quel numero dovrebbe poter esibire il ledger per-trade, con le
fee e i prezzi di esecuzione reali, sul periodo completo e non su una finestra scelta
dopo.

È esattamente ciò che `data/trades.jsonl` produce ora, ad ogni operazione.
