#!/usr/bin/env bash
# Idempotent "make sure the bot is up". Safe to run from any agent turn / routine (e.g. after a box reboot).
# Never creates or removes KILL. Prints one status line; never prints credentials.
cd "$(dirname "$0")"
if [ -f KILL ]; then echo "KILL present: bot intentionally stopped; not starting"; exit 0; fi
HB=logs/kalshi_heartbeat.json
P=$(cat logs/kalshi_supervisor.pid 2>/dev/null)
if [ -n "$P" ] && kill -0 "$P" 2>/dev/null && tr '\0' ' ' < /proc/"$P"/cmdline 2>/dev/null | grep -q run_kalshi.sh; then
  age=$(( $(date +%s) - $(stat -c %Y "$HB" 2>/dev/null || echo 0) ))
  echo "running (supervisor $P, heartbeat ${age}s old; watchdog restarts the bot if >120s)"; exit 0
fi
./start_kalshi.sh
