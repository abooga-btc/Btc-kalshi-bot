#!/usr/bin/env bash
# Stop: trip the KILL switch so the bot cancels its resting live orders, then stop the supervisor + bot.
cd "$(dirname "$0")"
touch KILL
for i in $(seq 1 15); do grep -q "KILL file present" logs/kalshi_bot.log 2>/dev/null && tail -1 logs/kalshi_bot.log | grep -q KILL && break; sleep 1; done
if [ -f logs/kalshi_supervisor.pid ]; then PID=$(cat logs/kalshi_supervisor.pid); kill -- -"$PID" 2>/dev/null || kill "$PID" 2>/dev/null; pkill -f "kalshi_bot.py run"; rm -f logs/kalshi_supervisor.pid; echo stopped; else echo "not running"; fi
echo "KILL file left in place. To restart live: rm KILL && ./kalshi_bot.py resume-live && ./start_kalshi.sh"
