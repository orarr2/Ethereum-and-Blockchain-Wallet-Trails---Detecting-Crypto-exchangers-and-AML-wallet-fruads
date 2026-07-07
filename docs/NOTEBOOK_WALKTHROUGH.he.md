# מסמך הליכה תא-אחר-תא של המחברת Crypto-AML-Analysis

מסמך זה עובר על כל 56 התאים של `Crypto-AML-Analysis.ipynb` ועונה לכל תא:

- **מה הוא עושה** - המטרה התפעולית בפסקה אחת.
- **APIs / SQL שהוא נוגע בהם** - איזה שירות חיצוני או dataset ציבורי של
  BigQuery מופעל, והשליפה המייצגת שהוא מריץ.
- **מה הוא מייצר** - ה-DataFrame / graph / dict שהתאים הבאים משתמשים בו.

Datasets ציבוריים ב-BigQuery שנעשה בהם שימוש:

| רשת       | Dataset                                                          |
|-----------|------------------------------------------------------------------|
| Ethereum  | `bigquery-public-data.crypto_ethereum` (`token_transfers`, `contracts`) |
| Tron      | `bigquery-public-data.goog_blockchain_tron_mainnet_us` (`decoded_events`) |
| Bitcoin   | `bigquery-public-data.crypto_bitcoin` (`transactions`)           |
| Zcash     | `bigquery-public-data.crypto_zcash` (`transactions`)             |

מקורות HTTP ציבוריים שנעשה בהם שימוש: מאגר ה-OFAC (מירר של 0xB10C),
CryptoScamDB, מירר NBCTF (נשמר בתוך הריפו הזה תחת
`data/nbctf_addresses.json`), JSON תוויות הקהילה של Etherscan (מירר של
brianleect), חוזה ה-Chainalysis לסנקציות דרך `isSanctioned` (view call
על-שרשרת דרך RPC ציבורי של Ethereum), ורזולוציית ENS הפוכה דרך RPC ציבורי
של Ethereum.

שום דבר במחברת לא בתשלום או תחת רישוי - הצינור מתוכנן כך שכל אנליסט
עם billing project ב-GCP יכול לשחזר את הכל מקצה לקצה.

---

## סעיף 1 - התקנה ואימות

### תא 0 - markdown, כותרת ותקציר
כותרת ראשית: המחברת מזהה מועמדים למחליפים בלתי-פורמליים של USDT ולידים
לקרבה למימון טרור על פני Ethereum, Tron, Bitcoin ו-Zcash. מציין את שני
העידונים המרכזיים (dust floor, נתיב TRC-20) ואת ההיקף המכוון (סף כניסה
`$10K-$2M`, עד `5,000` שולחים ייחודיים).

### תא 1 - markdown, "Setup and authentication"
כותרת סעיף.

### תא 2 - קוד, אימות מול BigQuery
מבקש מהמשתמש `BQ_PROJECT` ו-`BQ_ACCESS_TOKEN` דרך `getpass.getpass`
(קלט מוסתר). בונה אובייקט `google.oauth2.credentials.Credentials` עם
`refresh_handler` ו-`expiry` נאיבי (UTC ללא tzinfo, כפי ש-`google.auth`
דורש), ואז יוצר `bigquery.Client`. אם ה-token ריק - נופל חזרה ל-gcloud
Application Default Credentials.

- APIs: `google.cloud.bigquery`, `google.oauth2.credentials`.
- מייצר: את המשתנה הגלובלי `client` בו משתמש כל תא SQL בהמשך.

קטע מייצג:
```python
BQ_PROJECT      = getpass.getpass("1) GCP project ID or number: ").strip()
BQ_ACCESS_TOKEN = getpass.getpass("2) BigQuery access token (blank = use gcloud ADC): ").strip()
...
client = bigquery.Client(project=BQ_PROJECT, credentials=_make_token_credentials(BQ_ACCESS_TOKEN))
```

### תא 3 - markdown, "Config - datasets, contracts, thresholds"

### תא 4 - קוד, קבועי תצורה
כל הנתיבים ל-datasets, כתובות חוזה ה-USDT (hex ל-ERC-20 ו-hex/base58 ל-TRC-20),
חלונות ה-lookback הפר-רשת, מגבלות התוצאות הפר-רשת, ספי הזיהוי (טווח
שולחים ייחודיים, טווח USDT, dust floor, מקסימום עסקאות יוצאות), תקרת
העלות `MAX_GB_PER_QUERY` (150 GB, קשיח דרך `maximum_bytes_billed`), וסט
כתובות של בורסות מוכרות שמוחרגות מהמועמדים.

- מייצר: שמות שכל תא בהמשך קורא.

קטע מייצג:
```python
MIN_DISTINCT_SENDERS = 50
MAX_DISTINCT_SENDERS = 5_000
MIN_IN_USDT          = 10_000
MAX_IN_USDT          = 2_000_000
MIN_MEDIAN_USDT      = 50
MAX_OUT_TX           = 25
MAX_GB_PER_QUERY     = float(os.environ.get("BQ_MAX_GB", 150))
```

## סעיף 2 - עוזרי שליפה

### תא 5 - markdown, "Query helpers (live)"

### תא 6 - קוד, `run_query` / `run_query_chunked` / `cached`
עוטף את `client.query` ב-`QueryJobConfig(maximum_bytes_billed=...)` כך שכל
משפט SQL חסום מצד השרת. `run_query_chunked` מפצל מערך כתובות שגדול
מ-`ADDR_CHUNK_SIZE` (5,000 ברירת מחדל) למספר שליפות
`IN UNNEST(@addrs)` ומרכיב את התוצאות. `cached(name, fn, key)` שומר את
ה-return של `fn()` כ-pickle ב-`_cache/name_<sha1>.pkl` כך שריצה חוזרת
מדלגת על BigQuery כשהפרמטרים לא השתנו.

- APIs: `QueryJobConfig`, `ArrayQueryParameter` של BigQuery.
- מייצר: את העוטפים שכל תא לזיהוי מועמדים משתמש בהם.

קטע מייצג:
```python
def run_query(sql: str, params=None, max_gb: float | None = None) -> pd.DataFrame:
    cap_gb = MAX_GB_PER_QUERY if max_gb is None else max_gb
    cfg = bigquery.QueryJobConfig(maximum_bytes_billed=int(cap_gb * 1024**3))
    if params: cfg.query_parameters = params
    job = client.query(sql, job_config=cfg)
    df  = job.to_dataframe(create_bqstorage_client=False)
    billed_gb = (job.total_bytes_billed or 0) / 1024**3
    print(f"Billed: {billed_gb:,.3f} GB (~${billed_gb/1024*PRICE_PER_TB_USD:,.4f}) | rows={len(df):,}")
    return df
```

## סעיף 3 - זרעי OFAC ושכבת risk attribution

### תא 7 - markdown, "OFAC sanctioned seeds + Tron base58->hex"

### תא 8 - קוד, טעינת זרעי OFAC
מושך שלוש רשימות של OFAC (`ETH`, `USDT`, `XBT` ל-Bitcoin, `ZEC`) מהמירר
הציבורי `0xB10C/ofac-sanctioned-digital-currency-addresses`. בנוסף
ממיר כתובות base58 של TRX ל-hex כדי שיהיה אפשר להתאים אותן לשדות ה-hex
של `crypto_ethereum`.

- API: `https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/lists/sanctioned_addresses_<sym>.json`
- מייצר: הסטים `SANCTIONED_ETH`, `SANCTIONED_BTC`, `SANCTIONED_ZEC`,
  `SANCTIONED_TRON`.

### תא 9 - markdown, "Risk Attribution - multi-source wallet flagging"

### תא 10 - קוד, רגיסטר risk-attribution רב-מקורי
בונה `RISK_TAGS = {addr_lower: {"source:category", ...}}` שמאחד:

1. OFAC (מתא 8).
3. CryptoScamDB (`https://api.cryptoscamdb.org/v1/addresses`).
5. תגי Etherscan Phish/Hack - מוזרמים בעצלנות אחרי טעינת ה-labels ב-סעיף 9.
8. NBCTF (המשרד הלאומי למאבק במימון טרור בישראל) - נטען מתוך
   `data/nbctf_addresses.json` בריפו, ובמקרה של כשל נופל למירר מרוחק.
   שומר את המטא-דאטה של NBCTF (`order`, `affiliation`, `url`) ב-`NBCTF_META`
   כדי שתאי הפלט יוכלו להראות provenance.
2. חוזה Chainalysis לסנקציות (`0x40C57923...`) - נדחה עד תא ה-enrichment
   כדי לחסוך RPS על ה-RPC הציבורי.

מגדיר `annotate_risk(df, wallet_col)` שמוסיף את העמודות `risk_tags`,
`risk_categories`, `risk_source_count`, `risk_attribution_score` (0-100),
`is_known_risk`, ו-`confirmed` (מוגדר True רק כשיש >=2 מקורות עצמאיים
או כשיש קטגוריית "sanctions" או "terror").

- APIs: HTTP (OFAC, CryptoScamDB, NBCTF), on-chain view call
  (`isSanctioned(address)`).
- מייצר: `RISK_TAGS`, `NBCTF_META`, `annotate_risk`,
  `query_chainalysis_oracle`.

## סעיף 4 - זיהוי מועמדים על Ethereum ERC-20

### תא 11 - markdown, "Candidate detection - Ethereum USDT (ERC-20)"

### תא 12 - קוד, שליפת המועמדים ל-Ethereum
לב הזיהוי על ETH. מקבץ כל העברת USDT-ERC20 של `LOOKBACK_DAYS` האחרונים
(90 יום ברירת מחדל) לפי המקבל, מחשב פר-ארנק את `in_cnt`, `distinct_senders`,
`in_usdt`, `in_median_usdt`, `active_days`, `hours_of_day_active`,
`night_cnt`, `round100_cnt`, ואז מבצע JOIN עם סטטיסטיקות היציאה
פר-ארנק (`out_cnt`, `distinct_recipients`, `out_usdt`). מסנן על:

- `distinct_senders BETWEEN 50 AND 5_000`
- `out_cnt <= 25`
- `in_usdt BETWEEN 10_000 AND 2_000_000`
- `in_median_usdt >= 50` (ה-dust floor)
- הארנק לא ב-set של הבורסות המוכרות

- יעד SQL: `bigquery-public-data.crypto_ethereum.token_transfers`.
- מייצר: המחרוזת `eth_sql` (רצה בתא הבא).

קטע (זה ה-WHERE שקובע את "צורת המשפך"):
```sql
WHERE i.distinct_senders BETWEEN 50 AND 5000
  AND COALESCE(o.out_cnt,0) <= 25
  AND i.in_usdt BETWEEN 10000 AND 2000000
  AND i.in_median_usdt >= 50
  AND i.wallet NOT IN (<known-exchange addresses>)
ORDER BY i.distinct_senders DESC, i.in_usdt DESC
LIMIT 10000
```

### תא 13 - קוד, הרצת שליפת ETH
`eth = run_query(eth_sql)`. הרצה + תצוגה.

### תא 14 - markdown, "Drop smart contracts -> keep EOA only"

### תא 15 - קוד, סינון חוזים חכמים
מבצע JOIN מול `crypto_ethereum.contracts`, שומר רק Externally-Owned
Accounts. זה מה שהופך "כל כתובת" ל"ארנק שמופעל על ידי אדם".

- SQL: `SELECT address FROM crypto_ethereum.contracts WHERE address IN UNNEST(@addrs)`.
- מייצר: `eth_eoa` (רק EOA).

## סעיף 5 - זיהוי מועמדים על Tron TRC-20

### תא 16 - markdown, "Candidate detection - Tron USDT (TRC-20)"

### תא 17 - קוד, שליפת המועמדים ל-Tron
אותה מבנה כמו ה-ETH detector אבל קורא אירועי Transfer של TRC-20 מהדאטהסט
הציבורי של Tron ב-Google Cloud. שדה ה-`args` של האירוע הוא מערך JSON
`[from, to, value]`; ה-SQL שולף כל אחד ב-`JSON_VALUE` ומחלק את `value`
ב-`10^USDT_DECIMALS` (6).

- יעד SQL: `bigquery-public-data.goog_blockchain_tron_mainnet_us.decoded_events`
  עם סינון `address = <USDT_TRC20_HEX>` ו-`event_signature = 'Transfer(...)'`.
- מייצר: `tron_sql`.

### תא 18 - קוד, הרצת Tron + הנדסת פיצ'רים התנהגותיים
מריץ את `tron_sql`, מסנן כתובות שמופיעות גם כ-`decoded_events.address`
(חוזים), ואז מגדיר ומיישם:

- `add_behavioural_features(df)` -> `night_share`, `round100_share`,
  `pass_through` (=`out_usdt/in_usdt`), `is_accumulator`,
  `human_hours`, `human_schedule`.
- `informal_exchanger_score(df)` -> ציון קומפוזיטי ב-[0,1] מעל
  `distinct_senders`, `round100_share`, איזון של `pass_through`,
  לוח זמנים אנושי, לא-accumulator, ובונוס קטן כאשר
  `distinct_recipients in [2, 6]`.

מיישם על שני הצדדים (`eth_eoa`, `tron_eoa`) ומייצר את
`eth_ranked` ו-`tron_ranked`.

## סעיף 5b - זיהוי מועמדים על Bitcoin

### תא 19 - markdown, "Bitcoin (BTC) funnel-account detection"

### תא 20 - קוד, שליפת המועמדים ל-BTC והרצתה
קורא את `crypto_bitcoin.transactions` (partitioned by
`block_timestamp_month` - חיסכון עלות דרך partition-pruning). עושה UNNEST
ל-`outputs.addresses` כדי לבנות זרימת `(tx_hash, block_timestamp,
out_value_btc, recipient)`, מבצע JOIN על `tx_hash` עם זרימת
`inputs.addresses` כדי לייחס שולח לכל פלט, ואז מקבץ פר-ארנק עם אותם
פיצ'רי fan-in / fan-out / לילה / active-days כמו ב-ETH. מחשב `funnel_signal`
ישר ב-SQL, כך שה-`LIMIT` הפר-רשת חותך את הזנב (חלשים) ולא את הראש
(חזקים):

```sql
LN(1.0 + i.distinct_senders)                    -- fan-in
+ 2.0 * (1.0 / (1.0 + COALESCE(o.out_cnt, 0)))  -- concentration bonus
+ 0.5 * LN(1.0 + i.night_cnt)                   -- off-hours
+ 0.3 * LN(1.0 + i.active_days)                 -- long-lived
```

- יעד SQL: `bigquery-public-data.crypto_bitcoin.transactions`.
- מייצר: `btc` DataFrame.

## סעיף 5c - זיהוי מועמדים על Zcash שקוף

### תא 21 - markdown, "Zcash (ZEC) transparent funnel-account detection"

### תא 22 - קוד, שליפת המועמדים ל-ZEC והרצתה
אותו pattern של transactions מחולק ל-partitions כמו ב-BTC. בנוסף מחשב
`deshield_in_share` פר-מקבל: החלק היחסי של העסקאות שהצד ה-input שלהן היה
`addresses IS NULL` או מערך ריק, מה שמייצג על-שרשרת boundary של
shielded pool -> t-addr (unshielding). רק תעבורה שקופה (`t-addr`) גלויה
על-שרשרת; z-addrs מחוץ להיקף באופן מפורש.

- יעד SQL: `bigquery-public-data.crypto_zcash.transactions`.
- מייצר: `zec` DataFrame (ריק בחלונות שקטים - מצופה).

## סעיף 5d - איחוד ה-pool

### תא 23 - markdown, "Unify the candidate pool across all 4 chains"

### תא 24 - קוד, נירמול + concat
Cast של כל תאי `Decimal` ל-`float`, ואז שכתוב של הפריימים הפר-רשת
לסכמה משותפת (`chain`, `asset`, `raw_value_in`, `raw_value_out`,
`raw_value_median`, `value_in_usd_est`, `value_unit`) תוך שימוש
ב-`BTC_PRICE_USD` וב-`ZEC_PRICE_USD` לצורך דירוג בין-רשתי בדולרים.

- מייצר: `all_candidates` (שורה אחת פר-ארנק מועמד, סט עמודות אחיד).

## סעיף 6 - גרף העסקאות

### תא 25 - markdown, "Build the transaction graph (Layer 2)"

### תא 26 - קוד, שליפות edges פר-רשת + `nx.DiGraph`
לכל רשת מוגדרת פונקציה `fetch_edges_<chain>(wallets)` שמושכת כל העברה
שאחד מקצוותיה הוא מועמד שלנו (`from_address IN UNNEST(@addrs) OR
to_address IN UNNEST(@addrs)`). BTC ו-ZEC משתמשים ב-pattern של
transactions מחולק ל-partitions כדי להישאר זולים. `build_graph` מקבץ
edges לפי `(from, to)` ומכניס אותם ל-`networkx.DiGraph` עם משקלים
`total_usdt` ו-`tx_count`. התוצאות עוברות pickle דרך `cached()` כך שריצה
חוזרת מדלגת על BQ.

- יעדי SQL: `crypto_ethereum.token_transfers`, `decoded_events`,
  `crypto_bitcoin.transactions`, `crypto_zcash.transactions`.
- מייצר: `edges_eth`, `edges_tron`, `edges_btc`, `edges_zec`,
  `G`, `G_tron`, `G_btc`, `G_zec`.

## סעיף 7 - פיצ'רים על הגרף

### תא 27 - markdown, "Features (Layer 3) - graph + mixer/funnel + sanctioned proximity"

### תא 28 - קוד, `graph_features`
לכל גרף מחשב weighted PageRank, in/out degree, מקדם clustering, נפח
in/out ב-USDT, וספירת in/out tx. בנוסף גוזר:

- `mixer_score = degree_balance * value_balance * multi_hop_indicator`
- `funnel_score = LN(1 + in_degree) * (1 / (1 + out_degree))`
- `hops_to_sanctioned` דרך BFS דו-כיווני
  (`hops_to_sanctioned` הולך גם קדימה וגם על ה-reverse graph ולוקח את
  ה-min; זה תיקון באג מגרסה קודמת שהלכה רק על edges יוצאים).
- `is_sanctioned` (המועמד עצמו ב-OFAC).

- APIs: `networkx.pagerank`, `networkx.clustering`.
- מייצר: `feats_eth`, `feats_tron`, `feats_btc`, `feats_zec`.

## סעיף 8 - ציון anomaly

### תא 29 - markdown, "Unsupervised anomaly scoring (Layer 3) -> risk score"

### תא 30 - קוד, `score` -> Isolation-Forest + `risk_score` משוקלל
Standard-scale על עשרת הפיצ'רים הגרפיים/התנהגותיים פר-רשת, fit של
`IsolationForest(n_estimators=300, contamination=0.05)`, ואז שילוב:

```
risk_score = 60 * anomaly_scaled + 25 * informal_scaled + 15 * proximity_scaled
```

ה-fit על סט המועמדים שזוהו בלבד (לא על כל אוכלוסיית הגרף), כך שהמודל
מבחין בין המועמדים לבין עצמם ולא בין מועמד לבין מפקיד רגיל. כל שורה עם
`is_sanctioned=True` נכתבת מעל ל-`risk_score = 100`.

- APIs: `sklearn.ensemble.IsolationForest`,
  `StandardScaler`, `MinMaxScaler`.
- מייצר: `scored_eth`, `scored_tron`, `scored_btc`, `scored_zec`, ופריים
  מאוחד `scored`.

## סעיף 9 - Enrichment (תוויות)

### תא 31 - markdown, "Enrichment (Layer 1+) - human-readable labels"

### תא 32 - קוד, טעינת תוויות Etherscan + Chainalysis oracle
מוריד את ה-JSON של קהילת התוויות
(`brianleect/etherscan-labels/main/data/etherscan/combined/combinedAllLabels.json`)
וממזג `label_name` / `label_tags` ל-`scored`. תגי `phish-hack` /
`fake_phishing` נדחפים ל-`RISK_TAGS` כך ששכבת ה-attribution רואה אותם.
לאחר מכן מפעיל את `query_chainalysis_oracle` על תת-הקבוצה של מועמדי ETH -
עד 2,000 view calls סדרתיים של `isSanctioned(address)` כנגד RPC ציבורי של
Ethereum (`ethereum-rpc.publicnode.com` ראשון, אח"כ `cloudflare-eth.com`
/ `rpc.ankr.com` / `eth.merkle.io`).

- APIs: HTTP (JSON תוויות Etherscan), view call על-שרשרת
  (`Chainalysis SanctionsList.isSanctioned`).
- מייצר: `scored` מועשר עם עמודות תוויות, וכל hit של Chainalysis נדחף
  ל-`RISK_TAGS`.

## סעיף 10 - אבחון CTF

### תא 33 - markdown, "CTF diagnostic - are OFAC-listed addresses even active?"

### תא 34 - קוד, שליפות activity עבור OFAC (ETH + Tron)
סופר העברות USDT שבהן כתובת מסונקצנת היא צד לעסקה ב-90 הימים האחרונים
(ETH) / 30 הימים האחרונים (Tron). מאשר את התוצאה המוכרת - Tether מקפיאה
כתובות USDT מסונקצנות, אז קרבה ל-OFAC לבדה כמעט חסרת ערך כאות חי. המחברת
מזיזה את אות ה-CTF ל-behavioural/campaign shape (סעיף 13.6).

- יעדי SQL: `crypto_ethereum.token_transfers`, `decoded_events`.
- מייצר: `ofac_eth`, `ofac_tron`.

## סעיף 11 - Master frame וויזואליזציה בין-רשתית

### תא 35 - markdown, "Visualise + export"

### תא 36 - קוד, מיזוג master + פלוטים + כתיבת CSV ראשונית
- `merge_master(scored, all_candidates)` עושה left-join של פיצ'רי הגרף
  ה-scored לפריים המאוחד של המועמדים ומחזיר את סכמת `MASTER_COLS`.
- ממיין לפי `priority = 0.7 * risk_score + 0.3 * attributability`.
- כותב את `suspicious_wallets_master.csv` ואת החתכים הפר-רשת
  (`suspicious_usdt_eth.csv`, `suspicious_usdt_trc20.csv`,
  `suspicious_btc.csv`, `suspicious_zec.csv`) בשורש הריפו, כ-checkpoint
  מוקדם לפני התאים היקרים שבסעיף 13.
- מרנדר ארבע ויזואליזציות:
  1. Scatter fan-in vs fan-out בין-רשתי (`plot_cross_chain_scatter`) -
     `distinct_senders` על log-x מול `out_cnt` על y, מפוצל פר-רשת, בצבע
     של `risk_score`, טבעות אדומות סביב `is_known_risk`.
  2. רשת hub-and-spoke פר-רשת (`plot_funnel_network`) - top-40 hubs לפי
     in-degree ושכונה חסומה סביבם, ב-spring layout.
  3. עמודות provenance של תגי risk (`plot_risk_provenance`).
  4. טבלת top-50 של הלידים הכי attributable.

- מייצר: `master` DataFrame; ארבעה פלוטים inline; ה-CSV הראשוני של master.

## סעיף 13 - העמקה של זהות ו-attribution

### תא 37 - markdown, "Identity deepening / attribution"
### תא 38 - markdown, "13.1 Co-funding clustering"

### תא 39 - קוד, `cofunding_clusters`
לכל מפקיד שמימן יותר ממועמד אחד, רושם את כל הזוגות; מקבץ מועמדים לפי
edges של depositors משותפים; מסנן החוצה depositors בסגנון "hot wallet" של
CEX שמממנים יותר מ-`depositor_out_cap=100` מועמדים (אחרת כל המועמדים
נופלים לקלאסטר ענק אחד דרך Binance). מדווח על components מחוברים בגודל
>=2 כקבוצות אופרטור פוטנציאליות.

- APIs: pandas / networkx טהורים.
- מייצר: `clusters` DataFrame (`cluster_id`, `cluster_size`, `wallet`),
  `cofund_g` (`networkx.Graph` מועמד-מועמד).

### תא 40 - markdown, "13.1b Bitcoin co-spend clustering"

### תא 41 - קוד, שליפת clustering של co-spend על Bitcoin
מיישם את היוריסטיקת הבעלות הסטנדרטית של Bitcoin: כתובות שאי-פעם משתתפות
יחד כ-inputs של אותה עסקה כנראה בבעלות משותפת. מסנן החוצה עסקאות שנראות
כמו CoinJoin (>=10 inputs וכן >=90% כתובות distinct). ממפה כל כתובת
בקלאסטר ל-`entity` קנוני (הכתובת המינימלית ב-component לפי מיון), ואז
מסמן כל מועמד BTC שיושב באותה entity ככתובת BTC מסונקצנת עם התג
`btc_cospend:sanctioned_cluster` ב-`RISK_TAGS`.

- יעד SQL: `crypto_bitcoin.transactions`.
- מייצר: `btc_entities` (`entity`, `n_addresses`, `sample_addresses`); תגי
  risk חדשים ממוזגים ל-`scored` דרך `annotate_risk(scored)`.

### תא 42 - markdown, "13.2 Nearest-exchange anchor (ETH)"

### תא 43 - קוד, שליפת exchange-anchor
`exchange_addresses()` קורא את dict התוויות של Etherscan שכבר נטען
וסינון על כתובות שה-`labels` שלהן חופפים ל-set תגים מוכר של בורסות
(`binance`, `coinbase`, `kraken`, `okx`, ...). `nearest_exchange` מריץ
מעבר יחיד על `crypto_ethereum.token_transfers` ושומר רק העברות ישירות
שאחד הצדדים בהן הוא מועמד והשני הוא כתובת של בורסה מתויגת. זה עוגן
ה-KYC/subpoena - הליד הכי אקשיונבילי.

- יעד SQL: `crypto_ethereum.token_transfers`.
- מייצר: `anchors` DataFrame (`candidate`, `direction`, `exchange_addr`,
  `n_tx`, `usdt`) ואת ה-set הגלובלי `ex`.

### תא 44 - markdown, "13.3 Behavioural fingerprint"

### תא 45 - קוד, fingerprint של פעילות פר-שעת-UTC
שליפה אחת שמקבצת את ההעברות הנכנסות של כל מועמד לפי
`EXTRACT(HOUR FROM block_timestamp)` וסופרת סכומים עגולים למאה USDT.
בצד ה-Python בוחר את השעה הכי עמוסה פר-ארנק, מנחש UTC offset בתור
`(12 - peak_hour_utc) % 24` (עיגון קרב-לצהריים גס), ומחשב
`round_pct = round_100s / n_tx * 100`.

- יעד SQL: `crypto_ethereum.token_transfers`.
- מייצר: `fp` (ספירות שעתיות גולמיות), `peak` (fingerprint פר-ארנק).

### תא 46 - markdown, "13.4 ENS resolution"

### תא 47 - קוד, ENS reverse resolution
מנסה כמה RPCs ציבוריים של Ethereum, משתמש ב-`ens.ENS.from_web3` כדי
לבצע reverse resolution לכל אחד מ-100 מועמדי ETH המובילים ולקבל שם
`.eth` ראשי. skip שקט אם `web3` לא מותקן או אם אין RPC זמין.

- APIs: `web3`, `ens`, RPC ציבורי של Ethereum.
- מייצר: `ens` dict (`{address: ens_name}`).

### תא 48 - markdown, "13.5 Attributability score"

### תא 49 - קוד, ציון `attributability`
משלב ארבעה signals ל-`attributability` בטווח [0, 100]:
`+40` אם למועמד יש עוגן בורסה ישיר, `+30` אם יש לו תווית Etherscan,
`+20` אם יש לו שם ENS, `+10` אם הוא בטווח 2 hops מכתובת מסונקצנת, ועוד
חצי מה-`risk_attribution_score`. הפריים הממויין הופך ל"most attributable
leads".

- מייצר: `scored` עם עמודת `attributability` וממוין מחדש.

### תא 50 - markdown, "13.6 Behavioural CTF + modular enrichment"

### תא 51 - קוד, campaign score + anchors + bridges + geo + enrichment + report + CSVs
תא ההרכבה של ה-master וכל הפלטים. לפי הסדר:

1. `terror_signals.campaign_score(all_candidates)` מחשב
   `campaign_terror_score in [0, 100]` פר-מועמד כשילוב משוקלל של
   percentile של fan-in, קטנות של ה-median transfer, ריכוז מקבל יחיד,
   ומרכיב burst זמני אם קיים. אז
   `select_leads(..., per_chain_k=25)` מסמן את ה-top-25 פר-רשת בתור
   `is_top_campaign_lead`.
2. מייצר את `anchor_exchange`, `anchor_exchange_addr`, `anchor_usdt`,
   `anchor_links`, `has_exchange_anchor`. מגדיר
   `actionability = risk_score + 25 * has_exchange_anchor`.
3. `bridge_wallets.bridges_across_graphs({chain: G})` מריץ articulation-
   point detection על כל גרף פר-רשת, מערים תוצאות; מוסיף לכל שורת master
   `is_bridge_wallet` ו-`components_bridged`.
4. `geo_tagging.annotate_geo(...)` ממפה שמות בורסות לרשימות מדינות ואת
   שדה ה-`affiliation` של NBCTF (טקסט חופשי) לקוד מדינה; מוסיף
   `country_codes`, `country_source`, `hits_interesting_country`.
5. `enrichment.Enricher(sources=[PublicLabelsSource(labels=labels_dict)])`
   מעשיר את 100 המובילים לפי `actionability`. הערה חשובה:
   `ChainalysisOracleSource` ו-`ENSSource` נשמטים כאן בכוונה כי הם כבר
   רצו בתאים 32 ו-47 מעל אותם ארנקים - הרצה חוזרת שלהם הייתה מייצרת
   ~100 קריאות RPC ציבוריות סדרתיות פר-מקור, וזה בדיוק מה שהאט את התא
   בריצות קודמות ("לוקח נצח נצחים").
6. מחשב את
   `funnel_ratio_recipients = distinct_senders / max(1, distinct_recipients)`
   ואת `funnel_ratio_out_cnt = distinct_senders / max(1, out_cnt)`
   ואת ה-boolean `is_single_recipient = distinct_recipients <= 1`.
   זה מייצג במפורש את הריכוז "הרבה שולחים -> ארנק אחד -> מעט/יחיד
   מקבלים" כעמודות first-class ב-master, כך שאנליסט downstream יכול
   למיין ולסנן ישירות עליהן.
7. כותב CSVs ל-`data/`:
   - `data/suspicious_wallets.csv` - master ממוין מלא
   - `data/ctf_leads.csv` - חתך CTF (top-K פר-רשת או כל מועמד עם
     קטגוריית `terror`)
   - `data/suspicious_usdt_eth.csv`, `..._usdt_trc20.csv`, `..._btc.csv`,
     `..._zec.csv`
   - `suspicious_wallets_master.csv` בשורש הריפו (נתיב legacy, אותו תוכן
     כמו `data/suspicious_wallets.csv`)
8. `report_html.build_report(...)` כותב `report.html` עצמאי עם KPI cards,
   top-50 actionable leads, bridge wallets, פילוח מדינות, exchange
   anchors, קלסטרים של co-funding, ו-NBCTF hits עם provenance.

- APIs: מודולים לוקאליים (`terror_signals`, `bridge_wallets`, `geo_tagging`,
  `enrichment`, `report_html`).
- מייצר: `master` הסופי, `report.html`, וכל ה-CSVs.

## סעיף 12 - מגבלות

### תא 52 - markdown, "Limitations and responsible use"
מציין: לידים למחקר בלבד, לא הוכחה; XMR מחוץ להיקף (אין ledger ציבורי);
Zcash shielded מחוץ להיקף (מוצפן); כתובות USDT ב-OFAC רדומות כי Tether
מקפיאה אותן.

## סעיף 14 - סיכום סופי

### תא 53 - markdown, "Final summary - suspicious wallets across 4 chains"

### תא 54 - קוד, `print_summary`
מדפיס ספירות פר-רשת של ארנקים, כמה מהם עם תג risk ציבורי, כמה מהם
בקלאסטר co-funding, וספירה מצטברת של confirmed (>=2 מקורות עצמאיים או
sanctions/terror). מדפיס את גבולות הראות (visibility) כתזכורת אחרונה.

### תא 55 - קוד, תא ריק בסוף
ריק לפי מוסכמה.

---

## תשובה לשאלת ה-cross-section של unique senders/recipients

הדפוס שאתה חוקר אחריו - **הרבה שולחים ייחודיים -> ארנק אחד -> מקבל אחד
(או מעט) ייחודי** - ממומש בשלוש שכבות בקוד:

1. **שער fan-in ב-SQL** (תא 12, תא 17, תא 20, תא 22): כל מועמד עובר
   `distinct_senders BETWEEN 50 AND 5,000`.
2. **שער fan-out ב-SQL** (אותם תאים): `out_cnt <= 25`. שים לב - זה ספירת
   העסקאות היוצאות, לא ספירת מקבלים ייחודיים. ארנק ששולח 25 העברות
   לאותה כתובת יחידה עובר את שני הפילטרים, וזה בדיוק הדפוס המבוקש של
   מחליף בלתי-פורמלי.
3. **בונוסים בציונים**:
   - Bitcoin: ה-`funnel_signal` הקומפוזיטי (תא 20) נותן בונוס משמעותי
     ל-`out_cnt` נמוך דרך המחובר `2.0 * (1 / (1 + out_cnt))`.
   - `informal_exchanger_score` (תא 18) נותן בונוס אם
     `distinct_recipients` נופל בטווח "כמה בודדים" של 2-6.
   - `campaign_terror_score` (`terror_signals.py`) כולל תת-signal
     `concentration = (distinct_recipients <= 1)` במשקל 20% מהציון
     הכולל, כך שמשפכים למקבל יחיד מקבלים את בונוס הריכוז המקסימלי.

תא 51 מייצר בנוסף את היחסים המפורשים
`funnel_ratio_recipients = distinct_senders / max(1, distinct_recipients)`
ו-`funnel_ratio_out_cnt = distinct_senders / max(1, out_cnt)` ואת
ה-boolean `is_single_recipient` כעמודות ב-master frame, כך שאנליסט
downstream (או שאילתה על ה-CSV) יכול למיין ולסנן ישירות עליהן.
