# מדריך הפעלה: התראות סוכן ה-AML לטלפון

מדריך צעד-אחר-צעד להפעלת הסוכן האוטונומי כך שיסרוק **פעם ביום** וישלח לך
התראה בטלפון - **גם כשהמחשב כבוי**. כל הריצה על GitHub (חינם), אין צורך בשרת.

## איך זה עובד

```
GitHub Actions (cron, פעם ביום ב-12:00, רץ גם כשהמחשב כבוי)
   └─ run_nightly  : rerank של ה-master → top-K → דוסיירים
   └─ publish_site : דוסיירים → GitHub Pages (אתר)
   └─ notify       : digest → בוט טלגרם → 📱 התראה בטלפון (עם לינק לדוסייר)
```

הכל עובד **גם בלי שום מפתח** - במצב `mock` (בחינם, בלי BigQuery ובלי LLM):
תקבל digest של ה-top leads מתוך ה-`master` הקיים. כדי לקבל דוסיירים מלאים
(עם גרף אמיתי מ-BigQuery + ניתוח LLM) מוסיפים מפתחות בשלב 2.

---

## שלב 1 - יצירת בוט טלגרם

1. בטלגרם, חפש את **@BotFather** ופתח איתו צ'אט.
2. שלח `/newbot`, בחר שם ושם-משתמש לבוט (חייב להסתיים ב-`bot`).
3. BotFather יחזיר **Token** בצורה `123456789:AAF...` - **העתק אותו**. זה
   `TELEGRAM_BOT_TOKEN`.
4. פתח צ'אט עם הבוט החדש שלך ושלח לו הודעה כלשהי (למשל `hi`) - חובה, אחרת
   טלגרם לא ייתן לבוט לשלוח לך.
5. מצא את ה-**chat id** שלך: פתח בדפדפן
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   (החלף `<TOKEN>` ב-token שלך). חפש `"chat":{"id":123456789` - המספר הזה הוא
   `TELEGRAM_CHAT_ID`.

## שלב 2 - הגדרת Secrets ב-GitHub

בריפו: **Settings → Secrets and variables → Actions**.

**חובה (בשביל ההתראה):** לחץ *New repository secret* והוסף:

| שם | ערך |
|---|---|
| `TELEGRAM_BOT_TOKEN` | ה-token מ-BotFather |
| `TELEGRAM_CHAT_ID` | ה-chat id שלך |

**רשות (בשביל דוסיירים אמיתיים במקום mock):**

| סוג | שם | ערך |
|---|---|---|
| Secret | `ANTHROPIC_API_KEY` | מפתח Anthropic |
| Secret | `GCP_SA_KEY` | תוכן ה-JSON של service-account ל-BigQuery |
| Variable | `BQ_PROJECT` | מזהה פרויקט ה-GCP שלך |
| Variable | `INVESTIGATOR_LLM_PROVIDER` | `anthropic` |

> בלי הרשות - הכל עדיין רץ ב-`mock` ותקבל digest. עם הרשות - תקבל דוסיירים מלאים.

## שלב 3 - הפעלת GitHub Pages

**Settings → Pages → Source: GitHub Actions.** (זהו, לא צריך לבחור ענף.)

## שלב 4 - הרצת בדיקה עכשיו

**Actions → Investigator scan → Run workflow.** אחרי דקה-שתיים אמורה להגיע
התראה בטלגרם, והדוסיירים יעלו לאתר ה-Pages
(`https://<שם-המשתמש>.github.io/<שם-הריפו>/`).

מכאן זה אוטומטי: פעם ביום ב-**12:00 בישראל** (09:00 UTC).

---

## פקודות דו-כיווניות (הבוט מגיב לפקודות שלך בטלגרם)

מעבר להתראה האוטומטית, הבוט מקבל פקודות ממך בזמן אמת. workflow שני
(`.github/workflows/investigator-bot.yml`) בודק כל 5 דקות אם שלחת הודעה,
ומגיב. מותקן אוטומטית כשהריפו על main - אין מה להגדיר.

| פקודה | מה קורה |
|---|---|
| `/report` | דוח PDF מלא (מ-`report.html`) + master CSV מלא כקבצים מצורפים |
| `/scan` | ה-digest הנוכחי - top-3 לכל שרשרת (טקסט קצר) |
| `/top ethereum 10` | טופ-10 לפי שרשרת (ethereum / tron / bitcoin / zcash), עד 25 שורות |
| `/wallet 0xab...` | פירוט של ארנק בודד מתוך ה-master |
| `/stats` | סטטיסטיקה: כמה שורות לכל שרשרת, פיקי `actionability`, כמה `has_exchange_anchor` וכו' |
| `/help` | רשימת הפקודות |

**חשוב**: כל ריצת cron מתוזמנת גם מצרפת אוטומטית את **PDF + CSV** להודעת ה-digest,
כך שאתה מקבל בטלפון דוח מלא (KPIs, טופ-50 מובילים, bridges, מדינות, NBCTF) +
טבלת master מלאה של הארנקים החשודים להמשך חקירה עצמאית. הדוח מיוצר מ-`report.html`
של המחברת ומעוצב כמו הדשבורד ב-Streamlit.

**השהייה**: עד 5 דקות (מרווח ה-cron; לפעמים GitHub מוסיף עוד כמה דקות בעומס).
**אבטחה**: הבוט משיב **רק** ל-`TELEGRAM_CHAT_ID` שהוגדר ב-secrets. אם מישהו
אחר ימצא את הבוט, לא יקבל כלום.
**מאגר הנתונים**: הבוט קורא את `suspicious_wallets_master.csv` שבריפו. עדכון
של הקובץ (רק push ל-main) - התשובות מתעדכנות מיד.

## התאמות

- **תדירות/שעות:** ערוך את שתי שורות ה-`cron` בקובץ
  `.github/workflows/investigator-scan.yml`.
- **ערוץ אחר במקום טלגרם:** משתנה `NOTIFY_CHANNEL=email` (עם `SMTP_*` +
  `NOTIFY_EMAIL_TO`) או `NOTIFY_CHANNEL=ntfy` (עם `NTFY_TOPIC`, לצד אפליקציית
  ntfy בטלפון).
- **כמה מועמדים בהתראה:** קלט `top_k` בהרצה הידנית (ברירת מחדל 3 לכל שרשרת).

## בדיקה מקומית (רשות, כשהמחשב דלוק)

```bash
# מדפיס את ההודעה בלי לשלוח (dry-run) אם אין token:
python -m investigator.notify

# שליחה אמיתית מהמחשב:
#   PowerShell:
$env:TELEGRAM_BOT_TOKEN='...'; $env:TELEGRAM_CHAT_ID='...'; python -m investigator.notify
```

## סריקה יומית אוטומטית (Refresh workflow)

Workflow שלישי - `investigator-refresh.yml` - רץ **פעם ביום ב-10:00 בישראל
(07:00 UTC)**, מריץ את המחברת מקצה לקצה על BigQuery, מגלה ארנקים חדשים, וכותב
`master.csv` מעודכן חזרה ל-main עם `[skip ci]` כדי לא להפעיל אוטומטית סריקה
מיותרת. שעתיים אחר כך (12:00 בישראל) - ה-cron של ה-scan קורא את ה-master
החדש ושולח לך את ה-PDF+CSV המעודכנים.

### מה שצריך להגדיר פעם אחת (חד-פעמי)

בריפו → **Settings → Secrets and variables → Actions**:

| סוג | שם | ערך | מקור |
|---|---|---|---|
| Secret | `GCP_SA_KEY` | תוכן ה-JSON של Service Account ל-BigQuery | GCP Console → IAM → Service Accounts → Keys → Add Key (JSON) |
| Variable | `BQ_PROJECT` | ה-project id שלך ב-GCP | GCP Console → כותרת עליונה |
| Variable | `INVESTIGATOR_BQ_KILL_USD_DAILY` | `2` | תקרה יומית ב-USD (בקוד ה-kill-switch) |
| Variable | `BQ_MAX_GB` | `200` | תקרה פר-query בג'יגה |
| Variable | `LOOKBACK_DAYS` | `90` | חלון סריקה ETH (ברירת מחדל 90 יום) |
| Variable | `TRON_LOOKBACK_DAYS` | `7` | חלון Tron (7 יום) |

### שכבת הגנה 1 - Kill-switch בקוד

`config.run_query` מונה את סך העלות שנצברה במהלך הריצה. בכל query בודק אם
המעבר עליו יחצה את `INVESTIGATOR_BQ_KILL_USD_DAILY` - אם כן, זורק חריגה
וה-workflow נכשל אדום לפני שיתחייב עוד cent. **התקרה הזו היא ההגנה החזקה
ביותר**, כי היא בתוך ה-Python session ולא תלויה בפילטרים חיצוניים.

### שכבת הגנה 2 - Billing Alert ב-GCP (חובה להוסיף)

זו הרשת ביטחון החיצונית: אם משהו משתולל מחוץ ל-CI (למשל אתה מריץ ידנית),
Google ישלח לך מייל אזהרה. **חד-פעמי, 5 דקות**:

1. פתח [GCP Billing → Budgets & alerts](https://console.cloud.google.com/billing/budgets)
2. בחר את חשבון החיוב שמקושר ל-`BQ_PROJECT` שלך
3. **CREATE BUDGET** → תן שם: `AML Investigator daily cap`
4. **Scope**: Projects → בחר את `BQ_PROJECT` שלך
5. **Amount**:
   - Budget type: **Specified amount**
   - Target amount: `10` (USD) - כל חודש
6. **Actions - Set your budget alerts**:
   - התראה ב-**50%** ($5) - email
   - התראה ב-**90%** ($9) - email
   - התראה ב-**100%** ($10) - email
   - סמן ✅ **Send alerts to billing admins and users**
7. **FINISH**.

כשהחשבון יעבור סף - תקבל מייל **מיידית**. זה לא עוצר חיוב אוטומטית, אבל
נותן לך זמן לפעול לפני שהעלות עולה מדרגה.

### עלות משוערת

- ריצה מלאה = **~$1** לפי הרצת הייחוס (~160GB)
- 1× ליום × 30 = **~$30/חודש גולמי**
- מינוס 1TB חינם/חודש (~$6.25) = **~$24/חודש נטו**
- חשבון GCP חדש? יש **$300 קרדיט חינם ל-90 יום** - שלושה חודשים ראשונים ללא עלות בכלל
- Kill-switch יעצור לפני $2/יום = תקרה של $60/חודש

## הערה

כל דוסייר וכל התראה הם **כיווני חקירה (research lead), לא קביעת אשמה.** כל טענה
בדוסייר מצוטטת לתצפית של כלי, וה-trace המלא מצורף. זיהוי אישי נשאר לאכיפה
מוסמכת בלבד (צו KYC לבורסה).
