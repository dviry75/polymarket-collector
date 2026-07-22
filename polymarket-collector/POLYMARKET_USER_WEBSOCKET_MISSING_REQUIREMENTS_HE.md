# Audit חוסרים ממוקד — Polymarket User WebSocket

תאריך: 2026-07-22 UTC

## 1. Executive summary

`POLY_ADDRESS` אינו חסר ל-User WebSocket ואינו נקרא כלל בלקוח ה-WebSocket הקיים. גם לפי תיעוד Polymarket הרשמי, ערכי ה-auth של ערוץ user הם רק `apiKey`, `secret`, ו-`passphrase`; ה-subscriptions הם condition IDs וה-heartbeat הוא `PING` כל 10 שניות עם `PONG` מהשרת.

החסם המוכח כרגע הוא ששלושת ה-credentials טרם אומתו מול Polymarket לאחר תיקון ה-Base64. ה-secret תקין מבנית, אך תקינות מבנית אינה מוכיחה שהשלישייה פעילה, תואמת לאותו API key, או שייכת ל-Signer הידוע. מצב `AUTH_FAILED` הקודם נוצר מסגירת החיבור לפני acknowledgment/PONG.

לפי מדיניות ההפעלה שנבחרה, הצעד הבא הוא authenticated GET read-only עם `POLY_ADDRESS` השווה לכתובת ה-Signer. רק אם ה-GET מצליח יש לאתחל את `polymarket-live.service` בלבד ולמדוד 10 דקות רצופות של `CONNECTED` ו-PONGs.

ממצא ארכיטקטוני נפרד: אין בקוד הנוכחי authenticated REST client פעיל. `RealPolymarketTradingAdapter` הוא placeholder, ועם `LIVE_ADAPTER=mock` ה-reconciliation משתמש בנתוני mock. לכן כתובת signer/funder/signature type אינן נצרכות כעת על ידי השירות לצורך WebSocket או reconciliation אמיתי.

מקורות רשמיים: [User Channel](https://docs.polymarket.com/market-data/websocket/user-channel), [WebSocket Overview and Heartbeats](https://docs.polymarket.com/market-data/websocket/overview), [CLOB Authentication](https://docs.polymarket.com/api-reference/authentication).

## 2. Root cause נוכחי

ה-Base64 padding תוקן, אך לא בוצע מאז מבחן חי שמוכיח כי שלושת פרטי ה-L2 הם סט תואם ותקף. `POLY_ADDRESS` חסר רק לצורך יצירת חמשת headers של L2 REST בבדיקת GET; הוא אינו חלק מהודעת ההרשמה של User WebSocket.

לפיכך אין כרגע ראיה מספקת לבחור בין האפשרויות הבאות:

1. ה-secret המתוקן נכון והשלישייה תעבוד לאחר restart.
2. אחד משלושת פרטי L2 עדיין שגוי, לא תואם לאחרים או לא פעיל.
3. השלישייה שייכת ל-Signer אחר.

אין לנחש. authenticated GET עם כתובת ה-Signer הידועה יכול להוכיח שהשרת מקבל את השילוב לצורך L2 GET, ללא private key וללא write. לאחר מכן חיבור User WebSocket בפועל הוא ההוכחה הנפרדת לערוץ WebSocket.

## 3. מיפוי דרישות הקוד

### `live/config.py`

- `POLYMARKET_CLOB_HOST` נקרא ב-[`live/config.py`](live/config.py#L121), עם ברירת מחדל `https://clob.polymarket.com`.
- `POLYMARKET_MARKET_WS_URL` נקרא בשורה 122 עם ברירת מחדל רשמית.
- `POLYMARKET_USER_WS_URL` נקרא בשורה 123 עם ברירת מחדל רשמית.
- `POLYMARKET_USER_WS_ENABLED` נקרא בשורה 124 עם ברירת מחדל `true`.
- `POLYMARKET_PROFILE_ADDRESS` נקרא בשורה 129. זה metadata של הפרופיל בלבד; הוא אינו משמש ל-auth של User WebSocket או ליצירת REST headers.
- `POLYMARKET_ACCOUNT_LOGIN_TYPE` נקרא בשורה 130, עם ברירת מחדל `email`.
- אין קריאה ל-`POLY_ADDRESS`, `POLYMARKET_SIGNER_ADDRESS`, `POLYMARKET_FUNDER_ADDRESS`, `POLYMARKET_SIGNATURE_TYPE`, `POLYMARKET_EXPECTED_WALLET_ADDRESS`, `POLYMARKET_PRIVATE_KEY` או chain ID.

### `live/secrets.py`

- `REQUIRED_SECRET_NAMES` כולל `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE` בשורות 10–16.
- `user_ws_credentials_configured` ב-[`live/secrets.py`](live/secrets.py#L49) בודק רק את שלושת הראשונים, בשורה 55.
- כתובת, funder, signature type, chain ID ו-private key אינם נקראים כאן.

### User WebSocket client

- `AUTH_KEYS` מכיל רק את שלושת פרטי L2 ב-[`live/market_websocket.py`](live/market_websocket.py#L124), שורות 124–127.
- `credentials()` קורא רק אותם ומייצר בדיוק `apiKey`, `secret`, `passphrase`, שורות 155–157.
- הודעת subscription מכילה `type=user`, `markets=<condition IDs>` ו-`auth`, שורות 144–148 ו-200.
- condition IDs נלקחים מ-`user_ws_condition_ids()`; השאילתה נמצאת ב-[`live/repository.py`](live/repository.py#L670), שורות 670–674.
- `PING` ראשון נשלח מיד בשורות 200–202; לאחר מכן `PING` כל 10 שניות בשורות 245–249; `PONG` מעדכן acknowledgment וזמן PONG בשורות 263–270.
- אין שימוש בכתובת Signer, Proxy/Funder, signature type, chain ID או private key.

### authenticated REST client ו-reconciliation

- אין authenticated REST client בקוד האפליקציה.
- `RealPolymarketTradingAdapter` מחזיר `not_configured`, רשימות ריקות, וחוסם create/cancel ב-[`live/adapters/polymarket.py`](live/adapters/polymarket.py#L9), שורות 9–60.
- `ReconciliationWorker` קורא `adapter.get_open_orders()`, `get_trades()` ו-`get_positions()` ב-[`live/reconciliation.py`](live/reconciliation.py#L9), שורות 14–43.
- בחירת adapter נעשית ב-[`live/router.py`](live/router.py#L43), שורות 43–55. במצב `mock`, ה-reconciliation משתמש ב-`MockTradingAdapter`, לא ב-CLOB.
- ה-SDK המותקן כן מגדיר L2 headers: `POLY_ADDRESS` נלקח מ-`signer.address()` ב-`py_clob_client_v2/headers/headers.py`, שורות 36–68. כלומר זה Signer, לא Proxy/Funder.
- בנאי ה-SDK מקבל `chain_id`, private `key`, `creds`, `signature_type`, ו-`funder` בנפרד ב-`py_clob_client_v2/client.py`, שורות 161–191. המימוש דורש signer object גם לבניית headers, אף שעל פי פרוטוקול HMAC אפשר לבצע GET באמצעות כתובת signer ציבורית וה-L2 credentials בלבד.

### adapter ו-Environment examples

- `deploy/live.env.example` מגדיר `POLYMARKET_PROFILE_ADDRESS` ו-`POLYMARKET_ACCOUNT_LOGIN_TYPE`, בשורות 35–40, אך אינו מגדיר signer/funder/signature type.
- `.env.example` מכיל placeholders עתידיים `POLYMARKET_PRIVATE_KEY`, שלושת פרטי L2, `POLYMARKET_SIGNATURE_TYPE`, `POLYMARKET_FUNDER_ADDRESS`, ו-`POLYMARKET_EXPECTED_WALLET_ADDRESS`, בשורות 59–71.
- מבין placeholders אלה, בקוד הנוכחי נקרא רק `POLYMARKET_PROFILE_ADDRESS`; האחרים אינם מחוברים למימוש.

### systemd

- `polymarket-live.service` מגדיר `EnvironmentFile=/etc/polymarket-live/live.env` ללא `-` לפני הנתיב, ולכן קובץ חסר הוא שגיאת startup.
- השירות רץ כ-`dvir:dvir`, אך systemd manager קורא את EnvironmentFile ומעביר את הערכים לתהליך. הקובץ הוא `root:root` בהרשאה `600`; השירות פעיל, `EnvironmentFiles` מצביע לנתיב המדויק, ולכן מנגנון הטעינה תקין.
- שינוי EnvironmentFile אינו משנה Environment של תהליך שכבר רץ. לאחר שינוי נדרש restart של `polymarket-live.service`; `daemon-reload` אינו נדרש כאשר unit file עצמו לא השתנה.

## 4. מיפוי זהויות מדויק

| תפקיד | ערך ציבורי | שימוש נכון |
|---|---|---|
| Signer | `0x75D4148E7220b02545f822816901836679B0F7D7` | הערך של header `POLY_ADDRESS` ב-L2 REST; זהו ה-wallet שיצר/נגזרו עבורו ה-API credentials |
| Proxy/Funder/Profile | `0xcE075637152167517e1492FcF5ff2D131686ee38` | `funder` וכתובת הפרופיל שבה מוחזקים הכספים/הפוזיציות |
| signature type | `1` (`POLY_PROXY`) | סוג חתימת order של חשבון Email/Magic קיים; רלוונטי ל-client/order builder ולפרמטר balance/allowance |
| chain ID | `137` | Polygon; נדרש לאתחול signer/L1/order signing ב-SDK, לא להודעת User WebSocket ולא לחישוב HMAC GET ידני |

תשובות מפורשות:

1. `POLY_ADDRESS` צריך להיות כתובת ה-Signer: `0x75D4148E7220b02545f822816901836679B0F7D7`.
2. `funder` צריך להיות כתובת ה-Proxy/Profile: `0xcE075637152167517e1492FcF5ff2D131686ee38`.
3. לחשבון Email/Magic הקיים המתואר, `signature_type=1` (`POLY_PROXY`). התיעוד הרשמי מתאר סוג זה כ-flow המקובל לחשבונות Magic Link email/Google.
4. אף אחד משלושת הערכים אינו נדרש להודעת User WebSocket. Signer address נדרש ל-L2 REST headers. Funder ו-signature type נדרשים ל-client/order builder ול-balance/allowance semantics/מסחר עתידי, אך לא ל-open orders/trades GET HMAC עצמו.
5. אפשר לבדוק ללא private key וללא write שהשרת מקבל את ה-API credentials יחד עם כתובת ה-Signer באמצעות authenticated GET. הצלחה מוכיחה התאמה מעשית לכתובת שנשלחה. כשל אינו בהכרח מוכיח איזה שדה שגוי. ללא בדיקה חיה או יצירה/derive מחדש באמצעות L1, אי אפשר להוכיח קריפטוגרפית מראש שהשלישייה שייכת ל-Signer.

## 5. מבנה Environment בפועל

לא הודפסו ערכים. נמצאו 35 assignments ו-35 שמות ייחודיים; אין כפילויות.

| משתנה | מצב | סוג ערך נדרש | סודי/ציבורי | הערה |
|---|---|---|---|---|
| `POLYMARKET_API_KEY` | SET | L2 API key | סודי | חובה ל-User WS ול-REST GET |
| `POLYMARKET_API_SECRET` | SET | Base64 secret, 44 chars | סודי | חובה ל-User WS ול-HMAC REST |
| `POLYMARKET_API_PASSPHRASE` | SET | L2 passphrase | סודי | חובה ל-User WS ול-REST GET |
| `POLYMARKET_PROFILE_ADDRESS` | MISSING | כתובת Proxy/Profile, 0x + 40 hex | ציבורי | נקרא בקוד כ-metadata; לא חוסם User WS |
| `POLYMARKET_ACCOUNT_LOGIN_TYPE` | MISSING | `email` | ציבורי | ברירת המחדל בקוד כבר `email` |
| `POLYMARKET_CLOB_HOST` | NOT_REQUIRED | HTTPS URL | ציבורי | חסר בקובץ אך קיימת ברירת מחדל תקינה בקוד |
| `POLYMARKET_USER_WS_URL` | NOT_REQUIRED | WSS URL | ציבורי | חסר בקובץ אך קיימת ברירת מחדל תקינה בקוד |
| `POLYMARKET_USER_WS_ENABLED` | NOT_REQUIRED | boolean | ציבורי | חסר בקובץ אך ברירת המחדל `true` |
| `POLYMARKET_SIGNATURE_TYPE` | MISSING | integer `1` | ציבורי | placeholder בלבד; אינו נקרא בקוד הנוכחי |
| `POLYMARKET_FUNDER_ADDRESS` | MISSING | כתובת Proxy/Funder | ציבורי | placeholder בלבד; אינו נקרא בקוד הנוכחי |
| `POLYMARKET_EXPECTED_WALLET_ADDRESS` | MISSING | לא מוגדר סמנטית בקוד | ציבורי | placeholder בלבד; אין לנחש שהוא `POLY_ADDRESS` |
| `POLYMARKET_PRIVATE_KEY` | NOT_REQUIRED | private key | סודי ביותר | אסור/לא נדרש ל-User WS או ל-GET הידני; מסחר עתידי בלבד |
| chain ID variable | NOT_REQUIRED | integer `137` | ציבורי | אין משתנה כזה בקוד הנוכחי |

## 6. הפרדה לפי צורך

### חובה כדי שה-User WebSocket יהיה `CONNECTED`

- שלישיית L2 תואמת, פעילה ומדויקת: API key, secret, passphrase.
- URL הרשמי; ברירת המחדל בקוד כבר תקינה.
- לפחות condition ID מנוהל אחד של BTC Up or Down 5m; אחרת הקוד עובר ל-`DISABLED`.
- subscription מיידי בפורמט הנכון; כבר ממומש.
- `PING` כל 10 שניות וקליטת `PONG`; כבר ממומש.
- restart של LIVE נדרש רק כדי שהתהליך יקרא Environment שתוקן ידנית.

`POLY_ADDRESS`, funder, signature type, chain ID ו-private key אינם חוסמי WebSocket.

### חובה כדי ש-authenticated GET reconciliation יעבוד

- לבדיקת GET עצמאית: שלושת credentials, `POLY_ADDRESS=<Signer>`, timestamp ו-HMAC תקין.
- ל-`balance-allowance`: `signature_type=1`; לפי asset עשוי להידרש גם token ID.
- כדי שה-reconciliation של האפליקציה יהיה אמיתי ולא mock: נדרש בעתיד לממש authenticated read-only REST adapter ולמפות בו במפורש signer address. זה שינוי קוד שטרם קיים ואינו נדרש לחיבור User WebSocket עצמו.

### חובה רק למסחר עתידי

- private key של ה-Signer לצורך L1/order payload signing.
- `funder=0xcE...ee38`, `signature_type=1`, `chain_id=137` באתחול SDK.
- Real adapter ממומש, balances/allowances מתאימים, ומנגנוני safety מפורשים.
- ביטול kill switch והפעלת flags למסחר — אסור במסגרת הנוכחית.

### לא קריטי כרגע

UI, התראות, Rules, order submission, cancel, approvals ופעולות on-chain אינם קשורים להוכחת חיבור ה-User WebSocket.

## 7. טבלת חוסרים לפי עדיפות

| רכיב | מצב | מה חסר | ערך נדרש | סודי/ציבורי | מי צריך לספק | פעולה נדרשת |
|---|---|---|---|---|---|---|
| L2 credential set | לא מאומת חי לאחר התיקון | הוכחת server acceptance | שלושת הערכים הקיימים כסט | סודי | בעל החשבון | לבצע GET read-only מסונן |
| `POLY_ADDRESS` לבדיקת REST | ידוע אך לא מוגדר בלקוח אפליקטיבי | להעביר כ-header בבדיקת GET | Signer `0x75D...F7D7` | ציבורי | בעל החשבון כבר סיפק | להשתמש רק בסקריפט GET; אין משתנה קוד קיים |
| User WS process | `AUTH_FAILED` מההרצה הישנה | טעינת Environment המתוקן | אין ערך חדש | — | מפעיל | רק אחרי GET מוצלח: restart ל-LIVE בלבד |
| 10-minute evidence | חסר | רצף CONNECTED ו-PONGs | 10 דקות, PING/PONG בערך כל 10 שניות | ציבורי/תפעולי | מפעיל | ניטור רציף לאחר restart |
| BTC 5m subscriptions | דורש אימות חי | הוכחה שה-IDs הם conditions פעילים | condition IDs של BTC Up or Down 5m | ציבורי | המערכת/מפעיל | להשוות DB/Gamma discovery בזמן הבדיקה |
| Profile metadata | חסר אך לא חוסם | `POLYMARKET_PROFILE_ADDRESS` | Proxy `0xcE07...ee38` | ציבורי | בעל החשבון | אופציונלי לחיבור; ניתן להוסיף בחלון שינוי עתידי |
| REST reconciliation אמיתי | לא ממומש | authenticated read-only adapter | מימוש קוד ומיפוי signer | מעורב | מפתח | משימה נפרדת; לא לבצע כעת |
| Trading identity | לא מחובר ובכוונה חסום | funder/type/key/chain | Proxy, `1`, key, `137` | מעורב | בעלים/מפתח | מסחר עתידי בלבד |

## 8. Runbook בטוח לבעלים

### שלב A — פתיחת Environment והשלמת metadata ציבורי בלבד

אין שורת Environment שחייבים להוסיף כדי שה-User WebSocket יעבוד: שלושת המשתנים היחידים שהוא קורא כבר SET. אם רוצים להשלים metadata שהקוד כן קורא, פתחו:

```bash
sudoedit /etc/polymarket-live/live.env
```

הוסיפו רק אם אינן קיימות:

```dotenv
POLYMARKET_PROFILE_ADDRESS=0xcE075637152167517e1492FcF5ff2D131686ee38
POLYMARKET_ACCOUNT_LOGIN_TYPE=email
```

שמרו וצאו. אין להוסיף `POLY_ADDRESS` מתוך הנחה שהקוד יקרא אותו — אין קריאה כזו. אין להוסיף private key.

לצורך תיעוד עתידי בלבד, ה-placeholders הקיימים ב-`.env.example` מתאימים לערכים הציבוריים הבאים, אך הקוד הנוכחי אינו קורא אותם ולכן הם אינם נדרשים לחיבור או ל-GET העצמאי:

```dotenv
POLYMARKET_SIGNATURE_TYPE=1
POLYMARKET_FUNDER_ADDRESS=0xcE075637152167517e1492FcF5ff2D131686ee38
```

אין המלצה להוסיף `POLYMARKET_EXPECTED_WALLET_ADDRESS`, משום שאין בקוד הגדרה לסמנטיקה שלו.

### שלב B — בדיקה מסוננת של SET/כפילויות בלי ערכים

```bash
sudo /opt/polymarket-btc-live/.venv/bin/python -c "import collections; p='/etc/polymarket-live/live.env'; rows=[x for x in open(p,encoding='utf-8').read().splitlines() if x.strip() and not x.lstrip().startswith('#') and '=' in x]; d=collections.Counter(x.split('=',1)[0].strip() for x in rows); names=('POLYMARKET_API_KEY','POLYMARKET_API_SECRET','POLYMARKET_API_PASSPHRASE','POLYMARKET_PROFILE_ADDRESS','POLYMARKET_ACCOUNT_LOGIN_TYPE'); [print(n, 'SET' if d[n]==1 else ('MISSING' if d[n]==0 else 'DUPLICATE')) for n in names]"
```

אם אחד משלושת פרטי L2 הוא `MISSING` או `DUPLICATE`, עצרו. אל תבצעו restart.

### שלב C — authenticated GET read-only

הפקודה הבאה מבצעת GET בלבד, אינה קוראת private key, אינה מדפיסה credentials או response bodies, ומשתמשת בכתובת ה-Signer הציבורית עבור `POLY_ADDRESS`:

```bash
sudo /opt/polymarket-btc-live/.venv/bin/python - <<'PY'
import base64, json, re, time
import httpx
from py_clob_client_v2.signing.hmac import build_hmac_signature

path = "/etc/polymarket-live/live.env"
env = {}
for raw in open(path, encoding="utf-8"):
    line = raw.rstrip("\n")
    if line and not line.lstrip().startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key.strip()] = value

secret = env.get("POLYMARKET_API_SECRET", "")
assert len(secret) == 44
assert len(base64.b64decode(secret, altchars=b"-_", validate=True)) == 32
address = "0x75D4148E7220b02545f822816901836679B0F7D7"
host = env.get("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com").rstrip("/")

def headers(request_path):
    ts = int(time.time())
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": build_hmac_signature(secret, ts, "GET", request_path),
        "POLY_TIMESTAMP": str(ts),
        "POLY_API_KEY": env["POLYMARKET_API_KEY"],
        "POLY_PASSPHRASE": env["POLYMARKET_API_PASSPHRASE"],
    }

checks = (
    ("open_orders", "/data/orders", {"next_cursor": "MA=="}),
    ("trades", "/data/trades", {"next_cursor": "MA=="}),
    ("balance_allowance", "/balance-allowance", {"asset_type": "COLLATERAL", "signature_type": "1"}),
)
with httpx.Client(timeout=20, follow_redirects=False) as client:
    for name, request_path, params in checks:
        response = client.get(host + request_path, headers=headers(request_path), params=params)
        print(name, response.status_code, "OK" if 200 <= response.status_code < 300 else "FAILED")
        if not 200 <= response.status_code < 300:
            message = re.sub(r"[A-Za-z0-9_+/=-]{20,}", "[REDACTED]", response.text[:300])
            print("filtered_error", message)
            raise SystemExit(1)
PY
```

נקודת עצירה: אם GET כלשהו אינו 2xx, עצרו. אל תבצעו restart. שמרו רק status ושגיאה מסוננת.

### שלב D — רק לאחר שכל GET הצליח

רשמו baseline של DEMO:

```bash
systemctl show polymarket.service -p MainPID -p ActiveEnterTimestamp --no-pager
```

אתחלו LIVE בלבד:

```bash
sudo systemctl restart polymarket-live.service
```

ודאו ששירות LIVE פעיל וש-DEMO לא אותחל:

```bash
systemctl is-active polymarket-live.service
systemctl show polymarket-live.service -p MainPID -p ActiveEnterTimestamp --no-pager
systemctl show polymarket.service -p MainPID -p ActiveEnterTimestamp --no-pager
```

בדקו logs מסוננים בלבד:

```bash
sudo journalctl -u polymarket-live.service --since "2 minutes ago" --no-pager | sed -E 's/[A-Za-z0-9_+\/=.-]{24,}/[REDACTED]/g' | rg 'User WebSocket|user_ws|CONNECTED|AUTH_FAILED|PING|PONG|STALE|ERROR'
```

בדקו health ציבורי:

```bash
curl --fail --silent http://127.0.0.1:8001/health
```

בדקו את state המסונן ואת condition IDs בלי credentials:

```bash
/opt/polymarket-btc-live/.venv/bin/python - <<'PY'
import json, sqlite3
db = sqlite3.connect("file:/opt/polymarket-btc-live/poly_live.sqlite3?mode=ro", uri=True)
for key in ("user_ws_status", "user_ws_health"):
    row = db.execute("SELECT value FROM live_system_state WHERE key=?", (key,)).fetchone()
    print(key, row[0] if row else "MISSING")
rows = db.execute("SELECT condition_id,event_id FROM live_markets WHERE market_resolved=0 ORDER BY id DESC LIMIT 2").fetchall()
print("subscriptions", json.dumps(rows))
PY
```

בדיקת יציבות רציפה של 10 דקות, בדגימה כל 10 שניות:

```bash
/opt/polymarket-btc-live/.venv/bin/python - <<'PY'
import datetime, json, sqlite3, time
deadline = time.monotonic() + 600
last_pong = None
last_pong_change = time.monotonic()
while time.monotonic() < deadline:
    db = sqlite3.connect("file:/opt/polymarket-btc-live/poly_live.sqlite3?mode=ro", uri=True)
    row = db.execute("SELECT value FROM live_system_state WHERE key='user_ws_health'").fetchone()
    db.close()
    health = json.loads(row[0]) if row else {}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(now, health.get("status"), "ping", health.get("last_ping_at"), "pong", health.get("last_pong_at"), "stale", health.get("stale"), "markets", health.get("subscribed_condition_ids"))
    if health.get("status") != "CONNECTED" or health.get("stale"):
        raise SystemExit("FAILED: User WS not continuously healthy")
    pong = health.get("last_pong_at")
    if pong and pong != last_pong:
        last_pong, last_pong_change = pong, time.monotonic()
    if not pong or time.monotonic() - last_pong_change > 25:
        raise SystemExit("FAILED: no advancing PONG within 25 seconds")
    time.sleep(10)
print("PASS: 10 continuous minutes")
PY
```

לאחר הבדיקה:

```bash
curl --fail --silent http://127.0.0.1:8001/health
systemctl show polymarket.service -p MainPID -p ActiveEnterTimestamp --no-pager
```

יש להשוות את PID/`ActiveEnterTimestamp` של `polymarket.service` ל-baseline; שניהם חייבים להישאר זהים.

## 9. נקודות עצירה

עצירה מיידית ללא restart אם:

- אחד משלושת credentials חסר או כפול.
- בדיקת Base64 נכשלת.
- כל authenticated GET מחזיר non-2xx או אינו מגיע לשרת.
- מתקבלת שגיאת auth מסוננת.

לאחר restart המאושר, הבדיקה נכשלת ויש לעצור אבחון ללא שינוי נוסף אם:

- ה-state הוא `AUTH_FAILED`, `ERROR`, `STALE` או `DISABLED`.
- אין PONG מתקדם בכל חלון דגימה.
- החיבור מתנתק/מתחבר מחדש במהלך 10 הדקות.
- subscriptions אינם condition IDs של BTC Up or Down 5m פעילים.
- DEMO קיבל PID או זמן הפעלה חדש.
- `/health` נשאר `degraded`; יש לבדוק אם קיימת תקלה אחרת לפני ייחוס ל-WebSocket.

## 10. תנאי הצלחה מדויקים

אין להגדיר הצלחה לפני שכל התנאים מתקיימים:

1. open orders, trades ו-balance/allowance GET מחזירים 2xx ב-read-only test.
2. רק `polymarket-live.service` אותחל.
3. User WebSocket מגיע ל-`CONNECTED` ואינו עובר דרך מצב כשל במהלך 10 דקות.
4. `last_pong_at` מתקדם ברציפות בעקבות PINGs של 10 שניות; אין stale או reconnect.
5. `subscribed_condition_ids` מכילים condition IDs, לא token IDs, של BTC Up or Down 5m הפעילים.
6. PID וזמן ההפעלה של `polymarket.service` (DEMO) לא השתנו.
7. `/health` מחזיר `{"status":"ok"}` אם אין תקלה אחרת.

## 11. הצהרת בטיחות של ה-Audit

- לא שונה קוד או Environment.
- לא בוצע restart לשום שירות.
- לא נשלחה בקשת authenticated GET במסגרת ה-Audit.
- לא בוצעו POST, PUT, PATCH, DELETE, heartbeat של Orders, order או cancel.
- לא בוצעה פעולה on-chain או approve.
- לא נקרא private key.
- לא הודפסו או נרשמו API key, secret או passphrase.
- בוצעו רק קריאות מקומיות מסוננות, בדיקת תיעוד רשמי וכתיבת דוח זה.
- לא נוצר commit ולא בוצע push.
