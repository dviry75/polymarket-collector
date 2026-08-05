# Polymarket LIVE — מוכנות למסחר אמיתי, שאלות פתוחות ונוהל תפעול

תאריך בדיקה: 5 באוגוסט 2026 (UTC)
תחום: מערכת ה־LIVE בלבד — `/opt/polymarket-btc-live`, השירות `polymarket-live.service`, מסד `poly_live.sqlite3` והדומיין `live-poly.dvirtechnologies.com`.

## החלטה נוכחית: NO-GO למסחר אמיתי

אין כרגע פעולה בטוחה מסוג “Start” שיכולה להכניס את המערכת לעסקאות אמיתיות. אין להסתפק בשינוי flags או בכיבוי ה־Kill Switch: ה־build הפעיל חוסם מסחר אמיתי במכוון, סביבת השרת חסומה גאוגרפית למסחר, וכמה שערי בטיחות ותפעול עדיין אינם מחוברים או אינם עוברים.

המערכת כן פעילה ומתאימה כרגע ל־Paper Trading ולקריאות Polymarket מסוג read-only.

## תמונת מצב שנמדדה

| רכיב | מצב ב־5.8.2026 | משמעות |
|---|---|---|
| `polymarket-live.service` | `active (running)`, enabled, ללא restart מאז 3.8 | תהליך ה־LIVE יציב ברמת systemd |
| Public health | HTTP 200, `status=ok` | השירות וניהול האחסון עונים |
| מצב ביצוע | `TRADING_MODE=DEMO`, `LIVE_EXECUTION_MODE=PAPER_TRADING` | לא מצב מסחר אמיתי |
| Adapter | `polymarket` | GET מאומת בלבד; write חסום בקוד |
| Real trading flags | שניהם `false` | שליחת פקודות אינה armed |
| Kill Switch | `true` | כל Order אמיתי נחסם |
| Operator token | לא מוגדר | פעולות מפעיל מוגנות חסומות |
| Private key | לא נמצא בתהליך | אין signer ל־EIP-712 order payload |
| L2 API credentials | קיימים בתהליך; הערכים לא נחשפו | User WS ו־GET read-only יכולים לעבוד |
| User WebSocket | `CONNECTED`, PING/PONG עדכניים בזמן הסריקה | ערוץ account read-only עובד |
| Market WebSocket | מזרים snapshots; state נצפה גם כ־`DISCONNECTED` בזמן rollover | דורש SLO וניטור רציף, לא snapshot נקודתי |
| Rules | חוק Paper אחד, `inactive` | אין חוק REAL פעיל או endpoint ליצירתו |
| Deals / Orders / Fills / Positions | 0 / 0 / 0 / 0 | לא בוצע מסחר אמיתי |
| Reconciliation | run אחרון `ok`, 0 gaps, אך ישן בהרבה מסף 30 שניות | שער הסיכון היה חוסם Order |
| Account identity | אין snapshot ואין state מאומת | שער הסיכון היה חוסם Order |
| DB | כ־291MB; `160,000` snapshots בזמן הסריקה | retention עובד, אך נדרש capacity/SLO |
| Backups | אין רשומות ב־`live_backups`; קיימים עותקי deployment ידניים | אין הוכחת גיבוי אוטומטי ושחזור שוטף |
| Disk | כ־59% בשימוש, כ־7.6GB פנויים | תקין כרגע |
| Geoblock מהשרת | `blocked=true`, ארה״ב, Iowa | Polymarket צפוי לדחות Orders מהשרת הנוכחי |

לא הודפסו או נשמרו במסמך ערכי password, session secret, API key/secret/passphrase, כתובות פרטיות או private key.

## חסמי חובה לפני מסחר אמיתי

### P0 — אין מימוש אמיתי של Place/Cancel

- `LiveConfig.validation_errors()` מקבל רק `READ_ONLY` או `PAPER_TRADING`; `REAL_TRADING` נדחה במפורש.
- `RealPolymarketTradingAdapter.create_order()` מחזיר `REAL_POLYMARKET_ORDER_SUBMISSION_DISABLED_IN_THIS_BUILD`.
- כל פעולות `cancel_order`, `cancel_orders` ו־`cancel_all_orders` מחזירות `blocked`.
- ה־UI/API מאפשר יצירת חוקי `PAPER_TRADING` בלבד. אין endpoint ליצירת/הפעלת חוק REAL.
- ה־Trading Engine אינו מחובר לזרם השוק כמנוע REAL אוטומטי; רק Paper Engine מקבל snapshots.
- אין endpoint תפעולי לביטול Order אמיתי, manual exit או flatten-all.

מסקנה: שינוי environment בלבד אינו מפעיל מסחר; הוא עלול ליצור מצב ביניים מבלבל ולא בטוח.

### P0 — השרת הנוכחי חסום גאוגרפית

קריאת read-only מכתובת השרת אל `https://polymarket.com/api/geoblock` החזירה:

```json
{"blocked": true, "country": "US", "region": "IA"}
```

Polymarket דורשת בדיקת geoblock לפני Order ומציינת שפקודות מאזור חסום יידחו. יש לבחור תשתית באזור שמותר חוקית ותפעולית, לאחר אישור compliance. אין לעקוף geoblock באמצעות VPN או מנגנון הסוואה.

### P0 — signing, זהות ו־allowance אינם מוכנים

- אין private key בתהליך ואין client שמייצר וחותם EIP-712 Orders.
- יש להחליט ולאמת את סוג החשבון, `signature_type`, כתובת signer ו־funder לפי מודל החשבון העדכני.
- יש להוכיח שה־API credentials נגזרו עבור אותו signer ושיתרת ה־funder היא היתרה שאליה מתכוונים.
- אין flow ממומש ומאומת ל־allowance update/approval, ואין החלטה מי מורשה לבצע פעולה on-chain.
- יש לקבוע היכן נשמר private key. קובץ environment רגיל אינו ברירת מחדל מאושרת למסחר אמיתי.
- לפני מסחר יש לבצע geoblock, balance, allowance ו־account identity checks מאותה מכונה שתשלח Orders.

### P0 — שערי בטיחות נכשלים גם לפני ה־adapter

- `LIVE_OPERATOR_TOKEN` אינו מוגדר, לכן פעולות מפעיל מוגנות חסומות.
- Kill Switch פעיל.
- אין `account_identity_status=VERIFIED` ואין account snapshot.
- Risk Manager בודק `user_ws_last_message_at`, אך User WS שומר כרגע `user_ws_health` ו־`user_ws_status`; המפתח הנבדק לא קיים במסד.
- `LIVE_RECONCILIATION_INTERVAL_SECONDS=15` קיים בקונפיגורציה, אך אין loop מתוזמן. reconciliation רץ ידנית או בעת reconnect של User WS בלבד. הסף הוא 30 שניות, ולכן הוא stale רוב הזמן.
- שווקים פעילים נשמרים עם `token_mapping_status=verified`, בעוד Risk Manager מאשר רק `matched` או `unknown`. במצב הנוכחי mapping תקין לכאורה עלול להיחסם.
- בדיקת minimum order משווה `requested_amount_usd` מול `min_order_size`, שעלול להיות מספר shares. יש לאמת ולתקן יחידות לפני כסף אמיתי.
- אין health gate יחיד ואטומי שמוכיח שכל התנאים נשארו ירוקים ברגע שליחת Order.

### P0 — ניהול יציאה והתאוששות אינו שלם

כניסה אמיתית אסורה כל עוד אין הוכחה מלאה עבור:

- FOK entry: timeout, rejected, unmatched ותגובה לא חד־משמעית.
- partial fills, גם אם policy ראשונית אומרת “ללא partial”; המציאות יכולה להגיע באירועי WS/REST לא מסודרים.
- SL/TP על Best Bid עם worst-price limit ו־slippage ביחידות נכונות.
- retry bounded ל־Stop Loss בלי ליצור מכירה כפולה.
- cancel/replace, cancel-all ו־manual flatten.
- process crash או network partition בין POST לכתיבה למסד.
- restart עם Order/Position פתוחים וגילוי orphan remote/local.
- market resolution, redemption/manual claim ותיעוד PnL/fees.
- idempotency עמידה גם בין processes/restarts, לא רק lock בזיכרון.

### P1 — ניטור והתראות אינם Production-ready

- `alerts.py` מממש `NoopAlertProvider` שכותב audit למסד בלבד; אין Pager/Slack/Email/SMS.
- אין watchdog חיצוני שמתריע על service down, WS stale, reconciliation stale/gap, Order failed, Position orphan, disk, backup failure או Kill Switch activation.
- ה־User WS יכול להיות `CONNECTED` עם 0 trade/order events; נדרש מבחן synthetic/readiness ולא רק status.
- Public `/health` חושף כרגע נתוני disk/retention בניגוד לחוזה המתועד `{"status":"ok"}` בלבד, וזה גם גורם לכשל בדיקה בהתקנה הפעילה.
- לא הוגדרו SLOs: freshness, latency מ־signal עד submit, זמן acknowledgment, זמן exit, זמינות ו־RTO/RPO.

### P1 — source of truth ופריסה אינם נקיים

- קוד השרת ב־`/opt/polymarket-btc-live` שונה מהריפו במספר קבצים מהותיים, כולל config, router, websockets, repository, paper engine ו־`live_app.py`.
- `retention.py` וסקריפט health נמצאים בשרת אך לא ב־GitHub.
- ה־requirements אינם pinned; build חוזר עלול לקבל גרסאות שונות.
- הרצה על הריפו: `81 passed`, `9 subtests passed`.
- הרצה על ההתקנה הפעילה, `tests/` בלבד: `68 passed, 1 failed`. הכשל הוא חוזה public health שלא עודכן לאחר הוספת storage health.
- `pytest` משורש ההתקנה נכשל כבר ב־collection כי `/opt/.../output` אינו קריא למשתמש השירות. יש לקבוע test command קנוני (`pytest tests`) או pytest config שמחריג runtime directories.

אין להוסיף מסחר אמיתי לפני שהקוד שרץ ניתן לשחזור מ־commit/tag חתום או מזוהה, עם artifact/version גלוי ב־UI וב־health הפרטי.

### P1 — גיבוי, שחזור ושמירת מידע

- מנגנון retention פעיל ובריא, אבל הוא אינו גיבוי.
- אין רשומת backup שנוצרה דרך מנהל הגיבויים של המערכת.
- קיימים עותקי deployment ידניים, אך לא הוצגה בדיקת restore תקופתית.
- לפני מסחר יש להגדיר RPO/RTO, גיבוי אוטומטי מוצפן מחוץ לשרת, checksum, retention, restore drill והגנה על audit עסקי מפני מחיקה.

## שאלות פתוחות לבעלים

יש לענות על כולן במסמך החלטות לפני פיתוח/הפעלה של REAL.

### חשבון, חוקיות ותשתית

- [ ] באיזו מדינה/region מותר לנו להפעיל את שירות המסחר, ומהו אישור ה־compliance המתועד?
- [ ] האם החשבון הקיים נשאר Email/Magic Proxy (`signature_type=1`) או עוברים למודל deposit wallet העדכני (`signature_type=3`)?
- [ ] מהן כתובות ה־signer וה־funder הקנוניות, ומי מאשר התאמה ביניהן?
- [ ] מי מחזיק ב־private key, היכן הוא נשמר, מי יכול לקרוא אותו ומהו תהליך rotation/revocation?
- [ ] מי מורשה לבצע allowance/approval on-chain, ומהו limit המאושר?
- [ ] מהו סכום הכסף המקסימלי שמותר להפקיד בחשבון ה־bot?

### אסטרטגיה ומדיניות סיכון

- [ ] איזה חוק בדיוק נכנס ל־REAL: entry, SL, TP, חלון זמן, inactive windows והאם equality מדויק של Best Ask נשארת?
- [ ] האם הכיוון נקבע לפי YES/NO, Up/Down, או mapping אחר, ומהי בדיקת token mapping המחייבת?
- [ ] מהו סכום ה־canary הראשון? הוא חייב לעמוד גם ב־minimum order של השוק וביחידות הנכונות.
- [ ] האם כניסה היא FOK בלבד? האם מותר fallback ל־FAK או GTC? ברירת המחדל המומלצת לשלב ראשון: אין fallback אוטומטי.
- [ ] מהו worst-price/slippage המרבי לכניסה וליציאה?
- [ ] מהי מדיניות partial fill בכל אחד מהשלבים: entry, TP, SL ו־manual exit?
- [ ] האם מותר יותר מעסקה אחת לאירוע, יותר מצד אחד, או יותר מ־Order פתוח אחד?
- [ ] מהם limits מאושרים: לעסקה, exposure כולל, הפסד יומי, כשלי Order רצופים ועסקאות מפסידות רצופות?
- [ ] האם Stop Loss גובר תמיד על Take Profit באותו tick, כפי שקורה ב־Paper?
- [ ] מה עושים כאשר אין Bid, עומק לא מספיק, WS stale או spread חריג?
- [ ] מהי מדיניות market resolution ו־redemption?

### הפעלה ואחריות

- [ ] מי רשאי לבצע Arm, Start, Pause, Drain, Kill ו־Resume?
- [ ] האם Start דורש two-person approval? מומלץ שכן להפעלה ראשונה ולשינוי limits.
- [ ] לאן נשלחות התראות קריטיות ומי on-call בכל שעות הפעילות?
- [ ] באילו שעות/ימים מותר ל־bot לפעול, ובאיזה timezone קנוני?
- [ ] מהו חלון התחזוקה ומי מאשר deploy בזמן Position פתוח?
- [ ] מהם SLO, RTO ו־RPO?
- [ ] כמה אירועי shadow/Paper רצופים וכמה Orders canary מוצלחים נדרשים לפני הרחבת סכום?
- [ ] מי מבצע reconciliation כספי יומי מול Polymarket וה־wallet?
- [ ] כמה זמן שומרים Orders, fills, deals, audit ו־exports, ומה אסור למחוק?

## תכנית עבודה עד Go-Live

### Gate 1 — Source of truth

1. למזג ל־Git את כל הקוד שבאמת רץ, כולל retention, tests ו־deployment artifacts.
2. להסיר drift ולפרוס אך ורק מ־commit/tag ידוע.
3. לנעול dependencies ולתעד Python/OS/package versions.
4. להעביר full suite, lint/type/security checks, DB integrity ו־migration test.
5. להציג build SHA ב־health הפרטי וב־UI.

### Gate 2 — Real adapter מאובטח

1. לממש client רשמי עם chain, signer, credentials, signature type ו־funder מאומתים.
2. לממש create, cancel, batch cancel, cancel-all ו־get order.
3. לבצע geoblock, balance, allowance, market, tick size, minimum size ו־neg-risk checks לפני Order.
4. להפריד Secret Manager/runtime identity מקובץ non-secret environment.
5. לאסור הדפסת signed payloads, credentials ו־private key בלוגים, audit או errors.

### Gate 3 — מנוע, recovery ו־risk

1. להוסיף מודל `REAL_TRADING` מפורש עם state machine: `DISARMED → ARMED → RUNNING → DRAINING → STOPPED`.
2. לחבר Rules מסוג REAL בלבד ל־Trading Engine, עם הפרדה קשיחה מ־Paper.
3. לתקן את שלושת פערי ה־risk הידועים: reconciliation scheduling, User WS freshness key ו־token mapping statuses; לאמת יחידות minimum size.
4. להוסיף order lifecycle עמיד ל־restart, idempotency במסד ו־uncertain-submit recovery.
5. לממש TP/SL/manual exit/cancel-all/flatten, כולל partial fills.
6. להפוך reconciliation ל־loop רציף עם single-flight, timeout, backoff ו־Kill Switch על gap/staleness.

### Gate 4 — Observability ו־DR

1. התראות חיצוניות עם escalation policy.
2. metrics ו־dashboard ל־WS freshness, order latency/status, exposure, PnL, reconciliation, backups ודיסק.
3. גיבוי אוטומטי מוצפן מחוץ למכונה ו־restore drill מוצלח.
4. runbook incident מתורגל: disconnect, rejected orders, orphan position, crash, DB corruption ו־credential compromise.

### Gate 5 — Shadow ו־Canary

1. להריץ Paper/Shadow על אותו signal ולשמור intent שהיה נשלח, ללא POST.
2. להשוות לאורך תקופה מאושרת בין intent, order book, expected fill ו־reconciliation.
3. לבצע מבחן cancel/restart/recovery בסביבה לא־כספית או באמצעות adapter מבוקר.
4. רק לאחר אישור כתוב: canary אמיתי יחיד בסכום המאושר, עם מפעיל מול המסך ו־Kill מוכן.
5. לא להגדיל limits אוטומטית. כל הרחבה דורשת review של fills, fees, slippage, PnL ו־incidents.

## תנאי Go/No-Go לפני לחיצה על Start עתידי

כל השורות חייבות להיות `PASS` באותה בדיקה, סמוך להפעלה:

- [ ] build SHA מאושר ותואם ל־Git; worktree/artifact ללא drift.
- [ ] כל הבדיקות עוברות; אין כשל health contract.
- [ ] geoblock מחזיר `blocked=false` מאותו egress IP שישלח Orders.
- [ ] login, operator auth ו־reauth פעילים; secrets נטענים ממקור מאושר.
- [ ] signer/funder/signature type/chain/API credentials עברו identity test.
- [ ] balance ו־allowance מספיקים אך מוגבלים למדיניות.
- [ ] Kill Switch נבדק end-to-end, כולל cancel/flatten והתראה.
- [ ] Market WS ו־User WS fresh לאורך חלון SLO מוגדר.
- [ ] reconciliation fresh, 0 gaps ורץ אוטומטית.
- [ ] account identity `VERIFIED` עם snapshot עדכני.
- [ ] market accepting orders; mapping, token IDs, tick, minimum size ו־neg-risk מאומתים.
- [ ] אין Orders/Deals/Positions יתומים מלפני ההפעלה.
- [ ] Rule REAL יחיד ומאושר; amount/slippage/order types תואמים למדיניות.
- [ ] backup אחרון תקין ו־restore drill בתוקף.
- [ ] alerts הגיעו ל־on-call במבחן synthetic.
- [ ] מפעיל ו־approver זמינים; incident channel פתוח.
- [ ] canary plan וקריטריוני עצירה כתובים.

אם תנאי אחד נכשל: לא מכבים Kill Switch ולא מתחילים מסחר.

## נוהל תפעול שוטף — המצב הקיים (Paper/Read-only)

### תחילת משמרת

```bash
systemctl is-active polymarket-live.service nginx
systemctl show polymarket-live.service \
  -p ActiveState -p SubState -p MainPID -p NRestarts -p ExecMainStartTimestamp
curl -fsS http://127.0.0.1:8001/health
curl -fsS https://polymarket.com/api/geoblock
df -h /opt/polymarket-btc-live
```

לאחר login, לבדוק ב־health הפרטי:

- config validation ללא errors;
- Market WS ו־User WS אינם stale;
- PING/PONG ו־market message timestamps מתקדמים;
- Kill Switch נשאר פעיל;
- `real_submission_armed=false`;
- אין Orders/Deals/Positions לא צפויים;
- retention תקין ואין disk warning.

במצב הנוכחי `LIVE_OPERATOR_TOKEN` אינו מוגדר, ולכן אין לנסות פעולות write דרך ה־UI עד להחלטת secrets מסודרת.

### במהלך משמרת

- לעקוב אחר freshness של שני ערוצי ה־WS, לא רק אחרי `CONNECTED`.
- לבדוק שאין growth חריג ב־DB ובדיסק.
- לבדוק ש־service restarts נשאר 0 או מוסבר.
- לא לערוך ישירות את קוד `/opt`, את DB או את environment ללא change record וגיבוי.
- לא להפעיל flags של REAL ולא לכבות Kill Switch.

### סיום משמרת / שינוי גרסה

1. לוודא שאין Paper deal פתוח אם מתוכנן restart.
2. להפעיל Drain דרך הממשק רק לאחר הגדרת operator auth.
3. להמתין ל־`stop_ready=true`.
4. ליצור backup עקבי ולרשום checksum.
5. לפרוס build מזוהה בלבד.
6. להריץ `pytest -q tests`, health, DB integrity ו־WS checks.
7. לתעד מי פרס, commit, שעה, תוצאות ו־rollback point.

## נוהל תפעול עתידי — REAL, לאחר שכל ה־Gates הושלמו

### Arm ו־Start

1. מפעיל ו־approver עוברים יחד על checklist ה־Go/No-Go.
2. מפעילים `ARMED` בלי לאפשר עדיין Rule execution ומריצים preflight מלא.
3. מבצעים reconciliation נוסף ומקפיאים config/build SHA לתיעוד.
4. מפעילים Rule canary יחיד.
5. רק כפעולה האחרונה מכבים Kill Switch ועוברים ל־`RUNNING`.
6. מוודאים מיד ב־audit שהמעבר בוצע על ידי המשתמש הנכון ושאין Order שנשלח לפני ה־Start המאושר.

### בזמן RUNNING

- כל Order חייב לקבל local id, idempotency key, remote id או מצב `UNCERTAIN`, acknowledgment ו־reconciliation.
- כל Position חייב להיות מוסבר על ידי fills; כל orphan מפעיל Kill Switch והתראה.
- WS stale, reconciliation stale/gap, identity mismatch, daily loss, consecutive failures, disk critical או backup failure מפעילים policy ידועה מראש.
- אין לבצע deploy, secret rotation או שינוי limits בזמן exposure פתוח.

### עצירה רגילה

1. לעבור ל־`DRAINING` ולחסום entries חדשים.
2. לנהל exits קיימים עד exposure 0.
3. לבטל Orders פתוחים ולאמת ביטול ב־REST וב־User WS.
4. להריץ reconciliation סופי.
5. לעבור ל־`STOPPED`, להפעיל Kill Switch וליצור backup.

### אירוע חירום

1. להפעיל Kill Switch מיד.
2. לעצור entries חדשים.
3. לפי סוג התקלה והמדיניות המאושרת: cancel-all, ואז flatten מבוקר; אין לבצע flatten עיוור על state stale.
4. לא לעצור process לפני שמירת מצב ו־reconciliation, אלא אם המשך הריצה מגדיל סיכון.
5. לשמור logs/audit/DB snapshot, לפתוח incident ולתעד timestamps.
6. אין Resume עד root cause, reconciliation נקי, identity verified ואישור שני אנשים.

## פקודות אימות בטוחות

בדיקות אלה read-only או פועלות על test databases בלבד:

```bash
/opt/polymarket-btc-live/.venv/bin/python -m pytest -q tests
systemctl status polymarket-live.service --no-pager -l
journalctl -u polymarket-live.service --since "1 hour ago" --no-pager
curl -fsS http://127.0.0.1:8001/health
curl -fsS https://live-poly.dvirtechnologies.com/health
curl -fsS https://polymarket.com/api/geoblock
```

אין להריץ ידנית POST ל־Orders, cancel, allowance update, redemption או endpoint של Kill Switch כחלק מבדיקת health.

## מקורות רשמיים שנבדקו

- [Polymarket — Trading overview and authentication](https://docs.polymarket.com/trading/overview)
- [Polymarket — Create orders and order types](https://docs.polymarket.com/trading/orders/create)
- [Polymarket — Cancel orders](https://docs.polymarket.com/trading/orders/cancel)
- [Polymarket — Geographic restrictions](https://docs.polymarket.com/api-reference/geoblock)
- [Polymarket — Current quickstart and wallet models](https://docs.polymarket.com/quickstart)

## סיכום לבעלים

השלב הבא אינו “להדליק flags”, אלא לקבל החלטות על תשתית חוקית, זהות חשבון, secrets, risk ו־operations; לאחר מכן להשלים מימוש Real Adapter, recovery, reconciliation, alerts ו־DR. עד שכל Gate עובר, המצב המאושר הוא Paper/Read-only בלבד ו־Kill Switch פעיל.
