#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
bridge="$root/scripts/bridge.py"
: "${HERDR_PLUGIN_STATE_DIR:?HERDR_PLUGIN_STATE_DIR is missing}"
config_dir=${HERDR_PLUGIN_CONFIG_DIR:?HERDR_PLUGIN_CONFIG_DIR is missing}
python=${HERDR_QMK_PYTHON:-python3}
if [ -s "$config_dir/python" ]; then
  python=$(cat "$config_dir/python")
fi
if [ -z "${TYPESAFE_API_KEY:-}" ] && [ -s "$config_dir/typesafe-api-key" ]; then
  TYPESAFE_API_KEY=$(cat "$config_dir/typesafe-api-key")
  export TYPESAFE_API_KEY
fi
[ -f "$bridge" ] || {
  echo "qmk-herdr: missing $bridge; reinstall the plugin" >&2
  exit 1
}

# The bridge owns its lifecycle: it holds a lock in the state dir, so one
# runs per state dir whichever plugin root started it.
case "${1:-}" in
start | restart | stop | status)
  exec "$python" "$bridge" "$1"
  ;;
*)
  echo "usage: $0 {start|restart|stop|status}" >&2
  exit 2
  ;;
esac
