#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ -f KILL ]; then echo "KILL file present; remove it (rm KILL) and run ./kalshi_bot.py resume-live first"; exit 1; fi
if [ -f logs/kalshi_supervisor.pid ]; then
  P=$(cat logs/kalshi_supervisor.pid)
  if kill -0 "$P" 2>/dev/null && tr '\0' ' ' < /proc/"$P"/cmdline 2>/dev/null | grep -q run_kalshi.sh; then
    echo "already running (supervisor $P)"; exit 0
  fi
fi
if [ -z "${KALSHI_API_KEY_ID:-}" ] || [ -z "${KALSHI_PRIVATE_KEY:-}" ]; then
  echo "Kalshi credentials are not in this shell's environment; not starting (live would be halted)"; exit 2
fi
nohup setsid ./run_kalshi.sh > /dev/null 2>&1 &
sleep 1; echo "started supervisor $(cat logs/kalshi_supervisor.pid)"
