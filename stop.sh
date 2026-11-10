#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -f logs/supervisor.pid ] && kill -- -"$(ps -o pgid= $(cat logs/supervisor.pid) | tr -d ' ')" 2>/dev/null; rm -f logs/supervisor.pid; echo stopped
