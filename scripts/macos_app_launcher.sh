#!/usr/bin/env bash
set -u

app_root="$(cd "$(dirname "$0")/.." && pwd)"
app_bin="$app_root/MacOS/AgentLoop.bin"
quit_marker="${TMPDIR:-/tmp}/agentloop-quit-$$"
quit_timeout="${AGENTLOOP_QUIT_TIMEOUT_SECONDS:-5}"
kill_grace="${AGENTLOOP_KILL_GRACE_SECONDS:-1}"
child_pid=""

cleanup() {
  rm -f "$quit_marker"
}

forward_signal() {
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -TERM "$child_pid" 2>/dev/null || true
  fi
}

trap cleanup EXIT
trap forward_signal INT TERM
rm -f "$quit_marker"

AGENTLOOP_QUIT_MARKER="$quit_marker" "$app_bin" "$@" &
child_pid=$!
quit_started=-1

while kill -0 "$child_pid" 2>/dev/null; do
  if [[ -e "$quit_marker" ]]; then
    if [[ "$quit_started" -lt 0 ]]; then
      quit_started="$SECONDS"
    elif (( SECONDS - quit_started >= quit_timeout )); then
      kill -TERM "$child_pid" 2>/dev/null || true
      sleep "$kill_grace"
      kill -KILL "$child_pid" 2>/dev/null || true
      break
    fi
  fi
  sleep 0.2
done

wait "$child_pid" 2>/dev/null
status=$?
[[ -e "$quit_marker" ]] && exit 0
exit "$status"
