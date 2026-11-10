#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ -f logs/supervisor.pid ] && kill -0 "$(cat logs/supervisor.pid)" 2>/dev/null; then echo "already running (supervisor $(cat logs/supervisor.pid))"; exit 0; fi
nohup setsid ./run_forever.sh > /dev/null 2>&1 &
sleep 1; echo "started supervisor $(cat logs/supervisor.pid)"
