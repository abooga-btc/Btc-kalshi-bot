#!/usr/bin/env bash
# Supervisor + watchdog for the live Kalshi bot.
#  - restarts the bot if it exits/crashes
#  - restarts it if logs/kalshi_heartbeat.json is not refreshed for > STALE seconds (hung process)
#  - checks every CHECK seconds; detects its own pauses (box hibernation) and grants a grace period
#    instead of killing a bot that was simply frozen together with the whole box.
# Credentials (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY) are inherited from the environment only.
cd "$(dirname "$0")"
echo $$ > logs/kalshi_supervisor.pid
HB=logs/kalshi_heartbeat.json; STALE=120; CHECK=15; GRACE_GAP=60
slog(){ echo "$(date '+%F %T %Z') $*" >> logs/kalshi_supervisor.log; }
child=""
trap 'slog "supervisor stopping (signal)"; [ -n "$child" ] && kill "$child" 2>/dev/null; exit 0' TERM INT HUP
slog "supervisor $$ started (watchdog: heartbeat stale > ${STALE}s, check every ${CHECK}s)"
while true; do
  python3 -u kalshi_bot.py run >> logs/kalshi_stdout.log 2>&1 &
  child=$!; started=$(date +%s); last=$started
  slog "bot started pid $child"
  while kill -0 "$child" 2>/dev/null; do
    sleep "$CHECK" & wait $!
    now=$(date +%s)
    if (( now - last > GRACE_GAP )); then
      slog "supervisor itself was paused $((now - last))s (box suspended/hibernated); grace period before judging heartbeat"
      started=$now
    fi
    last=$now
    hb=$(stat -c %Y "$HB" 2>/dev/null || echo 0)
    ref=$(( hb > started ? hb : started ))
    if (( now - ref > STALE )); then
      slog "WATCHDOG: heartbeat stale $((now - hb))s (> ${STALE}s); restarting bot pid $child"
      kill -TERM "$child" 2>/dev/null
      for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$child" 2>/dev/null || break; sleep 1; done
      kill -KILL "$child" 2>/dev/null
      break
    fi
  done
  wait "$child"; rc=$?
  slog "bot pid $child exited with $rc; restarting in 10s"
  child=""
  sleep 10
done
