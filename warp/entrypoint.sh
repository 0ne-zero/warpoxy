#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Environment and File Definitions ---
CONFIG_DIR="/config"
ACCOUNT_FILE="${CONFIG_DIR}/wgcf-account.toml"
PROFILE_FILE="${CONFIG_DIR}/wgcf-profile.conf"
LOG_FILE="${CONFIG_DIR}/wireproxy.log"

# --- One-Time WARP Setup ---
cd "${CONFIG_DIR}"

# Register Cloudflare WARP account if not already done
if [ ! -f "${ACCOUNT_FILE}" ]; then
    echo "Account file not found. Registering new Cloudflare WARP account..."
    wgcf register --accept-tos --config "${ACCOUNT_FILE}"
fi

# Generate WireGuard profile if not already done
if [ ! -f "${PROFILE_FILE}" ]; then
    echo "WireGuard profile not found. Generating..."
    wgcf generate --config "${ACCOUNT_FILE}" --profile "${PROFILE_FILE}"
fi

# --- DYNAMIC ENDPOINT REPLACEMENT ---
# This is the critical step to ensure IP diversification.
# The ENDPOINT variable is passed in from docker-compose.

# For debugging
export ENDPOINT="engage.cloudflareclient.com:2408" 


if [ -n "${ENDPOINT}" ]; then
    echo "Found ENDPOINT variable. Replacing endpoint in ${PROFILE_FILE}..."
    # Use sed to find the line starting with "Endpoint =" and replace it.
    # The `|` is used as a separator to avoid issues with slashes in the endpoint.
    sed -i "s|Endpoint =.*|Endpoint = ${ENDPOINT}|" "${PROFILE_FILE}"
    echo "Endpoint successfully replaced with: ${ENDPOINT}"
else
    echo "ENDPOINT variable not set. Using default endpoint from profile."
fi

# --- Configure SOCKS5 Proxy directly in the profile ---
echo "Ensuring [Socks5] configuration exists in ${PROFILE_FILE}..."

# Check if the section header is missing.
if ! grep -q "\[Socks5\]" "${PROFILE_FILE}"; then
    echo "'[Socks5]' section not found. Appending it to the profile..."
    # Append the new section to the end of the file.
    printf "\n[Socks5]\nBindAddress = 0.0.0.0:%s\n" "${SOCKS5_PORT}" >> "${PROFILE_FILE}"
    echo "Section appended."
else
    echo "'[Socks5]' section already exists. No changes made."
fi

# --- Start WireProxy ---
echo "Starting wireproxy using the profile at ${PROFILE_FILE}..."
# 'exec' ensures that wireproxy becomes the main process (PID 1).
exec wireproxy -c "${PROFILE_FILE}" 2>&1 | tee "${LOG_FILE}"