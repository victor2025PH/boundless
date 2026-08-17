#!/usr/bin/env bash
# chatx_gate.sh - inspect / toggle the ChatX AI-gateway eligibility gate on the VPS.
#
# The gate is AI_GATEWAY_REQUIRE_CLAIM in yuntech/.env.local. Gateway code treats it as:
#   value == "0"        -> gate OPEN  (any fingerprint gets a device token = install-and-use)
#   missing or non-"0"  -> gate CLOSED (only fingerprints in the trial-claim ledger get tokens)
# The same token then unlocks the Telegram credential pool, so this one switch governs both
# AI-character white-listing AND TG-credential white-listing.
#
# Usage (run FROM the VPS, or via chatx_gate.ps1 which scps+runs this):
#   bash chatx_gate.sh status   # read current gate + machine/claim/seat/char water-lines
#   bash chatx_gate.sh on       # CLOSE the gate (require claim)  -> new installs must claim first
#   bash chatx_gate.sh off      # OPEN the gate (install-and-use) -> current production default
set -u
APP=/home/ubuntu/yuntech
ENVF="$APP/.env.local"
cd "$APP" || { echo "NO_APP_DIR $APP"; exit 1; }
ACTION="${1:-status}"

show_status() {
  local REQ
  REQ=$(grep -E '^AI_GATEWAY_REQUIRE_CLAIM=' "$ENVF" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d ' \r')
  echo "AI_GATEWAY_REQUIRE_CLAIM=${REQ:-<missing>}"
  if [ "$REQ" = "0" ]; then
    echo "GATE=OPEN   (install-and-use; anyone can get AI + TG creds; white-listing OFF)"
  else
    echo "GATE=CLOSED (claim required before AI + TG creds are issued)"
  fi
  node - <<'NODE' 2>&1
const os=require('os'),path=require('path'),fs=require('fs');
let dd=path.join(os.homedir(),'hualing-leads'); if(!fs.existsSync(dd)) dd=path.join(os.homedir(),'yuntech-leads');
let db; try{ db=new (require('better-sqlite3'))(path.join(dd,'ai-gateway.db'),{readonly:true}); }catch(e){ console.log('quota db unavailable:',e.message); process.exit(0); }
const today=new Date().toISOString().slice(0,10);
const g=db.prepare("SELECT used FROM gw_quota WHERE day=? AND mid='__GLOBAL__'").get(today);
const m=db.prepare("SELECT COUNT(*) n,COALESCE(SUM(used),0) s FROM gw_quota WHERE day=? AND mid<>'__GLOBAL__'").get(today);
const mids=db.prepare("SELECT DISTINCT mid FROM gw_quota WHERE mid<>'__GLOBAL__'").all().map(r=>r.mid);
let tg=0; try{ tg=db.prepare("SELECT COUNT(*) n FROM tg_cred_assign").get().n; }catch(e){}
const claims=new Set();
const cp=path.join(dd,'trial-claims.json');
if(fs.existsSync(cp)){ const j=JSON.parse(fs.readFileSync(cp,'utf8')); for(const c of Object.values(j.byId||{})) claims.add(String(c.fingerprint||'').toUpperCase()); }
const noClaim=mids.filter(x=>!claims.has(x));
console.log('--- water-lines ---');
console.log('today_global_chars   '+((g&&g.used)||0)+' / 2000000');
console.log('today_active_machines '+m.n+'  (chars '+m.s+')');
console.log('alltime_machines     '+mids.length+'   claims_on_file '+claims.size);
console.log('machines_WITHOUT_claim '+noClaim.length+(noClaim.length?('  ['+noClaim.join(', ')+']'):''));
console.log('tg_pool_seats_used   '+tg+' / 430');
NODE
}

case "$ACTION" in
  status) show_status ;;
  on|off)
    if [ "$ACTION" = "on" ]; then V=1; else V=0; fi
    if grep -qE '^AI_GATEWAY_REQUIRE_CLAIM=' "$ENVF"; then
      sed -i -E "s/^AI_GATEWAY_REQUIRE_CLAIM=.*/AI_GATEWAY_REQUIRE_CLAIM=$V/" "$ENVF"
    else
      printf '\nAI_GATEWAY_REQUIRE_CLAIM=%s\n' "$V" >> "$ENVF"
    fi
    pm2 restart yuntech --update-env >/dev/null 2>&1
    sleep 4
    echo "applied AI_GATEWAY_REQUIRE_CLAIM=$V; pm2 restarted"
    show_status
    ;;
  *) echo "unknown action '$ACTION' (use: status|on|off)"; exit 2 ;;
esac
