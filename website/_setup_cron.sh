#!/usr/bin/env bash
set -eu
cp /home/ubuntu/yuntech/scripts/growth-snapshot.sh /home/ubuntu/growth-snapshot.sh
cp /home/ubuntu/yuntech/scripts/daily-post.sh /home/ubuntu/daily-post.sh
chmod +x /home/ubuntu/growth-snapshot.sh /home/ubuntu/daily-post.sh

# stripe 对账巡检需要管理口令：ADMIN_KEY 优先，回退 TELEGRAM_SETUP_KEY（与 requireAdmin 口径一致）
ADMIN_KEY=$(grep -E '^ADMIN_KEY=' /home/ubuntu/yuntech/.env.local 2>/dev/null | cut -d= -f2- | tr -d '\r"' || true)
[ -n "${ADMIN_KEY:-}" ] || ADMIN_KEY=$(grep -E '^TELEGRAM_SETUP_KEY=' /home/ubuntu/yuntech/.env.local 2>/dev/null | cut -d= -f2- | tr -d '\r"' || true)

crontab -l 2>/dev/null | grep -vE 'growth-snapshot\.sh|daily-post\.sh|stripe-reconcile' > /tmp/cron.new || true
echo '20 3 * * * /home/ubuntu/growth-snapshot.sh' >> /tmp/cron.new
echo '0 11 * * * /home/ubuntu/daily-post.sh' >> /tmp/cron.new
# 卡支付双重对账兜底：每日 04:10 拉 Stripe 已付 session 与订单库比对，webhook 漏单自动补账
if [ -n "${ADMIN_KEY:-}" ]; then
  echo "10 4 * * * curl -fsS -m 120 \"http://127.0.0.1:3000/api/admin/stripe-reconcile?key=$ADMIN_KEY&hours=25\" >> /home/ubuntu/stripe-reconcile.log 2>&1" >> /tmp/cron.new
fi
crontab /tmp/cron.new
rm -f /tmp/cron.new
echo '---CRON---'
crontab -l

echo '---SEED GROWTH---'
/home/ubuntu/growth-snapshot.sh
tail -1 /home/ubuntu/growth-snapshot.log
