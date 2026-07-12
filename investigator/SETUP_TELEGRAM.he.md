# מדריך הפעלה: התראות סוכן ה-AML לטלפון

מדריך צעד-אחר-צעד להפעלת הסוכן האוטונומי כך שיסרוק **פעמיים ביום** וישלח לך
התראה בטלפון - **גם כשהמחשב כבוי**. כל הריצה על GitHub (חינם), אין צורך בשרת.

## איך זה עובד

```
GitHub Actions (cron, פעמיים ביום, רץ גם כשהמחשב כבוי)
   └─ run_nightly  : rerank של ה-master → top-K → דוסיירים
   └─ publish_site : דוסיירים → GitHub Pages (אתר)
   └─ notify       : digest → בוט טלגרם → 📱 התראה בטלפון (עם לינק לדוסייר)
```

הכל עובד **גם בלי שום מפתח** - במצב `mock` (בחינם, בלי BigQuery ובלי LLM):
תקבל digest של ה-top leads. כדי לקבל דוסיירים מלאים (עם גרף אמיתי מ-BigQuery
+ ניתוח LLM) מוסיפים מפתחות בשלב 2.

> **חשוב - נתוני הריצה הראשונה הם דמו.** קובץ ה-`master` האמיתי נוצר רק כשמריצים
> את המחברת (`Crypto-AML-Analysis.ipynb`) והוא לא נשמר בריפו. כדי שהצינור לא
> יקרוס, הריפו כולל טבלת דוגמה קטנה (`investigator/data/sample_master.csv`,
> מכתובות NBCTF ציבוריות ושורות הייחוס). ההתראה והאתר יסומנו בבירור **DEMO DATA**.
> כדי לקבל לידים אמיתיים: הרץ את המחברת פעם אחת, העלה (commit) את
> `suspicious_wallets_master.csv` לריפו - ואז הסריקה תשתמש בו במקום בדוגמה.

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

מכאן זה אוטומטי: פעמיים ביום, **06:00 ו-18:00 UTC** (≈ 09:00 ו-21:00 בישראל).

---

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

## הערה

כל דוסייר וכל התראה הם **כיווני חקירה (research lead), לא קביעת אשמה.** כל טענה
בדוסייר מצוטטת לתצפית של כלי, וה-trace המלא מצורף. זיהוי אישי נשאר לאכיפה
מוסמכת בלבד (צו KYC לבורסה).
