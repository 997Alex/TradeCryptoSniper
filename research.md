# research.md — cosa dobbiamo ancora capire

Documento di lavoro. Registra **cosa sappiamo**, **cosa è già stato confutato** (per non ripetere
lavoro morto) e **il programma di analisi** da eseguire quando ci sarà volume sufficiente.

Aggiornato: 2026-08-03. Vedi [README.md](README.md) per il funzionamento del bot.

---

## 1. Criterio di go-live

**Regola concordata: si passa live solo quando il limite inferiore dell'intervallo di Wilson al 95%
sul win rate supera il breakeven.** Non il win rate puntuale, non il PnL nominale: il limite
inferiore.

Il breakeven **non è ~89%** come indicato in versioni precedenti di questo repo. Al prezzo
realmente pagato (ask, non mid) e con la commissione reale del venue:

```
fill medio registrato          85.76¢   (misurato su 66 fill del log)
premio ask sul mid             +0.8-1.0¢ (misurato sul tape PolyML)
ask reale                      ~86.6-86.8¢
fee = 0.07 x min(p, 1-p)       ~0.93-0.94¢/share  (fee_rate: 0.07 confermato dal tape)
BREAKEVEN                      87.4% - 87.6%
```

Quanti trade servono, in funzione del win rate vero:

| win rate mantenuto | n necessario | sconfitte | giorni a 187 trade/giorno | n corretto per correlazione |
|---|---|---|---|---|
| 88.5% | 5 056 | 581 | **27.0** | 6 016 |
| 90.0% | 694 | 69 | 3.7 | 825 |
| 92.0% | 204 | 16 | 1.1 | 242 |
| 95.0% | 67 | 3 | 0.4 | 79 |

**Stato al 2026-08-03 20:42: W64/L8, n=72, win rate 88.9%, limite inferiore di Wilson 79.6%.**

⚠️ **Il numero "giusto" di trade non è una costante: dipende da quanto è buona la strategia.** Più
il win rate vero è vicino al breakeven, più dati servono. Al ritmo attualmente osservato (~88.5%)
servono circa **27 giorni di esercizio continuo**. Se il win rate vero fosse 92%, basterebbe un
giorno. Non si può accorciare questa attesa scommettendo di più — vedi §6.

---

## 2. Fatti stabiliti (non ri-derivare)

### Il mercato è calibrato

Dal tape PolyML (`data/live/book_tape_5m.parquet`, 1200 finestre, stessi mercati e stesse coin),
win rate reale contro win rate implicito nel prezzo:

| ask del favorito | n | reale | implicito | scarto |
|---|---|---|---|---|
| 0.50-0.60 | 129 | 48.8% | 56.4% | −7.5pp |
| 0.60-0.70 | 147 | 67.3% | 65.6% | +1.7pp |
| 0.70-0.80 | 169 | 75.7% | 75.6% | +0.1pp |
| 0.80-0.85 | 132 | 80.3% | 83.0% | −2.7pp |
| **0.85-0.90** | 144 | 90.3% | 88.0% | **+2.2pp** |
| 0.90-0.95 | 192 | 96.9% | 93.2% | +3.6pp |
| 0.95-1.00 | 252 | 95.6% | 97.5% | −1.9pp |

Tutti gli scarti stanno dentro il rumore. **Non esiste un edge gratuito nel comprare il favorito a
nessun prezzo.** Questo è il fatto centrale: implica che nessuna regola d'ingresso può vincere, e
che l'unico margine ottenibile sta nei **costi**, non nella previsione.

### EV netto per prezzo (ask reale, fee 0.07)

| banda ask | n | EV netto/$1 | ±1 SE |
|---|---|---|---|
| 75-80¢ | 85 | +0.71% | 5.75% |
| **80-85¢** | 126 | **−6.51%** | 4.51% |
| **85-88¢ (la nostra)** | 87 | **+3.12%** | 3.79% |
| 88-92¢ | 128 | −2.20% | 3.17% |
| 92-96¢ | 169 | **+3.70%** | **1.25%** |
| 96-101¢ | 272 | −1.83% | 1.22% |

**Abbassare la soglia sotto 85¢ è la mossa peggiore disponibile.** L'unica cella oltre 2 SE è
92-96¢, ma è 1 su 8 esaminate e lì una sconfitta richiede ~17 vittorie per essere ripagata:
**ipotesi da pre-registrare, non da adottare.**

### La fee è invariante di scala

`0.07 x min(p, 1-p)` equivale esattamente al **7% della vincita lorda a qualunque prezzo** per un
favorito. Le commissioni non danno quindi alcun motivo per preferire prezzi alti o bassi. Lo
**spread** invece sì: 1.88¢ su una vincita di 6¢ a 94¢ è il 31% del margine, contro il 6% a 60¢.

### Le coin differiscono nell'esecuzione, non nella previsione

| coin | spread in banda | profondità mediana in vendita | exit5 vs bid |
|---|---|---|---|
| ETH | 0.97¢ | $172 | 0.00¢ |
| BTC | 1.00¢ | $905 | 0.00¢ |
| SOL | 1.26¢ | $72 | 0.00¢ |
| DOGE | 1.91¢ | $54 | 0.01¢ |
| XRP | 3.10¢ | $65 | 0.00¢ |

Test di permutazione sui win rate per coin: **p = 0.54**, nessuna differenza predittiva. Spread e
profondità sono invece misurati su ogni osservazione e vanno considerati reali.

**`exit5 == bid` su tutte le coin**: uscire da 5 share non costa nulla rispetto al bid.

### I trade nella stessa finestra sono correlati

Misurato sul tape, favoriti con mid 0.85-0.95, n=392 su 192 finestre, 302 coppie intra-finestra:

```
phi = 0.142        permutazione (shuffle entro coin) p = 0.017
P(perdono entrambi) osservata 0.99%  vs  0.44% se indipendenti  =  2.26x
```

**Conseguenze pratiche:**
- Le posizioni aperte nella stessa finestra **non sono campioni indipendenti**. Il campione
  effettivo è inferiore al numero di trade e ogni intervallo di confidenza calcolato ignorando
  questo è **troppo stretto** (correzione ~+19% sul n necessario).
- Con `max_concurrent_open_positions: 4` a $5, l'esposizione per finestra è ~8.7% del bankroll con
  probabilità di perdita congiunta più che doppia rispetto all'indipendenza.
- **Ogni analisi futura deve raggruppare per `window_ts`,** non trattare i trade come indipendenti.

---

## 3. Ipotesi già confutate (non ripetere)

Sette famiglie testate, **nessuna sopravvissuta** alla verifica avversariale:

| ipotesi | esito | perché |
|---|---|---|
| forma di avvicinamento (sale/scende/piatto in banda) | ✗ | le due sconfitte più istruttive hanno forme **opposte**; il filtro ne blocca una e lascia passare l'altra |
| ora del giorno / giorno della settimana | ✗ | l'effetto sparisce condizionando sul prezzo (permutazione p=0.75); r=0.52 fra ask medio orario e win rate orario |
| differenze di win rate fra coin | ✗ | permutazione p=0.54. DOGE risultava **peggiore** nei nostri 61 trade (33% loss) e **migliore** nel tape (+6.9pp) |
| aumento della puntata su serie di vittorie | ✗ | leva, non edge: le sconfitte sono senza memoria; flat $15 batteva la rampa |
| spostare la banda a 94-97¢ per ridurre la fee | ✗ | la fee è invariante di scala; la cella ha EV negativo sulla sua stessa premessa |
| ordini maker per catturare lo spread | ✗ | P(vittoria \| eseguito) 58-64% contro un bid all'86.5%: l'unica cosa che porta il favorito al tuo bid è il sottostante che gli va contro |
| stop-loss post-ingresso | ✗ (teoria) | in un mercato calibrato la quotazione a 75¢ **è** la probabilità: vendere lì è un trade equo meno un secondo spread e una seconda fee (~−21 bps) |

**Lezione metodologica:** ognuna sembrava funzionare in-sample. Sono morte tutte per *data mining*.
Con 24 bucket orari e due settimane di dati, **il 48% delle simulazioni produce un bucket
"significativamente più sicuro" che è puro rumore.** Vedi §5.

---

## 4. Il programma di analisi

Da eseguire quando `data/trades.jsonl` avrà volume. Ordine di priorità.

### Diagnostica — cosa non va

1. **Curva di affidabilità** (reale vs implicito per bucket di prezzo). È la diagnostica madre. Se è
   piatta sulla diagonale il mercato è calibrato e nessuna regola d'ingresso può vincere.
2. **Attribuzione del PnL.** Scomporre il PnL realizzato in `edge del segnale − spread pagato −
   fee pagata − slippage − timing`. Oggi non sappiamo dove vanno i soldi; i ~$3 di fee non
   addebitate sono il sintomo.
3. **Test di selezione avversa — da fare per primo.** Il tape campiona a istanti fissi; il nostro
   bot entra quando il prezzo **tocca** 85-88¢. Sono popolazioni diverse. Confrontare i nostri fill
   reali con l'ask contemporaneo del tape: se riempiamo sistematicamente peggio, il trigger sta
   selezionando esattamente i momenti in cui arriva flusso informato, e l'87.5% del tape
   sovrastima ciò che possiamo catturare. **Se questo test fallisce, ogni altro numero misura una
   popolazione che non tradiamo.**

   ⚠️ **Oggi il test NON è eseguibile, e il limite è del tape, non del ledger.** L'ultima
   osservazione del book nel tape è a `t0+200`; i nostri ingressi cadono fra `t0+210` e `t0+285`
   (finestra `monitor_start_seconds: 90` → `min_entry_seconds_left: 15`). Il confronto più vicino
   disponibile precede quindi il fill di **10-85 secondi**, e in un mercato a 5 minuti vicino alla
   scadenza la deriva di prezzo su quell'intervallo domina l'effetto che si vuole misurare:
   qualunque numero ottenuto così **confonde selezione avversa e deriva**. La chiave di join è
   invece corretta (`coin` + `window_ts` ricostruiscono esattamente lo slug del tape, verificato).

   **Per rendere il test eseguibile** serve una di queste: (a) estendere la scaletta di cattura di
   PolyML oltre `t0+200` — il tetto attuale è imposto da `late_ladder` in `polymarket.py`, che
   scarta 240 e 260; (b) far registrare al nostro bot il book CLOB al momento dell'ingresso, non
   solo il mid di Gamma; (c) trattare il confronto a `t0+200` come **limite superiore** dell'effetto
   e dichiararlo tale. L'opzione (b) è anche l'unica che risolve la domanda §9.4.
4. **Slippage modellato vs realizzato.** `max_slippage_pct: 1.0` prevede 86.4¢ da un mid di 85.6¢ e
   il tape dice 86.36¢. Verificare che regga sul volume. ⚠️ Vedi §7, difetto 1.

### Superficie di decisione — dove va la soglia

5. **EV netto × volume**, non EV da solo. Una banda con EV migliore ma un terzo dei trade può
   rendere meno in totale.
6. **Walk-forward.** Stimare la soglia sulle settimane 1-2, testarla sulla 3. Un ottimo che si
   sposta fra i periodi è rumore.
7. **Grafico di stabilità della soglia** su finestre mobili. Se vaga, fermarsi.

### Ricerca del segnale — l'unica cosa che crea edge

8. **Regressione sul residuo:** `esito − probabilità implicita nel prezzo` contro feature candidate
   (momentum dello spot fra snapshot, dispersione dello spot fra fonti, sbilanciamento della
   profondità bid/ask, deriva dell'ask da p100 a p200). **Ciò che predice il residuo è edge reale;
   ciò che predice l'esito sta solo riscoprendo il prezzo.**
9. **Pre-registrare** lista di feature e regola di decisione *prima* di guardare, con controllo FDR.
   Cinque ipotesi sono morte di data mining: è questa la disciplina che ferma la sesta.
10. **Analisi di potenza prima del test.** Sapere quale dimensione d'effetto il campione può
    rilevare, prima di eseguirlo.

### Rischio — quanto puntare

11. **Kelly frazionario con incertezza sui parametri.** Kelly pieno assume p noto; il nostro ha
    SE ±2.5pp e Kelly cambia segno dentro quell'intervallo. Al prezzo realmente pagato Kelly vale
    **+1.0% … +2.5% del bankroll** ($2.30–$5.75 su $230): il bot ne punta 5, quindi è **già al
    massimo**. Usare mezzo-Kelly o una frazione mediata sulla posteriore.
12. **Dimensione campionaria effettiva.** Le posizioni aperte nella stessa finestra sono correlate
    (§2): raggruppare per `window_ts`. n=72 vale sensibilmente meno di 72 prove indipendenti.

---

## 5. Disciplina: pre-registrazione

Prima di ogni test, scrivere **in questo file, con la data**: ipotesi, feature, bucket, regola di
decisione, dimensione campionaria minima. Poi guardare i dati. Mai il contrario.

Perché non è burocrazia — simulazione con **nessun effetto reale**, due settimane di dati:

```
 7 bucket:  l'11.6% delle simulazioni produce un bucket "significativamente più sicuro"
24 bucket:  il 48.2% delle simulazioni ne produce uno
```

A 168 celle (ora × giorno) il test ha **potenza zero**: non può accendersi in nessuna direzione.

Tempi di riempimento a 187 trade/giorno, per n=400 per bucket:

| bucketing | bucket | trade/bucket/giorno | giorni |
|---|---|---|---|
| giorno della settimana | 7 | 26.7 | 15 |
| blocchi da 4 ore | 6 | 31.2 | 13 |
| ora del giorno | 24 | 7.8 | 51 |
| ora × giorno | 168 | 1.1 | 359 |

**Una finestra mobile di 2 settimane è controproducente**: limita ogni bucket a un intervallo di
confidenza più largo del margine decisionale. Con l'effetto stabile, usare *tutta* la storia.

---

## 6. Perché aumentare la puntata non accelera il recupero

Il rapporto di payoff è **invariante di scala**: a 86¢ una vittoria rende il 16.3% della posta e una
sconfitta il 100%, **a qualunque taglia**. Il breakeven resta identico. Aumentare la puntata
moltiplica deriva e deviazione standard nella stessa misura: la probabilità di essere in vantaggio a
un dato orizzonte **non cambia**.

Simulazione, 1000 trade (~5 giorni al nostro ritmo), 4000 run, bankroll $233:

| puntata | p=87.0% | | p=87.5% | | p=88.5% | |
|---|---|---|---|---|---|---|
| | mediana | P(−50%) | mediana | P(−50%) | mediana | P(−50%) |
| 2.1% ($4.89) | $215 | 2% | $243 | 1% | $310 | 0% |
| 5.0% ($11.65) | $172 | 41% | $231 | 26% | $415 | 6% |
| 10.0% ($23.30) | $85 | 79% | $154 | 67% | $513 | 35% |
| 15.0% ($34.95) | $27 | 92% | $67 | 84% | $424 | 60% |

Al 10% del bankroll per puntata, con il win rate che stiamo osservando, **l'esito mediano è perdere
un terzo del capitale** con il 67% di probabilità di dimezzarlo lungo il percorso.

**Puntare $25 su un bankroll di $1000 va benissimo** — è il 2.5%, la stessa frazione di oggi. La
regola è: **scalare la puntata con il bankroll, non scalare la frazione.**

---

## 7. Difetti noti che invalidano le misure attuali

1. **`ROUND_DOWN` cancella il modello di slippage.** `paper_trader.py` calcola `mid x 1.01` e poi
   tronca ai centesimi interi: `0.85 x 1.01 = 0.8585 -> 85¢`. Il log lo conferma: ogni
   `ENTER ... at 85¢` produce `filled ... @ 85¢`. Il bot modella lo slippage e poi lo butta via.
2. **Nessuna fee viene addebitata.** `grep -n fee src/paper_trader.py` non trova nulla, contro un
   `fee_rate: 0.07` confermato. Sulle 64 vittorie sono ~$3.11 non contabilizzati.
3. **Il ledger riempie al mid, non all'ask** — un prezzo che non esiste su nessun lato del book.

**Conseguenza: il PnL riportato è ottimista di ~160 bps.** Il primo lavoro da fare è correggere il
modello di riempimento (comprare all'ask, togliere il `ROUND_DOWN`, dedurre
`0.07 x min(p, 1-p) x share`). Non guadagna EV: **rimuove un'illusione** su cui poggiano il cancello
win-rate, i prior dei bucket e qualunque confronto paper/live.

Difetti aggiuntivi in [README.md](README.md#difetti-noti-e-non-corretti), inclusi il ciclo di
feedback disallineato del win rate e la corsa fra i cap di concorrenza dentro un batch.

---

## 8. `data/trades.jsonl` — schema

Una riga JSON per trade risolto, append-only, mai riscritta. Scritta da
`PaperTrader._append_ledger`. `meta` non è **mai** letto da un percorso decisionale.

| campo | significato |
|---|---|
| `v` | versione dello schema |
| `entry_ts`, `resolve_ts` | epoch secondi (UTC) di ingresso e risoluzione |
| `entry_iso` | **UTC**, con suffisso `Z`. Non è ora locale: lo sarebbe stata in disaccordo di 2h con `entry_ts`/`window_ts` e andrebbe all'indietro al cambio d'ora |
| `window_ts` | **inizio della finestra da 5 minuti — raggruppare per questo campo** (§2). Verificato identico allo `window_ts` di `_process_window`, all'epoch dello slug e al `t0` del tape PolyML |
| `coin`, `side`, `token_id`, `market_id`, `question` | identificazione |
| `quoted_raw` | `outcomePrices` grezzo di Gamma, **a piena precisione** |
| `quoted_mid_cents` | il mid **troncato ai centesimi** che il cancello ha realmente confrontato. `_parse_prices` tronca (`0.8557 → 85`), quindi ha fino a 1¢ di errore, tutto verso il basso: **più grande del premio ask-sul-mid di 0.8¢ che vogliamo misurare.** Per la precisione usare `quoted_raw` |
| `booked_fill_cents` | il prezzo effettivamente registrato |
| `realized_markup_cents` | `booked − quoted`, cioè quanto è **davvero** costato il modello di slippage. Vale 0 ogni volta che il modello ha girato senza cambiare nulla (difetto 1) |
| `slippage_pct_modeled` | percentuale prevista dal modello (≠ costo reale: vedi sopra) |
| `size`, `cost_cents`, `payout_cents`, `pnl_cents`, `won` | economia del trade |
| `seconds_left_at_entry` | secondi residui alla chiusura; `-1` sui percorsi che non lo passano |
| `open_at_entry` | posizioni già aperte (per il damper di correlazione e §12) |
| `streak` | poll consecutivi in banda |
| `max_entry_allowed_cents` | tetto del cancello win-rate in quell'istante. **`null` sui percorsi di early-entry e di fallback a scadenza**, che non ricevono `remaining_s` e **scavalcano del tutto il cancello**: un numero qui farebbe sembrare violato un vincolo mai applicato |
| `bucket_win_rate_at_entry` | win rate del bucket **al momento della decisione** |
| `bucket`, `liquidity_usd`, `invest_usd`, `balance_after_cents` | contesto |
| `mode` | `paper` o `live` — **mai mescolare i due nella stessa analisi** |

`bucket_stats.json` resta autorevole per i conteggi aggregati. Questo file è la fonte per tutto il
resto.

**Dati esterni utilizzabili:** `../PolyML/data/live/book_tape_5m.parquet` copre gli **stessi
mercati** (stesso formato di slug, stesse 5 coin) con bid/ask reali a −30/−10/−2s e +100/+160/+200s,
prezzi di esecuzione (`fill5`, `fill169`), profondità in vendita, `exit5` ed esito (`resolved_up`).
È l'unica fonte che misura il prezzo **realmente pagabile**; il nostro bot vede solo il mid di Gamma.

---

## 9. Domande aperte

1. **Il nostro trigger seleziona avversamente?** (§4.3 — la domanda più importante)
2. Il residuo di +2.2pp nella banda 0.85-0.90 sopravvive fuori campione?
3. La cella 92-96¢ (+3.70%, l'unica oltre 2 SE) è reale o è 1 su 8 confronti?
4. Quanto vale davvero il premio ask-sul-mid sui *nostri* ingressi, non su campioni a istanti fissi?
5. La correlazione intra-finestra (phi=0.14) è stabile, e quanto riduce il campione effettivo?
6. Le feature di PolyML (momentum e dispersione dello spot) predicono il residuo? Leggere prima
   `../PolyML/RESULTS.md` e `../PolyML/reports/` per non ripercorrere vicoli ciechi.
