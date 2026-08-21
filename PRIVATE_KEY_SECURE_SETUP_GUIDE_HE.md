# מדריך מאובטח: Private Key של Polymarket ב־GCP Secret Manager

תאריך בדיקת הסביבה: 2026-08-05 (UTC).

## כללי עצירה מחייבים

- אין להדביק או לשלוח את ה־Private Key בצ'אט, ב־ticket, ב־Git או בפקודת shell.
- אין להריץ `set -x`. הבלוקים להלן מתחילים ב־`set +x`.
- אין לשמור את המפתח ב־`.env`, בקובץ זמני או קבוע, ב־systemd, בלוג או process argument.
- אין לקרוא Secret payload לצורך תצוגה. בדיקות הגישה מפנות את הפלט ל־`/dev/null`; בדיקת הכתובת מדפיסה רק כתובות ציבוריות ותוצאת `MATCH`/`MISMATCH`.
- אין לשנות את `LIVE_EXECUTION_MODE=PAPER_TRADING`, את `LIVE_TRADING_ENABLED=false`, את `LIVE_ORDER_SUBMISSION_ENABLED=false`, את Kill Switch או את Pause Entries.
- אין להפעיל Canary, לבצע Allowance/Approval, לשלוח Order, לבצע Cancel, Redemption או פעולה on-chain.
- בכל `MISMATCH`, שגיאת מבנה, הרשאת IAM רחבה לא מוסברת, או מצב שאינו `PAPER + PAUSED`: עוצרים. אין לבצע Restart של השירות לאחר הוספת גרסת הסוד.
- אין למחוק Secret או Secret version. Rollback משבית את הגרסה החדשה בלבד.

## ממצאי בדיקות הקריאה

| פריט | ערך שנמצא |
|---|---|
| Hostname / VM | `polymarket-live-fi` |
| GCP project | `lyrical-carver-490321-t6` |
| Project number | `590957160427` |
| Zone | `europe-north1-a` |
| Region | `europe-north1` |
| Service Account מחובר | `590957160427-compute@developer.gserviceaccount.com` |
| `gcloud` | מותקן ב־`/snap/bin/gcloud`, גרסה `578.0.0` |
| חשבון `gcloud` פעיל בשרת | אותו Compute Service Account |
| OAuth scopes | scopes מצומצמים ל־Storage read-only, Logging, Monitoring, Service Management/Control ו־Trace; **אין `cloud-platform`** |
| Service | `polymarket-live.service`, פעיל ומאופשר |
| WorkingDirectory | `/opt/polymarket-btc-live/repo/polymarket-collector` |
| Unit file | `/etc/systemd/system/polymarket-live.service` |
| EnvironmentFile | `/etc/polymarket-live/live.env`, הרשאות `0600 root:root` |
| ExecStart | `/opt/polymarket-btc-live/.venv/bin/uvicorn live_app:app --host 127.0.0.1 --port 8001` |
| מצב מסחר | `LIVE_EXECUTION_MODE=PAPER_TRADING`, ‏`LIVE_PAPER_TRADING_ENABLED=true` |
| חסימות מסחר אמיתי | `LIVE_TRADING_ENABLED=false`, ‏`LIVE_ORDER_SUBMISSION_ENABLED=false`, ‏`LIVE_CANARY_ARMED=false` |
| בטיחות runtime במסד | `kill_switch=true`, ‏`pause_entries=true`, ‏`canary_armed=false` |
| מצב חשיפה בעת הבדיקה | 0 Orders פתוחים, 0 Deals פתוחים, 0 Strategy Positions פעילות, 0 Intents לא פתורים |
| `/health` | `{"status":"ok"}` |
| Signer צפוי | `0x75D4148E7220b02545f822816901836679B0F7D7` |
| Profile / Funder | `0xcE075637152167517e1492FcF5ff2D131686ee38` |
| Signature type | `1`, שהקוד ממפה ל־`POLY_PROXY` |
| Secret logical name בקוד | `POLYMARKET_PRIVATE_KEY` |
| Secret prefix | `polymarket-live` |
| Secret ID בפועל | `polymarket-live-POLYMARKET_PRIVATE_KEY` |
| Secret project reference | `projects/lyrical-carver-490321-t6/secrets/polymarket-live-POLYMARKET_PRIVATE_KEY/versions/latest` |

`AGENTS.md` לא נמצא ב־repository. מצב Git שנמצא: branch ‏`codex/live-full-implementation-20260805`, ארבעה commits לפני `origin/main`, ללא שינויי working tree לפני יצירת מדריך זה.

לא ניתן היה לזהות מהשרת אם Secret Manager API פעיל, אם הסוד כבר קיים, או אילו bindings קיימים על הסוד/הפרויקט: כל קריאות ה־metadata/IAM נכשלו ב־`ACCESS_TOKEN_SCOPE_INSUFFICIENT`. אין להסיק מכך שה־API או הסוד אינם קיימים. הפקודות המנהליות להלן משיגות את המידע מ־Cloud Shell לפני כל שינוי.

## כיצד הקוד טוען את הסוד

אין צורך לשנות את ה־repository, יחידת systemd או `/etc/polymarket-live/live.env`:

1. קובץ הסביבה כבר מכיל את הערכים הלא־סודיים `GOOGLE_CLOUD_PROJECT=lyrical-carver-490321-t6` ו־`GOOGLE_SECRET_MANAGER_PREFIX=polymarket-live`.
2. `live_app.py` יוצר `GoogleSecretManagerProvider` בזמן startup וטוען סודות לזיכרון התהליך בלבד.
3. `live/secrets.py` מחבר prefix, מקף ושם לוגי; לכן `POLYMARKET_PRIVATE_KEY` הופך ל־`polymarket-live-POLYMARKET_PRIVATE_KEY` ונקרא מ־`versions/latest`.
4. `live/adapters/polymarket.py` מפעיל `Account.from_key(private_key)`, גוזר כתובת ומסרב להמשיך אם אינה תואמת ל־`POLYMARKET_SIGNER_ADDRESS`.
5. אותו adapter בודק גם שה־SDK מחזיר את ה־Signer, ה־Wallet וה־Wallet Type הצפויים. Type ‏`1` ממופה ל־`POLY_PROXY`; ה־wallet הוא ה־Funder המוגדר.

האפליקציה מקבלת מחרוזת hex ש־`eth-account` מסוגל לפרש. תהליך ההזנה להלן מקבל קלט עם או בלי `0x`, דורש בדיוק 64 ספרות hex, ושומר תמיד פורמט קנוני עם `0x`.

## סביבת הרצה 1: שרת פינלנד — בדיקות מקדימות בלבד

הבלוק הבא אינו משנה דבר. יש להריץ אותו לפני שינוי OAuth/IAM/Secret. הוא אינו מדפיס ערכי סוד.

```bash
set +x
set -o pipefail

cd /opt/polymarket-btc-live/repo

test "$(hostname)" = "polymarket-live-fi"
test "$(pwd)" = "/opt/polymarket-btc-live/repo"
git status --short --branch

systemctl is-active polymarket-live.service
systemctl show polymarket-live.service \
  -p FragmentPath -p WorkingDirectory -p ActiveState -p SubState --no-pager
curl --fail --silent --show-error http://127.0.0.1:8001/health
printf '\n'

# מציגים רק משתני בטיחות וכתובות ציבוריות מרשימה סגורה.
sudo -n awk -F= '
BEGIN {OFS="="}
/^[[:space:]]*(TRADING_MODE|LIVE_EXECUTION_MODE|LIVE_PAPER_TRADING_ENABLED|LIVE_TRADING_ENABLED|LIVE_ORDER_SUBMISSION_ENABLED|LIVE_KILL_SWITCH|LIVE_PAUSE_ENTRIES|LIVE_CANARY_ARMED|LIVE_ADAPTER|GOOGLE_CLOUD_PROJECT|GOOGLE_SECRET_MANAGER_PREFIX|POLYMARKET_SIGNER_ADDRESS|POLYMARKET_PROFILE_ADDRESS|POLYMARKET_FUNDER_ADDRESS|POLYMARKET_SIGNATURE_TYPE)[[:space:]]*=/ {
  key=$1; gsub(/[[:space:]]/, "", key); value=substr($0,index($0,"=")+1); print key,value
}' /etc/polymarket-live/live.env

# אסור ששם משתנה המפתח עצמו יופיע ב־EnvironmentFile.
if sudo -n grep -qE '^[[:space:]]*(export[[:space:]]+)?POLYMARKET_PRIVATE_KEY=' /etc/polymarket-live/live.env; then
  echo 'STOP: POLYMARKET_PRIVATE_KEY נמצא ב־EnvironmentFile; אין להמשיך.' >&2
  return 1 2>/dev/null || exit 1
else
  echo 'OK: Private Key אינו שמור ב־EnvironmentFile.'
fi

sqlite3 -readonly /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT key,value FROM live_system_state WHERE key IN ('kill_switch','pause_entries','canary_armed') ORDER BY key;"

sqlite3 -readonly /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT 'open_orders',COUNT(*) FROM live_orders WHERE status NOT IN ('filled','cancelled','unmatched','failed','blocked')
   UNION ALL SELECT 'active_positions',COUNT(*) FROM live_strategy_positions WHERE state IN ('OPEN','TP_OPEN','EXITING','EXIT_RECONCILIATION_REQUIRED')
   UNION ALL SELECT 'unresolved_intents',COUNT(*) FROM live_strategy_intents WHERE state NOT IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED','REDEEMED')
   UNION ALL SELECT 'open_deals',COUNT(*) FROM live_deals WHERE status IN ('created','entry_pending','open','partially_open','exit_pending');"

METADATA_URL='http://metadata.google.internal/computeMetadata/v1'
METADATA_HEADER='Metadata-Flavor: Google'
curl -fsS -H "$METADATA_HEADER" "$METADATA_URL/project/project-id"; printf '\n'
curl -fsS -H "$METADATA_HEADER" "$METADATA_URL/instance/name"; printf '\n'
curl -fsS -H "$METADATA_HEADER" "$METADATA_URL/instance/zone"; printf '\n'
curl -fsS -H "$METADATA_HEADER" "$METADATA_URL/instance/service-accounts/default/email"; printf '\n'
curl -fsS -H "$METADATA_HEADER" "$METADATA_URL/instance/service-accounts/default/scopes"; printf '\n'

# במצב שנמצא כעת פקודה זו חייבת להדפיס MISSING ולסמן שיש לבצע את תיקון ה־scope.
if curl -fsS -H "$METADATA_HEADER" \
  "$METADATA_URL/instance/service-accounts/default/scopes" \
  | grep -Fxq 'https://www.googleapis.com/auth/cloud-platform'; then
  echo 'cloud-platform: PRESENT'
else
  echo 'cloud-platform: MISSING'
fi
```

תוצאת החובה לפני ההמשך: service פעיל, `/health` תקין, `PAPER_TRADING`, מסחר/שליחה/Canary כבויים, `kill_switch=true`, ‏`pause_entries=true`, וכל ארבע הספירות הן 0. אם לא — עוצרים.

## סביבת הרצה 2: GCP Cloud Shell / מחשב ניהולי

יש להריץ עם חשבון GCP ניהולי שמורשה לבדוק Service Usage, Compute, Secret Manager ו־IAM. ה־Private Key מוקלד **רק** ב־prompt המוסתר שבבלוק. אין להכניס אותו במקום placeholder או לשורת פקודה.

> **אזהרת downtime:** שלב OAuth עוצר ומפעיל מחדש את `polymarket-live-fi`; השירות אינו זמין בזמן שה־VM כבוי. בסביבה שנבדקה `cloud-platform` חסר, ולכן אין לדלג על שלב זה. הפקודה משמרת במפורש את כתובת ה־Service Account הקיימת.

```bash
set +x
set -o pipefail

PROJECT_ID='lyrical-carver-490321-t6'
VM_NAME='polymarket-live-fi'
ZONE='europe-north1-a'
REGION='europe-north1'
VM_SERVICE_ACCOUNT='590957160427-compute@developer.gserviceaccount.com'
SECRET_ID='polymarket-live-POLYMARKET_PRIVATE_KEY'

gcloud auth list --filter=status:ACTIVE --format='table(account,status)'
gcloud config set project "$PROJECT_ID"
gcloud projects describe "$PROJECT_ID" --format='value(projectId,projectNumber)'

# בדיקה: האם Secret Manager API פעיל. אין payload.
API_NAME="$(gcloud services list --enabled --project="$PROJECT_ID" \
  --filter='config.name=secretmanager.googleapis.com' --format='value(config.name)')"
if [ "$API_NAME" = 'secretmanager.googleapis.com' ]; then
  echo 'Secret Manager API: ENABLED'
else
  echo 'Secret Manager API: DISABLED'
  # שינוי GCP: מפעיל API בלבד; אינו יוצר Secret ואינו מעניק IAM.
  gcloud services enable secretmanager.googleapis.com --project="$PROJECT_ID"
fi

# בדיקת IAM קיימת לפני שינוי. הפלט מכיל principals/roles בלבד, לא סודות.
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.members:serviceAccount:${VM_SERVICE_ACCOUNT}" \
  --format='table(bindings.role,bindings.members,bindings.condition.expression)'

# metadata בלבד: האם הסוד קיים ומה ה־replication שלו.
if gcloud secrets describe "$SECRET_ID" --project="$PROJECT_ID" \
  --format='yaml(name,replication,createTime,labels)' >/dev/null 2>&1; then
  echo "Secret קיים: $SECRET_ID — אין ליצור מחדש ואין למחוק גרסאות."
  gcloud secrets describe "$SECRET_ID" --project="$PROJECT_ID" \
    --format='yaml(name,replication,createTime,labels)'
  gcloud secrets get-iam-policy "$SECRET_ID" --project="$PROJECT_ID" \
    --format='table(bindings.role,bindings.members,bindings.condition.expression)'
else
  echo "Secret לא נמצא: $SECRET_ID"
  # שינוי GCP: יוצר container ריק בלבד, ב־Region של ה־VM; עדיין אין payload.
  gcloud secrets create "$SECRET_ID" --project="$PROJECT_ID" \
    --replication-policy=user-managed --locations="$REGION"
fi

# ===== DOWNTIME מתחיל כאן: תיקון OAuth scope החסר =====
# בדיקה חוזרת של ה־Service Account לפני עצירה, כדי לא להחליף זהות בטעות.
ATTACHED_SA="$(gcloud compute instances describe "$VM_NAME" \
  --project="$PROJECT_ID" --zone="$ZONE" \
  --format='value(serviceAccounts[0].email)')"
if [ "$ATTACHED_SA" != "$VM_SERVICE_ACCOUNT" ]; then
  echo "STOP: Service Account מחובר אינו הערך שנבדק: $ATTACHED_SA" >&2
  return 1 2>/dev/null || exit 1
fi

# מסוכן / downtime: עוצר את כל ה־VM ואת השירות.
gcloud compute instances stop "$VM_NAME" --project="$PROJECT_ID" --zone="$ZONE"

# שינוי תצורת VM: משמר את אותו Service Account ומחליף scopes ב־cloud-platform.
# cloud-platform הוא OAuth scope; ההרשאה בפועל עדיין מוגבלת באמצעות IAM.
gcloud compute instances set-service-account "$VM_NAME" \
  --project="$PROJECT_ID" --zone="$ZONE" \
  --service-account="$VM_SERVICE_ACCOUNT" \
  --scopes=cloud-platform

# מסוכן / downtime מסתיים לאחר העלייה: מפעיל את ה־VM מחדש.
gcloud compute instances start "$VM_NAME" --project="$PROJECT_ID" --zone="$ZONE"
for attempt in $(seq 1 60); do
  VM_STATUS="$(gcloud compute instances describe "$VM_NAME" \
    --project="$PROJECT_ID" --zone="$ZONE" --format='value(status)')"
  [ "$VM_STATUS" = 'RUNNING' ] && break
  sleep 5
done
if [ "$VM_STATUS" != 'RUNNING' ]; then
  echo "STOP: ה־VM לא הגיע ל־RUNNING; מצב נוכחי: $VM_STATUS" >&2
  return 1 2>/dev/null || exit 1
fi

# בדיקות קריאה לאחר העלייה. אם SSH עדיין אינו זמין, להמתין ולנסות שוב.
gcloud compute instances describe "$VM_NAME" --project="$PROJECT_ID" --zone="$ZONE" \
  --format='yaml(name,status,serviceAccounts)'
gcloud compute ssh "$VM_NAME" --project="$PROJECT_ID" --zone="$ZONE" \
  --command='hostname; systemctl is-active polymarket-live.service; curl -fsS http://127.0.0.1:8001/health; printf "\n"'

echo 'עצור כאן וחזור לבלוק שרת פינלנד כדי לוודא שוב PAPER + PAUSED.'
read -r -p 'לאחר שאימתת PAPER + PAUSED, הקלד CONTINUE: ' SCOPE_CONFIRMATION
if [ "$SCOPE_CONFIRMATION" != 'CONTINUE' ]; then
  unset SCOPE_CONFIRMATION
  echo 'STOP: לא התקבל אישור בטיחות.' >&2
  return 1 2>/dev/null || exit 1
fi
unset SCOPE_CONFIRMATION

# שמירת גרסת rollback קודמת — metadata בלבד, בלי payload.
PREVIOUS_ENABLED_VERSION="$(gcloud secrets versions list "$SECRET_ID" \
  --project="$PROJECT_ID" --filter='state=ENABLED' --sort-by='~createTime' --limit=1 \
  --format='value(name)')"
printf 'PREVIOUS_ENABLED_VERSION=%s\n' "${PREVIOUS_ENABLED_VERSION:-NONE}"

# ===== הזנת ה־Private Key דרך prompt מוסתר בלבד =====
# trap מבטיח unset גם ב־Ctrl-C/שגיאה. המשתנים אינם מועברים ב־process arguments.
trap 'unset PRIVATE_KEY_INPUT PRIVATE_KEY_HEX' EXIT HUP INT TERM
IFS= read -r -s -p 'הדבק Private Key (הקלט מוסתר) ולחץ Enter: ' PRIVATE_KEY_INPUT
printf '\n'

case "$PRIVATE_KEY_INPUT" in
  0x*|0X*) PRIVATE_KEY_HEX="${PRIVATE_KEY_INPUT:2}" ;;
  *)       PRIVATE_KEY_HEX="$PRIVATE_KEY_INPUT" ;;
esac

if [[ ! "$PRIVATE_KEY_HEX" =~ ^[[:xdigit:]]{64}$ ]]; then
  unset PRIVATE_KEY_INPUT PRIVATE_KEY_HEX
  trap - EXIT HUP INT TERM
  echo 'STOP: מבנה המפתח אינו 32 bytes hex. דבר לא נשלח ל־Secret Manager.' >&2
  return 1 2>/dev/null || exit 1
fi

# נשמר פורמט קנוני 0x + 64 hex. הערך עובר ל־gcloud רק דרך stdin.
PRIVATE_KEY_INPUT="0x${PRIVATE_KEY_HEX}"
unset PRIVATE_KEY_HEX

if NEW_VERSION_RESOURCE="$(printf '%s' "$PRIVATE_KEY_INPUT" \
  | gcloud secrets versions add "$SECRET_ID" --project="$PROJECT_ID" \
      --data-file=- --format='value(name)')"; then
  unset PRIVATE_KEY_INPUT
  trap - EXIT HUP INT TERM
else
  unset PRIVATE_KEY_INPUT
  trap - EXIT HUP INT TERM
  echo 'STOP: הוספת Secret version נכשלה.' >&2
  return 1 2>/dev/null || exit 1
fi

NEW_VERSION="${NEW_VERSION_RESOURCE##*/}"
unset NEW_VERSION_RESOURCE
printf 'NEW_VERSION=%s (יש לרשום מספר זה לצורך rollback)\n' "$NEW_VERSION"
gcloud secrets versions describe "$NEW_VERSION" --secret="$SECRET_ID" \
  --project="$PROJECT_ID" --format='yaml(name,state,createTime)'

# IAM מינימלי: binding על הסוד הספציפי בלבד.
gcloud secrets add-iam-policy-binding "$SECRET_ID" --project="$PROJECT_ID" \
  --member="serviceAccount:${VM_SERVICE_ACCOUNT}" \
  --role='roles/secretmanager.secretAccessor'

gcloud secrets get-iam-policy "$SECRET_ID" --project="$PROJECT_ID" \
  --format='table(bindings.role,bindings.members,bindings.condition.expression)'

# ביקורת: אסור להשאיר secretAccessor רחב ברמת הפרויקט עבור ה־VM.
gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten='bindings[].members' \
  --filter="bindings.members:serviceAccount:${VM_SERVICE_ACCOUNT} AND bindings.role:roles/secretmanager.secretAccessor" \
  --format='table(bindings.role,bindings.members,bindings.condition.expression)'

# אם ורק אם הפקודה הקודמת הציגה binding לא־מותנה של secretAccessor ברמת הפרויקט,
# מסירים אותו אחרי שווידאנו שה־binding הספציפי לסוד קיים.
# אזהרת IAM — שינוי הרשאות; אין להריץ אם הפלט היה ריק או אם יש condition שדורש ניתוח.
# gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
#   --member="serviceAccount:${VM_SERVICE_ACCOUNT}" \
#   --role='roles/secretmanager.secretAccessor'

# אין לתת ל־VM roles/secretmanager.admin, secretVersionManager או secretVersionAdder.
# אם טבלת ה־IAM הראשונית הציגה role רחב אחר, יש לעצור ולנתח אותו לפני Restart.

# metadata בלבד: רשימת הגרסאות; לעולם לא versions access כאן.
gcloud secrets versions list "$SECRET_ID" --project="$PROJECT_ID" \
  --sort-by='~createTime' --limit=10 --format='table(name,state,createTime,destroyTime)'

# ===== ROLLBACK ב־Cloud Shell — לא להריץ עכשיו =====
# במקרה של כשל, החלף NEW_VERSION_RECORDED במספר שהודפס למעלה, ואז הרץ:
# NEW_VERSION_TO_DISABLE='NEW_VERSION_RECORDED'
# gcloud secrets versions describe "$NEW_VERSION_TO_DISABLE" \
#   --secret="$SECRET_ID" --project="$PROJECT_ID" --format='yaml(name,state,createTime)'
# gcloud secrets versions disable "$NEW_VERSION_TO_DISABLE" \
#   --secret="$SECRET_ID" --project="$PROJECT_ID"
# אין להריץ versions destroy ואין למחוק את ה־Secret.
```

אם בדיקת ה־IAM ברמת הפרויקט מציגה `roles/secretmanager.secretAccessor`, יש להסיר את ה־binding הרחב באמצעות הפקודה המסומנת רק לאחר אימות ה־binding הספציפי. אם מופיעים `Owner`, ‏`Editor`, ‏`secretmanager.admin` או role מותאם אישית, אין לנחש אם ניתן להסירו: יש לבצע review של ה־role וה־condition. תנאי ההצלחה של מדריך זה דורש של־VM לא תהיה דרך רחבה אחרת לקרוא Secrets.

## סביבת הרצה 3: שרת פינלנד — אימות מאובטח ו־Restart

מתחילים רק לאחר שה־VM עלה, ה־scope תוקן, הגרסה נוצרה וה־IAM הספציפי הוגדר. בלוק זה קורא את הסוד לזיכרון של תהליך Python קצר בלבד; הוא אינו מדפיס או שומר אותו.

> **אזהרת הפסקת שירות:** `systemctl restart` גורם להפסקה קצרה. אין `daemon-reload`, משום שלא השתנו unit או EnvironmentFile. ה־Restart מתבצע רק לאחר `MATCH` וכל בדיקות הבטיחות.

```bash
set +x
set -o pipefail

cd /opt/polymarket-btc-live/repo/polymarket-collector
PROJECT_ID='lyrical-carver-490321-t6'
SECRET_ID='polymarket-live-POLYMARKET_PRIVATE_KEY'
EXPECTED_SIGNER='0x75D4148E7220b02545f822816901836679B0F7D7'
EXPECTED_PROFILE='0xcE075637152167517e1492FcF5ff2D131686ee38'
EXPECTED_FUNDER='0xcE075637152167517e1492FcF5ff2D131686ee38'
EXPECTED_SIGNATURE_TYPE='1'

test "$(hostname)" = 'polymarket-live-fi'
systemctl is-active polymarket-live.service
curl --fail --silent --show-error http://127.0.0.1:8001/health
printf '\n'

METADATA_URL='http://metadata.google.internal/computeMetadata/v1'
METADATA_HEADER='Metadata-Flavor: Google'
test "$(curl -fsS -H "$METADATA_HEADER" "$METADATA_URL/project/project-id")" = "$PROJECT_ID"
test "$(curl -fsS -H "$METADATA_HEADER" "$METADATA_URL/instance/name")" = 'polymarket-live-fi'
test "$(curl -fsS -H "$METADATA_HEADER" "$METADATA_URL/instance/service-accounts/default/email")" \
  = '590957160427-compute@developer.gserviceaccount.com'
curl -fsS -H "$METADATA_HEADER" "$METADATA_URL/instance/service-accounts/default/scopes" \
  | grep -Fx 'https://www.googleapis.com/auth/cloud-platform'

# אימות PAPER + PAUSED חוזר, לפני גישה לסוד.
sudo -n awk -F= '
BEGIN {OFS="="}
/^[[:space:]]*(LIVE_EXECUTION_MODE|LIVE_PAPER_TRADING_ENABLED|LIVE_TRADING_ENABLED|LIVE_ORDER_SUBMISSION_ENABLED|LIVE_KILL_SWITCH|LIVE_PAUSE_ENTRIES|LIVE_CANARY_ARMED|POLYMARKET_SIGNER_ADDRESS|POLYMARKET_PROFILE_ADDRESS|POLYMARKET_FUNDER_ADDRESS|POLYMARKET_SIGNATURE_TYPE|GOOGLE_CLOUD_PROJECT|GOOGLE_SECRET_MANAGER_PREFIX)[[:space:]]*=/ {
  key=$1; gsub(/[[:space:]]/, "", key); value=substr($0,index($0,"=")+1); print key,value
}' /etc/polymarket-live/live.env

sqlite3 -readonly /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT key,value FROM live_system_state WHERE key IN ('kill_switch','pause_entries','canary_armed') ORDER BY key;"

# בדיקת הרשאת Service Account: ה־payload נקרא אך נזרק ואינו מודפס.
if gcloud secrets versions access latest --secret="$SECRET_ID" \
  --project="$PROJECT_ID" >/dev/null; then
  echo 'Secret access: OK (payload לא הודפס)'
else
  echo 'STOP: ל־VM אין גישה לסוד.' >&2
  return 1 2>/dev/null || exit 1
fi

# בדיקת מבנה + נגזרת כתובת באותה ספרייה ובאותו eth-account שהקוד משתמש בהם.
# ה־Private Key נשאר בזיכרון התהליך הקצר בלבד. הפלט מוגבל לכתובות ציבוריות ולתוצאה.
export VERIFY_PROJECT_ID="$PROJECT_ID"
export VERIFY_SECRET_ID="$SECRET_ID"
export VERIFY_EXPECTED_SIGNER="$EXPECTED_SIGNER"
if ! /opt/polymarket-btc-live/.venv/bin/python - <<'PY'
import os
import re
import sys
from eth_account import Account
from google.cloud import secretmanager

project = os.environ["VERIFY_PROJECT_ID"]
secret_id = os.environ["VERIFY_SECRET_ID"]
expected = os.environ["VERIFY_EXPECTED_SIGNER"]
name = f"projects/{project}/secrets/{secret_id}/versions/latest"
private_key = None
raw = None
try:
    private_key = secretmanager.SecretManagerServiceClient().access_secret_version(
        request={"name": name}
    ).payload.data.decode("utf-8").strip()
    raw = private_key[2:] if private_key[:2].lower() == "0x" else private_key
    if not re.fullmatch(r"[0-9a-fA-F]{64}", raw or ""):
        print(f"Expected signer: {expected}")
        print("Result: MISMATCH")
        raise SystemExit(42)
    derived = Account.from_key(private_key).address
    match = derived.lower() == expected.lower()
    print(f"Derived signer: {derived}")
    print(f"Expected signer: {expected}")
    print(f"Result: {'MATCH' if match else 'MISMATCH'}")
    raise SystemExit(0 if match else 42)
finally:
    private_key = None
    raw = None
PY
then
  unset VERIFY_PROJECT_ID VERIFY_SECRET_ID VERIFY_EXPECTED_SIGNER
  echo 'STOP: MISMATCH/מבנה לא תקין. אין לבצע Restart.' >&2
  return 1 2>/dev/null || exit 1
fi
unset VERIFY_PROJECT_ID VERIFY_SECRET_ID VERIFY_EXPECTED_SIGNER

# התאמת ה־Proxy/Funder/Signature Type הציבוריים לתצורה שנבדקה.
test "$EXPECTED_PROFILE" = "$EXPECTED_FUNDER"
test "$EXPECTED_SIGNATURE_TYPE" = '1'
printf 'Profile/Funder: %s\nSignature type: %s (POLY_PROXY)\n' \
  "$EXPECTED_FUNDER" "$EXPECTED_SIGNATURE_TYPE"

# בדיקה נוספת מול הקוד הקיים: מיפוי type 1 חייב להיות POLY_PROXY.
/opt/polymarket-btc-live/.venv/bin/python - <<'PY'
from live.adapters.polymarket import WALLET_TYPES
raise SystemExit(0 if WALLET_TYPES.get(1) == "POLY_PROXY" else 1)
PY

# Snapshot ציבורי בלבד עשוי לאמת proxy wallet. אם אין פעילות ציבורית, התוצאה עשויה
# להישאר UNVERIFIED; במקרה כזה אין לטעון שנעשה account-truth verification ואין להתקדם למסחר אמיתי.
export VERIFY_PROFILE="$EXPECTED_PROFILE"
export VERIFY_FUNDER="$EXPECTED_FUNDER"
/opt/polymarket-btc-live/.venv/bin/python - <<'PY'
import asyncio
import os
from live.account_identity import PublicAccountIdentityClient

profile = os.environ["VERIFY_PROFILE"]
funder = os.environ["VERIFY_FUNDER"]
result = asyncio.run(PublicAccountIdentityClient().resolve(profile))
proxy = result.resolved_proxy_wallet
print(f"Configured profile: {profile}")
print(f"Configured funder: {funder}")
print(f"Resolved public proxy: {proxy or 'UNVERIFIED'}")
print(f"Proxy/Funder result: {'MATCH' if proxy and proxy.lower() == funder.lower() else 'UNVERIFIED'}")
PY
unset VERIFY_PROFILE VERIFY_FUNDER

# בדיקות דליפה לפני Restart — מדפיסות תוצאה בלבד, לעולם לא match.
if git grep -qE '(0x)?[0-9A-Fa-f]{64}' -- ':!requirements.lock.txt'; then
  echo 'STOP: נמצא candidate באורך Private Key בקובץ tracked; יש לבדוק ללא הדפסתו.' >&2
  return 1 2>/dev/null || exit 1
else
  echo 'Git leak scan: OK'
fi
if history | grep -Eq '(0x)?[0-9A-Fa-f]{64}'; then
  echo 'STOP: נמצא candidate ב־shell history; אין להמשיך.' >&2
  return 1 2>/dev/null || exit 1
else
  echo 'History leak scan: OK'
fi
if ps -eo args= | grep -Eq '(0x)?[0-9A-Fa-f]{64}'; then
  echo 'STOP: נמצא candidate ב־process arguments; אין להמשיך.' >&2
  return 1 2>/dev/null || exit 1
else
  echo 'Process-argument leak scan: OK'
fi

# אין צורך ואין להריץ systemctl daemon-reload: unit/env references לא השתנו.
RESTART_FROM="$(date --iso-8601=seconds)"

# DOWNTIME קצר: Restart בטוח; אין כאן הפעלת מסחר או Canary.
sudo systemctl restart polymarket-live.service

for attempt in $(seq 1 30); do
  if systemctl is-active --quiet polymarket-live.service \
     && curl -fsS http://127.0.0.1:8001/health >/dev/null; then
    break
  fi
  sleep 2
done

systemctl status polymarket-live.service --no-pager --lines=20
curl --fail --silent --show-error http://127.0.0.1:8001/health
printf '\n'

# לוגים אחרונים עם redaction של כל candidate באורך 32 bytes ושל ערכי PRIVATE_KEY.
sudo journalctl -u polymarket-live.service --since "$RESTART_FROM" --no-pager -o cat \
  | sed -E \
      -e 's/(0x)?[0-9A-Fa-f]{64}/[REDACTED_PRIVATE_KEY_CANDIDATE]/g' \
      -e 's/(POLYMARKET_PRIVATE_KEY[=:][[:space:]]*)[^[:space:]]+/\1[REDACTED]/Ig' \
  | tail -n 100

# סריקת journal ללא הדפסת שורות חשודות.
if sudo journalctl -u polymarket-live.service --since "$RESTART_FROM" --no-pager -o cat \
  | grep -Eq '(POLYMARKET_PRIVATE_KEY[=:][[:space:]]*(0x)?[0-9A-Fa-f]{64}|(^|[^0-9A-Fa-f])(0x)?[0-9A-Fa-f]{64}([^0-9A-Fa-f]|$))'; then
  echo 'STOP: נמצא candidate של Private Key בלוגים. השאר PAUSED ובצע incident handling.' >&2
  return 1 2>/dev/null || exit 1
else
  echo 'Journal leak scan: OK'
fi

# אימות סופי: startup מכריח Pause Entries; Kill Switch חייב להישאר true.
sqlite3 -readonly /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT key,value FROM live_system_state WHERE key IN ('kill_switch','pause_entries','canary_armed') ORDER BY key;"

sqlite3 -readonly /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT 'open_orders',COUNT(*) FROM live_orders WHERE status NOT IN ('filled','cancelled','unmatched','failed','blocked')
   UNION ALL SELECT 'active_positions',COUNT(*) FROM live_strategy_positions WHERE state IN ('OPEN','TP_OPEN','EXITING','EXIT_RECONCILIATION_REQUIRED')
   UNION ALL SELECT 'unresolved_intents',COUNT(*) FROM live_strategy_intents WHERE state NOT IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED','REDEEMED')
   UNION ALL SELECT 'open_deals',COUNT(*) FROM live_deals WHERE status IN ('created','entry_pending','open','partially_open','exit_pending');"

sudo -n awk -F= '
/^[[:space:]]*(LIVE_EXECUTION_MODE|LIVE_PAPER_TRADING_ENABLED|LIVE_TRADING_ENABLED|LIVE_ORDER_SUBMISSION_ENABLED|LIVE_KILL_SWITCH|LIVE_PAUSE_ENTRIES|LIVE_CANARY_ARMED)[[:space:]]*=/ {print $0}
' /etc/polymarket-live/live.env

unset PROJECT_ID SECRET_ID EXPECTED_SIGNER EXPECTED_PROFILE EXPECTED_FUNDER EXPECTED_SIGNATURE_TYPE RESTART_FROM
```

אם `Resolved public proxy` הוא `UNVERIFIED`, ה־Private Key/Signer יכול עדיין להיות `MATCH`, אך account-truth המלא לא הוכח ממקור ציבורי. המערכת חייבת להישאר `PAPER + PAUSED`; אין להשתמש בכך כאישור ל־Canary או למסחר אמיתי. הקוד עצמו יבצע שוב signer/wallet/type checks כאשר ה־secure client יאותחל.

## Rollback

Rollback אינו מוחק דבר:

1. ב־Cloud Shell, מציגים metadata של הגרסאות ורושמים את מספר הגרסה החדשה.
2. מוודאים שהמספר תואם ל־`NEW_VERSION` שנרשם בעת ההוספה.
3. משביתים רק אותה באמצעות `gcloud secrets versions disable` שבסוף בלוק Cloud Shell.
4. Secret Manager `latest` יחזור לגרסה המאופשרת הקודמת, אם קיימת.
5. בשרת פינלנד מבצעים Restart קצר כדי לנקות את הערך החדש מזיכרון התהליך ולטעון את הקודם, ואז בודקים שוב health ו־`PAPER + PAUSED`.

פקודות שרת פינלנד לאחר השבתת הגרסה ב־Cloud Shell:

```bash
set +x
cd /opt/polymarket-btc-live/repo/polymarket-collector

# DOWNTIME קצר לצורך rollback. אין daemon-reload ואין שינוי דגלי מסחר.
sudo systemctl restart polymarket-live.service

for attempt in $(seq 1 30); do
  if systemctl is-active --quiet polymarket-live.service \
     && curl -fsS http://127.0.0.1:8001/health >/dev/null; then
    break
  fi
  sleep 2
done

systemctl status polymarket-live.service --no-pager --lines=20
curl --fail --silent --show-error http://127.0.0.1:8001/health; printf '\n'
sqlite3 -readonly /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT key,value FROM live_system_state WHERE key IN ('kill_switch','pause_entries','canary_armed') ORDER BY key;"
sqlite3 -readonly /opt/polymarket-btc-live/poly_live.sqlite3 \
  "SELECT 'open_orders',COUNT(*) FROM live_orders WHERE status NOT IN ('filled','cancelled','unmatched','failed','blocked')
   UNION ALL SELECT 'active_positions',COUNT(*) FROM live_strategy_positions WHERE state IN ('OPEN','TP_OPEN','EXITING','EXIT_RECONCILIATION_REQUIRED')
   UNION ALL SELECT 'unresolved_intents',COUNT(*) FROM live_strategy_intents WHERE state NOT IN ('FILLED','PARTIAL_FINAL','ZERO_FILL','CANCELED','REJECTED','FAILED','SETTLED','REDEEMED');"
```

אם לא הייתה גרסה מאופשרת קודמת, לאחר השבתת הגרסה החדשה האפליקציה לא תוכל לטעון `POLYMARKET_PRIVATE_KEY`; זה rollback תקין למצב "Signer לא מוגדר" כל עוד השירות נשאר בריא, PAPER ו־PAUSED. אין להשמיד את הגרסה ואין למחוק את הסוד ללא אישור מפורש ונפרד.

## תנאי הצלחה

השלב הושלם רק כאשר כולם מתקיימים:

- Secret version חדשה קיימת במצב `ENABLED`, ומספרה נרשם ל־rollback.
- ל־VM יש `cloud-platform` והוא עדיין מחובר בדיוק ל־`590957160427-compute@developer.gserviceaccount.com`.
- ל־Service Account יש `roles/secretmanager.secretAccessor` על `polymarket-live-POLYMARKET_PRIVATE_KEY` בלבד, ואין לו הרשאת Secret Manager רחבה אחרת.
- גישת ה־VM ל־Secret עובדת בלי להדפיס payload.
- בדיקת `eth-account` מחזירה `MATCH` מול `0x75D4148E7220b02545f822816901836679B0F7D7`.
- Funder/Profile נשארים `0xcE075637152167517e1492FcF5ff2D131686ee38` ו־Signature Type נשאר `1 (POLY_PROXY)`; כל מגבלה של public proxy verification מתועדת.
- השירות פעיל ו־`/health` מחזיר `ok`.
- `LIVE_EXECUTION_MODE=PAPER_TRADING`, מסחר אמיתי/שליחת Orders/Canary כבויים, ו־Kill Switch + Pause Entries פעילים.
- אין Orders, Deals, Positions או Intents חדשים/פתוחים כתוצאה מהתהליך.
- סריקות Git, history, process arguments ו־journal אינן מזהות Private Key.
- לא בוצעו Order, Allowance/Approval, Canary, Restart לפני `MATCH`, או שינוי שמאפשר כניסות למסחר.

## סדר הביצוע

1. להריץ את בלוק "שרת פינלנד — בדיקות מקדימות בלבד" ולעצור אם מצב הבטיחות אינו מדויק.
2. להריץ ב־Cloud Shell את בדיקות API/IAM/Secret metadata.
3. לבצע את תיקון OAuth scope — זהו downtime של ה־VM — תוך שמירה על אותו Service Account.
4. לאמת שה־VM והשירות חזרו ושוב נמצאים `PAPER + PAUSED`.
5. ליצור את Secret container רק אם אינו קיים; לא ליצור מחדש ולא למחוק גרסאות.
6. להזין את המפתח ב־prompt המוסתר, להוסיף גרסה דרך stdin ולרשום את מספרה.
7. להעניק `secretAccessor` על הסוד הספציפי בלבד ולבצע audit ל־IAM ברמת הפרויקט.
8. להריץ בשרת את בדיקת הגישה וה־Signer; בכל `MISMATCH` לעצור ללא Restart.
9. רק לאחר `MATCH`, לבצע Restart קצר, health/log/leak/state checks, ולהשאיר `PAPER + PAUSED`.
10. במקרה כשל: להשבית בלבד את הגרסה החדשה, לבצע Restart rollback ולבדוק שוב את מצב הבטיחות.
