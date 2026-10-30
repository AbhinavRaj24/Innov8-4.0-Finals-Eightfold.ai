#!/usr/bin/env bash
# Runs the agent against the live arena. Restarts it after a crash at most twice,
# mirroring the arena's own rule (a third crash ends the run). State on disk lets a
# restart resume where it stopped.
cd "$(dirname "$0")"
export ARENA_URL="${ARENA_URL:-https://innov8-battle-arena.onrender.com}"
: "${ARENA_KEY:?set ARENA_KEY in the environment before running (never hard-code it)}"
export PENALTY_FACTOR="${PENALTY_FACTOR:-0.05}"
export MARKET_OPEN="${MARKET_OPEN:-11:30}"          # "HH:MM", update when new timings are announced
mkdir -p logs state
for attempt in 1 2 3; do
  echo "$(date +%T) starting agent (attempt $attempt)" >> logs/stdout.log
  python3 -u agent.py >> logs/stdout.log 2>&1
  code=$?
  [ "$code" -eq 0 ] && { echo "$(date +%T) agent finished cleanly" >> logs/stdout.log; break; }
  echo "$(date +%T) agent exited with code $code" >> logs/stdout.log
  sleep 3
done
