#!/bin/sh
set -eu

# ---- Config via envs (defaults are sane) ----
SOCKS5_PORT="${SOCKS5_PORT:-1080}"         # Host will bind to 127.0.0.1:warp_host_port in compose
ENDPOINT="${ENDPOINT:-}"                    # e.g. "engage.cloudflareclient.com:2408" or POP hostname
WARP_KEY="${WARP_KEY:-}"                    # Warp device key (optional)
SCAN="${SCAN:-false}"                       # "true" to enable endpoint scanning
CFON="${CFON:-false}"                       # "true" to enable Psiphon
COUNTRY="${COUNTRY:-DE}"                    # Psiphon country if CFON=true (ISO-2)
DNS_ADDR="${DNS_ADDR:-1.1.1.1}"             # Custom DNS if desired
CACHE_DIR="${CACHE_DIR:-/config/.cache}"    # Where warp-plus stores generated profiles
VERBOSE="${VERBOSE:-false}"                 # "true" to add --verbose

# ---- Build CLI args ----
ARGS="--bind 0.0.0.0:${SOCKS5_PORT} --dns ${DNS_ADDR} --cache-dir ${CACHE_DIR}"
[ -n "${ENDPOINT}" ] && ARGS="${ARGS} --endpoint ${ENDPOINT}"
[ -n "${WARP_KEY}" ] && ARGS="${ARGS} --key ${WARP_KEY}"
[ "${SCAN}" = "true" ] && ARGS="${ARGS} --scan"
if [ "${CFON}" = "true" ]; then
  ARGS="${ARGS} --cfon --country ${COUNTRY}"
fi
[ "${VERBOSE}" = "true" ] && ARGS="${ARGS} --verbose"

echo "[warp-plus] starting with args: ${ARGS}"
exec /usr/local/bin/warp-plus ${ARGS}

# warp-plus --bind 0.0.0.0:1080 --dns 1.1.1.1 --scan --cfon --country DE --verbose