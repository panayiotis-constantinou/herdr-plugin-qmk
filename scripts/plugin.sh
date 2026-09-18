#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
state_dir=${HERDR_PLUGIN_STATE_DIR:?HERDR_PLUGIN_STATE_DIR is missing}
pidfile="$state_dir/qmk-herdr.pid"
logfile="$state_dir/qmk-herdr.log"
bridge="$root/scripts/bridge.py"
python=${HERDR_QMK_PYTHON:-python3}
mkdir -p "$state_dir"

running() {
  [ -s "$pidfile" ] || return 1
  pid=$(cat "$pidfile")
  case "$pid" in *[!0-9]* | '') return 1 ;; esac
  kill -0 "$pid" 2>/dev/null || return 1
  ps -p "$pid" -o command= 2>/dev/null | grep -F "$bridge" >/dev/null
}

stop() {
  if running; then
    kill "$pid"
    i=0
    while kill -0 "$pid" 2>/dev/null && [ "$i" -lt 20 ]; do
      sleep 0.1
      i=$((i + 1))
    done
    kill -9 "$pid" 2>/dev/null || true
  fi
  rm -f "$pidfile"
}

start() {
  [ -f "$bridge" ] || {
    echo "qmk-herdr: missing $bridge; reinstall the plugin" >&2
    exit 1
  }
  running && {
    echo "qmk-herdr is running (pid $pid)"
    return
  }
  rm -f "$pidfile"
  : >"$logfile"
  port_file="${HERDR_PLUGIN_CONFIG_DIR:?HERDR_PLUGIN_CONFIG_DIR is missing}/midi-port"
  if [ -s "$port_file" ]; then
    nohup "$python" "$bridge" "$(cat "$port_file")" >>"$logfile" 2>&1 &
  else
    nohup "$python" "$bridge" >>"$logfile" 2>&1 &
  fi
  echo $! >"$pidfile"
  sleep 0.2
  running || {
    cat "$logfile" >&2
    rm -f "$pidfile"
    exit 1
  }
  echo "qmk-herdr started (pid $pid)"
}

case "${1:-}" in
restart)
  stop
  start
  ;;
stop)
  stop
  echo "qmk-herdr stopped"
  ;;
status)
  if running; then
    echo "qmk-herdr is running (pid $pid)"
  else
    echo "qmk-herdr is stopped"
    [ ! -s "$logfile" ] || tail -n 20 "$logfile"
    exit 1
  fi
  ;;
*)
  echo "usage: $0 {restart|stop|status}" >&2
  exit 2
  ;;
esac
