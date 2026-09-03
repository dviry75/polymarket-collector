# דוח מימוש מלא ומוכנות Canary — Polymarket LIVE

תאריך: 2026-08-05 UTC
שרת: `polymarket-live-fi`, אזור `europe-north1-a`, פינלנד
Branch: `codex/live-full-implementation-20260805`
מצב סופי: **NOT READY**

## תקציר מנהלים

מומשו Real Adapter ל־SDK הרשמי המאוחד, state machine עמידה לאסטרטגיית BTC Up/Down 5m, Order Book מלא בזיכרון, reconciliation, recovery, UI תפעולי, timeline/alerts, ארכוב מאומת בבדיקות ופריסת Paper בטוחה. השירות פעיל ובריא, Market WS ו־User WS מחוברים, אך הכניסות חסומות: `Pause Entries=true`, ‏Kill Switch פעיל, Canary כבוי, ושליחת Orders אמיתיים כבויה.

אין מוכנות ל־Canary בגלל Stop A: ל־VM חסר OAuth scope ל־Secret Manager; לכן לא ניתן היה לבצע read-only validation של signer/wallet/balance/allowances/open orders/positions/closed-only. בנוסף bucket וטיימר הארכוב עדיין אינם פעילים בייצור, ונתיב redemption אוטומטי אמיתי נשאר מושבת ולא אומת. לא בוצעו Order, Cancel אמיתי, Approval, Redemption או טרנזקציה on-chain.

## נקודת המוצא ומה השתנה

לפני השינוי היו שירות LIVE, login, DB ו־Market/User WebSocket בסיסיים, אך הקוד השתמש ב־SDK ישן, לא הייתה state machine עמידה מלאה, reconciliation לא כיסה את כל מקור האמת הכספי, Order Book deltas סיכנו מחיקת עומק, ולא היו archive מאומת, UI תפעולי מלא או חסמי Canary רב־שכבתיים. Raw WS ו־snapshots בתדירות גבוהה הובילו לקצב של כ־1.8GB/day.

בפועל נוספו או שונו:

- מעבר בלעדי ל־`polymarket-client==0.1.0b21`; הוסרו `py_clob_client_v2`, ‏`py_order_utils` ו־`poly_eip712_structs`.
- Real Adapter עם create/sign ואז post, preflight allowance מפורש וללא auto-approval; BUY/SELL FAK, ‏SELL GTC, ביטול ממוקד בלבד, heartbeat, reads ו־redeem מאושר.
- חוק מדויק: trigger ב־0.74, תקרת 0.76, ‏TP 0.96, ‏Stop 0.66 עד 0.55, ‏Emergency 0.60 עד 0.01, חלון 120 שניות ו־$5 All-In כולל עמלות.
- נעילת Event durable, ניסיון כניסה יחיד גם ב־zero fill, caps של עסקה/חשיפה/פקודות, idempotency ו־mutex durable נגד TP/exit כפול.
- טיפול ב־partial/zero/delayed/pending/rejected/unknown/timeout ללא retry עיוור; reconciliation קובע את התוצאה.
- Order Book עם snapshot מחליף, delta נקודתי, מחיקת level רק ב־size=0, מיון, duplicate/out-of-order/gap/reconnect fail-closed ושני token books.
- reconciliation ב־startup, reconnect, אחרי פעולה ובמחזוריות; remote account הוא מקור האמת, וכל פער מפעיל Pause Entries.
- טבלאות additive ל־event states, intents, fills, positions, deals, timeline, alerts ו־archive runs; ערכי כסף/מחיר חדשים נשמרים כ־Decimal קנוני ב־TEXT.
- Overview, Strategy, Orders/Positions, Logs ו־Ops; Pause/Resume, Emergency preview/confirm, CSRF, re-auth, filters/export, timeline מקושר ו־Alerts persistent.
- Raw Market WS כבוי ב־LIVE. snapshots מלאים נשמרים רק בשינוי ובייצור לכל היותר פעם ב־10 שניות לכל token; כל frame עדיין מעובד בזיכרון.
- archive יומי gzip NDJSON עם SHA-256, manifest, upload אטומי, read-back לפני מחיקה ודרישת lifecycle של 365 יום; מימוש production מוכן אך טרם הופעל מול bucket אמיתי.
- SQLite עבר ל־WAL, ‏`busy_timeout=30s` ו־`synchronous=NORMAL`; shutdown/startup האחרון הושלם נקי.

## Commits וקבצים מרכזיים

- `3266d4f` — `feat(live): add unified SDK strategy state machine`
- `ec11777` — `feat(live): add operations UI archive and safety tests`
- `5f649e9` — `fix(live): harden persistence and deployment recovery`

קבצים מרכזיים: `live/adapters/polymarket.py`, ‏`live/config.py`, ‏`live/order_book.py`, ‏`live/strategy.py`, ‏`live/strategy_repository.py`, ‏`live/strategy_runtime.py`, ‏`live/reconciliation.py`, ‏`live/market_discovery.py`, ‏`live/market_websocket.py`, ‏`live/archive.py`, ‏`live/router.py`, ‏`live_app.py`, ‏`deploy/*`, ‏`requirements*.txt` ו־`tests/test_live_*`.

## SDK ותיעוד רשמי

גרסאות מותקנות ונעולות:

- `polymarket-client==0.1.0b21`
- `google-cloud-storage==3.13.0`
- `websockets==15.0.1`
- `fastapi==0.141.1`
- `uvicorn==0.52.1`

`pip check`: `No broken requirements found`.

ההחלטות נבדקו מול [מדריך migration הרשמי](https://docs.polymarket.com/getting-started/migrate-from-previous-sdks), [Place Orders](https://docs.polymarket.com/trading/place-orders), [Wallets & Auth](https://docs.polymarket.com/trading/wallets-auth), [Manage Orders](https://docs.polymarket.com/trading/manage-orders), [Real-time Order Updates](https://docs.polymarket.com/trading/real-time-order-updates), [Manage Positions](https://docs.polymarket.com/trading/positions/manage), [Heartbeat](https://docs.polymarket.com/api-reference/trade/send-heartbeat), [Geoblock](https://docs.polymarket.com/api-reference/geoblock) ו־[PyPI](https://pypi.org/project/polymarket-client/).

ה־SDK מגדיר HTTP timeout של connect=5s, read/write=10s ו־pool=2s. ה־adapter אינו מבצע retry לאחר post לא ודאי; הוא מחזיר `unknown` ועובר ל־reconciliation.

## Migrations, גיבוי ו־restore

- גיבוי config לפני השינוי: `/opt/polymarket-btc-live/backups/live.env.pre_full_20260805_205748`, הרשאות `0600`, בעלות root.
- גיבוי DB: `/opt/polymarket-btc-live/backups/poly_live_20260805_205824.sqlite3.gz`, גודל 29,438,173 bytes.
- SHA-256: `20f6c4eb209c31443400e658fa4ae1164d631f4258298623f20e01e29eda9f54`.
- רשומת DB: status=`ok`, reason=`pre_full_live_migration`.
- restore למדגם נפרד: `integrity_check=ok`, ‏17 טבלאות ו־108,371 snapshots לפני migration.
- migration הורץ פעמיים על העותק: idempotent, הנתונים נשמרו, הטבלאות החדשות נוצרו ו־Pause נשאר פעיל.
- DB בייצור לאחר הפריסה: `journal_mode=wal`, ‏`integrity_check=ok`.

## בדיקות

פקודת הסיום:

```text
/opt/polymarket-btc-live/.venv/bin/python -m pytest -q
101 passed, 7 warnings, 9 subtests passed in 55.02s
```

האזהרות הן deprecation של FastAPI/Starlette בלבד. בנוסף:

- `python -m compileall -q live live_app.py ...` — עבר.
- `pip check` — עבר.
- `git diff --check` — עבר.
- סריקת diff ל־secret assignments — 0 ממצאים.
- אין import או שימוש ב־SDK הישן.

הכיסוי כולל את כל הטריגרים והגבולות, simultaneous trigger, All-In ועמלות, zero/partial/full fill, delayed/pending/rejected/timeout/unknown, crash/restart/idempotency, TP/Stop/Emergency ללא oversell, dust/resolution, reconciliation/adoption, snapshot/delta/delete/out-of-order/duplicate/reconnect, Market/User WS, archive checksum/read-back/no-delete-on-failure, Login/Admin/CSRF/re-auth/masking, Pause/Resume, Emergency ו־UI logs/alerts.

## Paper soak ו־Market WS אמיתי

השירות רץ מול Market WS ו־User WS אמיתיים במצב `PAPER_TRADING` ו־Paused במשך יותר מ־20 דקות, חצה 5 Events שונים של 5 דקות ועבר כמה restart יזומים. בסיום שני ה־WS היו `CONNECTED`; לא נוצרו Orders, intents, deals או positions.

ה־fixture soak הדטרמיניסטי הריץ 12 Events וכל מסלולי ה־trigger שלא בהכרח הופיעו בשוק:

```text
events=12
entry_intents=9
simultaneous_skips=3
frames_processed=22
duplicate_entry_groups=0
parallel_exit_groups=0
active_positions=0
legacy_orders=0
integrity_check=ok
```

## קצב גידול DB ו־retention

לפני התיקון: 228MB ב־20:26 לעומת 266MB ב־20:56 — כ־1.8GB/day. Raw WS הגיע ל־58,104 רשומות.

לאחר הפריסה:

- `live_websocket_events` נשאר 58,104 ולא גדל: Raw Market WS persistence כבוי.
- לאחר throttle של 10 שניות: 44 snapshots בחלון 5 דקות, payload ממוצע 4,035 bytes.
- מדד האפליקציה לאחר חלון הייצור: `db_growth_projected_mb_day=319.246`.
- תחזית payload steady-state היא כ־52MB/day לפני overhead; גם התחזית השמרנית של קובץ ה־DB נמוכה משמעותית מ־1GB/day.
- retention מקומי 30 יום וארכוב 365 יום נאכפים בקוד ובבדיקות.

הקבלה “לא GB/day” הושגה במדידה. הקבלה “archive אמיתי ל־GCS מאומת” עדיין לא הושגה משום שאין bucket/IAM וטיימר פעיל; לכן המצב נשאר NOT READY.

## UI וראיות

- `GET https://live-poly.dvirtechnologies.com/live/login` — 200.
- session חתום שנוצר בזיכרון בלבד: Overview — 200, כל markers נמצאו, 22,053 bytes.
- Logs — 200, markers של Logs/Persistent Alerts/Export נמצאו, 50,618 bytes.
- Alerts API — 200 ורשימה תקינה.
- בדיקת password login חיובית מכוסה ב־TestClient; לא נעשה ניסיון לחשוף או לשחזר סיסמת production.
- לא היה דפדפן headless מותקן בשרת, ולכן לא נוצרו screenshots. בוצע smoke מבני מלא של HTML/API בדומיין הציבורי.

## Deployment ו־runtime

- `polymarket-live.service`: `active/running`, `NRestarts=0` לאחר האתחול האחרון.
- shutdown: `Application shutdown complete`; startup: `Application startup complete`.
- `GET /health`: `{"status":"ok"}`.
- HTTPS, HSTS, `X-Frame-Options: DENY`, ‏`nosniff` ו־Permissions-Policy פעילים.
- geographic preflight רשמי: `ALLOWED`, country=`FI`, region=`02`.
- Market WS=`CONNECTED`; User WS=`CONNECTED`.
- Market metadata דינמי אומת: token mapping, condition, tick (0.001/0.01 לפי market), min order=5, fee=0.07 ו־accepting orders.
- `Pause Entries=true`, ‏Kill Switch=`true`, ‏Canary armed=`false`, ‏Canary consumed=`false`.
- strategy intents=0, active positions=0, open legacy orders=0, deals=0.
- reconciliation=`NOT_READY`, block reason=`RECONCILIATION_FAILED`, בגלל Secret Manager PermissionDenied.
- יחידת archive/timer קיימת במאגר, אך ההתקנה ל־systemd לא בוצעה: הפעלת job מתוזמן שמעלה ואז מוחק snapshots מקומיים לאחר read-back דורשת אישור תפעולי מפורש.

## Stop A — Secrets, wallet, allowances וארכוב

Project: `lyrical-carver-490321-t6`
VM: `polymarket-live-fi`
Zone: `europe-north1-a`
Service Account: `590957160427-compute@developer.gserviceaccount.com`

ה־VM מחזיק scopes של Storage read-only, logging, monitoring, service management/control ו־trace בלבד. אין `cloud-platform`; קריאת Secret Manager מחזירה `ACCESS_TOKEN_SCOPE_INSUFFICIENT`/`PermissionDenied`.

מצב presence בלבד, ללא ערכים:

- profile/funder/signer addresses מוגדרים; signature type=`1` (`POLY_PROXY`).
- private key ו־operator token אינם בקובץ env.
- API key/secret/passphrase, login hash ו־session secret עדיין קיימים ב־env הישן; יש להעבירם ל־Secret Manager ולהסירם רק לאחר read-back מוצלח.
- כל שבעת Secret Manager resources טרם ניתנים לקריאה מה־VM.

שמות הסודות המדויקים:

```text
polymarket-live-POLYMARKET_PRIVATE_KEY
polymarket-live-POLYMARKET_API_KEY
polymarket-live-POLYMARKET_API_SECRET
polymarket-live-POLYMARKET_API_PASSPHRASE
polymarket-live-LIVE_LOGIN_PASSWORD_HASH
polymarket-live-LIVE_SESSION_SECRET
polymarket-live-LIVE_OPERATOR_TOKEN
```

הפעולות הנדרשות, ממסוף Admin מאובטח ולא בצ׳אט:

1. לעצור את ה־VM, להגדיר את אותו Service Account עם scope `cloud-platform`, ולהפעיל מחדש:

```bash
gcloud compute instances stop polymarket-live-fi --zone=europe-north1-a --project=lyrical-carver-490321-t6
gcloud compute instances set-service-account polymarket-live-fi --zone=europe-north1-a --project=lyrical-carver-490321-t6 --service-account=590957160427-compute@developer.gserviceaccount.com --scopes=cloud-platform
gcloud compute instances start polymarket-live-fi --zone=europe-north1-a --project=lyrical-carver-490321-t6
```

2. ליצור כל secret, להוסיף את הערך דרך stdin מקומי (`read -s`; לא argument ולא צ׳אט), ולהעניק ל־Service Account `roles/secretmanager.secretAccessor` על כל secret בנפרד:

```bash
PROJECT_ID=lyrical-carver-490321-t6
SERVICE_ACCOUNT=590957160427-compute@developer.gserviceaccount.com
for SECRET_ID in \
  polymarket-live-POLYMARKET_PRIVATE_KEY \
  polymarket-live-POLYMARKET_API_KEY \
  polymarket-live-POLYMARKET_API_SECRET \
  polymarket-live-POLYMARKET_API_PASSPHRASE \
  polymarket-live-LIVE_LOGIN_PASSWORD_HASH \
  polymarket-live-LIVE_SESSION_SECRET \
  polymarket-live-LIVE_OPERATOR_TOKEN
do
  gcloud secrets describe "$SECRET_ID" --project="$PROJECT_ID" >/dev/null 2>&1 || \
    gcloud secrets create "$SECRET_ID" --project="$PROJECT_ID" --replication-policy=automatic
  gcloud secrets add-iam-policy-binding "$SECRET_ID" --project="$PROJECT_ID" \
    --member="serviceAccount:$SERVICE_ACCOUNT" \
    --role=roles/secretmanager.secretAccessor
done

add_secret_version() {
  SECRET_ID="$1"
  read -rsp "Value for $SECRET_ID: " SECRET_VALUE
  printf '\n'
  printf '%s' "$SECRET_VALUE" | gcloud secrets versions add "$SECRET_ID" \
    --project="$PROJECT_ID" --data-file=-
  unset SECRET_VALUE
}
# להפעיל ידנית פעם אחת לכל אחד משבעת השמות; אין להדביק ערכים לצ׳אט.
```

3. ליצור bucket ייעודי, למשל `gs://lyrical-carver-490321-t6-polymarket-live-archive`, באזור `europe-north1`, עם Uniform Access, Public Access Prevention, הצפנת Google-managed או CMEK מאושר, lifecycle delete בגיל 365 יום, ו־IAM מינימלי:

```bash
BUCKET=gs://lyrical-carver-490321-t6-polymarket-live-archive
gcloud storage buckets create "$BUCKET" --project="$PROJECT_ID" \
  --location=europe-north1 --uniform-bucket-level-access \
  --public-access-prevention

# lifecycle.json
# {"rule":[{"action":{"type":"Delete"},"condition":{"age":365}}]}
gcloud storage buckets update "$BUCKET" --lifecycle-file=lifecycle.json
gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member="serviceAccount:$SERVICE_ACCOUNT" --role=roles/storage.objectAdmin
gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member="serviceAccount:$SERVICE_ACCOUNT" --role=roles/storage.legacyBucketReader
```

ברירת המחדל של GCS מצפינה במנוחה במפתחות Google-managed. אם נדרש CMEK, יש ליצור key ייעודי, לתת ל־GCS Service Agent הרשאת encrypt/decrypt ולהוסיף `--default-encryption-key` בעת יצירת ה־bucket.

4. להגדיר `LIVE_ARCHIVE_GCS_BUCKET=lyrical-carver-490321-t6-polymarket-live-archive`, לבצע integration על עותק DB עם old snapshots, לאמת generation/checksum/manifest/read-back/no-delete-on-failure, ורק אז, באישור תפעולי מפורש, להתקין ולהפעיל את הטיימר:

```bash
cd /opt/polymarket-btc-live/repo/polymarket-collector
sudo install -o root -g root -m 0644 deploy/polymarket-live-archive.service /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/polymarket-live-archive.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket-live-archive.timer
systemctl list-timers polymarket-live-archive.timer --all --no-pager
```

5. לאחר תיקון ה־scope/IAM: לבצע read-only preflight מלא — signer derivation/match, wallet address/type, closed-only, pUSD balance, allowances, open orders, trades, positions וחשיפה לא פתורה. אם allowance חסר, להציג preview של החוזים והסכומים ולעצור לאישור נפרד.

כרגע הנתונים הבאים **לא נבדקו**: signer match, wallet read-back מה־SDK, closed-only, balance, allowances, open orders/trades/positions וחשיפה מרוחקת. אין להסתמך על ערכי config בלבד.

## Redemption ו־Canary

ה־adapter כולל `redeem_positions` מאושר בלבד, וה־state machine מסמנת winner כ־`REDEEM_PENDING`; ב־Paper resolution נסגרת אוטומטית. ב־LIVE אין כרגע trigger אוטומטי שמבצע redemption אמיתי. הנתיב נשאר מושבת ולא נבדק on-chain. זהו חסם נוסף ל־READY FOR CANARY.

Canary לא אושר ולא בוצע. לכן אין trigger/order IDs/fills/fees/P&L אמיתיים לדווח. המגבלות בקוד הן Event יחיד, ניסיון כניסה יחיד, עד $5 All-In, consume durable לפני network, וחזרה ל־Pause; אך הן לא יופעלו עד Stop A, archive validation, redemption readiness ואישור Canary מפורש חדש.

## רשימת פערים גלויה

1. Secret Manager scope/IAM חסומים; secrets לא נטענים מהמקור המיועד.
2. חמישה secrets קיימים עדיין ב־env הישן ומחייבים migration מבוקר.
3. signer/wallet/balance/allowance/closed-only/account truth לא אומתו.
4. GCS bucket, lifecycle, IAM, upload/read-back אמיתי וטיימר production לא הופעלו.
5. automatic LIVE redemption אינו מחובר ואינו מאומת.
6. login production עם password בפועל לא נוסה; session מאומת ובדיקת password אוטומטית כן עברו.
7. לא נוצרו screenshots בשל היעדר דפדפן headless; HTML/API עברו smoke.
8. `gh` אינו מותקן, ולכן לפי workflow הפרסום לא בוצעו push או Draft PR.
9. Canary ומסחר רציף לא בוצעו, כמתחייב מנקודות העצירה.

## מסקנה

**NOT READY** — הקוד והפריסה הבטוחה מוכנים להמשך Stop A, אך אין הרשאה או ראיות מספיקות ל־Approval או Canary. השירות נשאר פעיל לצפייה ואיסוף נתוני שוק בלבד, ב־Paper+Paused, ללא פקודות אמיתיות.
