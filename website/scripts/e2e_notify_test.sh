#!/usr/bin/env bash
# End-to-end test of the order notification loop (run ON the server; secrets stay local).
# Uses a fake customer chat_id so no real Telegram user is messaged (DM attempts fail silently).
set -uo pipefail

APP_DIR="/home/ubuntu/yuntech"
BASE="http://127.0.0.1:3000"
ENV="$APP_DIR/.env.local"

KEY="$(grep -E '^ADMIN_KEY=' "$ENV" | cut -d= -f2- | tr -d '\r')"
[ -z "$KEY" ] && KEY="$(grep -E '^TELEGRAM_SETUP_KEY=' "$ENV" | cut -d= -f2- | tr -d '\r')"
SECRET="$(grep -E '^TELEGRAM_WEBHOOK_SECRET=' "$ENV" | cut -d= -f2- | tr -d '\r')"
FAKE_CHAT="999999001"

echo "== 1) create order =="
OID=$(curl -fsS -X POST "$BASE/api/order" -H 'Content-Type: application/json' \
  -d '{"plan":"pro","edition":"pro","period":"monthly","amount":249,"contact":"e2e-notify@internal","fingerprint":"E2ENOTIFYFP01","lang":"zh"}' \
  | sed -n 's/.*"order_id":"\([^"]*\)".*/\1/p')
echo "order_id=$OID"
[ -z "$OID" ] && { echo "FAIL: no order id"; exit 1; }

echo "== 2) simulate customer /start $OID (bind notify chat) =="
if [ -n "$SECRET" ]; then HDR=(-H "x-telegram-bot-api-secret-token: $SECRET"); else HDR=(); fi
curl -fsS -X POST "$BASE/api/telegram/webhook" -H 'Content-Type: application/json' "${HDR[@]}" \
  -d "{\"update_id\":900000001,\"message\":{\"message_id\":1,\"chat\":{\"id\":$FAKE_CHAT,\"type\":\"private\"},\"text\":\"/start $OID\",\"from\":{\"language_code\":\"zh\"}}}" >/dev/null
echo "webhook posted"

echo "== 3) verify notify_chat bound =="
curl -fsS "$BASE/api/admin/orders?status=pending&key=$KEY" \
  | python3 -c "import sys,json; o=[x for x in json.load(sys.stdin)['orders'] if x['id']=='$OID']; print('notify_chat=', (o[0].get('notify_chat') if o else 'ORDER_MISSING'))"

echo "== 4) mark paid → activated (with fake license code) =="
curl -fsS -X POST "$BASE/api/admin/order-status" -H 'Content-Type: application/json' -H "x-setup-key: $KEY" \
  -d "{\"id\":\"$OID\",\"status\":\"paid\"}" >/dev/null && echo "marked paid"
curl -fsS -X POST "$BASE/api/admin/order-status" -H 'Content-Type: application/json' -H "x-setup-key: $KEY" \
  -d "{\"id\":\"$OID\",\"status\":\"activated\",\"code\":\"E2E-FAKE-LICENSE-CODE\"}" >/dev/null && echo "marked activated"

echo "== 5) customer self-serve pickup (public GET) =="
curl -fsS "$BASE/api/order?id=$OID" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('status=',d['status'],'code=',d.get('code'))"

echo "== 6) SLA scan endpoint =="
curl -fsS "$BASE/api/admin/order-sla?key=$KEY"; echo

echo "== 7) cleanup (cancel test order) =="
curl -fsS -X POST "$BASE/api/admin/order-status" -H 'Content-Type: application/json' -H "x-setup-key: $KEY" \
  -d "{\"id\":\"$OID\",\"status\":\"cancelled\"}" >/dev/null && echo "cancelled $OID"
echo "== DONE =="
