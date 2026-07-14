#!/usr/bin/env bash
# Validate the stale-paid (offline-fallback) SLA detection: backdate a paid order's paid_at,
# confirm the scan flags it exactly once (dedup on 2nd call), then clean up.
# NOTE: this sends ONE real admin alert to prove delivery — the test order id is identifiable.
set -uo pipefail

APP_DIR="/home/ubuntu/yuntech"
BASE="http://127.0.0.1:3000"
ENV="$APP_DIR/.env.local"
KEY="$(grep -E '^ADMIN_KEY=' "$ENV" | cut -d= -f2- | tr -d '\r')"
[ -z "$KEY" ] && KEY="$(grep -E '^TELEGRAM_SETUP_KEY=' "$ENV" | cut -d= -f2- | tr -d '\r')"

# locate orders-db.json (default ~/hualing-leads, legacy ~/yuntech-leads)
DB="/home/ubuntu/hualing-leads/orders-db.json"
[ -f "$DB" ] || DB="/home/ubuntu/yuntech-leads/orders-db.json"
echo "db=$DB"

echo "== create + mark paid =="
OID=$(curl -fsS -X POST "$BASE/api/order" -H 'Content-Type: application/json' \
  -d '{"plan":"pro","edition":"pro","period":"monthly","amount":249,"contact":"e2e-SLA-test@internal","fingerprint":"","lang":"zh"}' \
  | sed -n 's/.*"order_id":"\([^"]*\)".*/\1/p')
echo "order_id=$OID"
curl -fsS -X POST "$BASE/api/admin/order-status" -H 'Content-Type: application/json' -H "x-setup-key: $KEY" \
  -d "{\"id\":\"$OID\",\"status\":\"paid\"}" >/dev/null && echo "marked paid"

echo "== backdate paid_at by 30 min =="
python3 - "$DB" "$OID" <<'PY'
import sys,json,datetime
db,oid=sys.argv[1],sys.argv[2]
d=json.load(open(db,encoding="utf-8"))
o=d["orders"][oid]
o["paid_at"]=(datetime.datetime.utcnow()-datetime.timedelta(minutes=30)).isoformat()+"Z"
json.dump(d,open(db,"w",encoding="utf-8"),ensure_ascii=False)
print("backdated paid_at=",o["paid_at"])
PY

echo "== SLA scan #1 (expect stale>=1) =="
curl -fsS "$BASE/api/admin/order-sla?key=$KEY"; echo
echo "== SLA scan #2 (expect stale=0, deduped) =="
curl -fsS "$BASE/api/admin/order-sla?key=$KEY"; echo

echo "== cleanup =="
curl -fsS -X POST "$BASE/api/admin/order-status" -H 'Content-Type: application/json' -H "x-setup-key: $KEY" \
  -d "{\"id\":\"$OID\",\"status\":\"cancelled\"}" >/dev/null && echo "cancelled $OID"
echo "== DONE =="
