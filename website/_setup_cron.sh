#!/usr/bin/env bash
set -eu
cp /home/ubuntu/yuntech/scripts/growth-snapshot.sh /home/ubuntu/growth-snapshot.sh
cp /home/ubuntu/yuntech/scripts/daily-post.sh /home/ubuntu/daily-post.sh
chmod +x /home/ubuntu/growth-snapshot.sh /home/ubuntu/daily-post.sh

crontab -l 2>/dev/null | grep -vE 'growth-snapshot\.sh|daily-post\.sh' > /tmp/cron.new || true
echo '20 3 * * * /home/ubuntu/growth-snapshot.sh' >> /tmp/cron.new
echo '0 11 * * * /home/ubuntu/daily-post.sh' >> /tmp/cron.new
crontab /tmp/cron.new
rm -f /tmp/cron.new
echo '---CRON---'
crontab -l

echo '---SEED GROWTH---'
/home/ubuntu/growth-snapshot.sh
tail -1 /home/ubuntu/growth-snapshot.log
