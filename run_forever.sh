#!/usr/bin/env bash
# Supervisor: restarts the paper bot if it crashes.
cd "$(dirname "$0")"
echo $$ > logs/supervisor.pid
while true; do
  python3 -u bot.py run >> logs/stdout.log 2>&1
  echo "$(date '+%F %T %Z') bot exited with $?; restarting in 10s" >> logs/supervisor.log
  sleep 10
done
