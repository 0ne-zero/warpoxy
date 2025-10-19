#!/bin/sh

# Exit immediately if a command exits with a non-zero status.
set -e

# --- Environment and File Definitions ---
CONFIG_DIR="/config"
ACCOUNT_FILE="${CONFIG_DIR}/wgcf-account.toml"
PROFILE_FILE="${CONFIG_DIR}/wgcf-profile.conf"
LOG_FILE="${CONFIG_DIR}/wireproxy.log"

# Default PUID/PGID to 911 if not set
PUID=${PUID:-911}
PGID=${PGID:-911}

# --- User and Permission Setup ---
echo "Setting up user and permissions..."
# Create a group and user, ignoring errors if they already exist
addgroup -g "${PGID}" appgroup 2>/dev/null || true
adduser -u "${PUID}" -G appgroup -h "${CONFIG_DIR}" -s /bin/sh -D appuser 2>/dev/null || true

# Ensure the config directory is owned by the app user
chown -R "${PUID}:${PGID}" "${CONFIG_DIR}"

# --- One-Time WARP Setup ---
cd "${CONFIG_DIR}"

# Register Cloudflare WARP account if not already done
if [ ! -f "${ACCOUNT_FILE}" ]; then
    echo "Account file not found. Registering new Cloudflare WARP account..."
    # Use su-exec to run as the correct user from the start
    su-exec appuser wgcf register --accept-tos --config "${ACCOUNT_FILE}"
fi

# Generate WireGuard profile if not already done
if [ ! -f "${PROFILE_FILE}" ]; then
    echo "WireGuard profile not found. Generating..."
    su-exec appuser wgcf generate --config "${ACCOUNT_FILE}" --profile "${PROFILE_FILE}"
fi

# --- Configure WireProxy directly in the profile ---
echo "Ensuring [Socks5] configuration exists in ${PROFILE_FILE}..."

# Use grep -q to silently check for the presence of the section header.
# The `|| true` prevents the script from exiting if grep doesn't find a match.
if ! grep -q "\[Socks5\]" "${PROFILE_FILE}"; then
    echo "'[Socks5]' section not found. Appending it to the profile..."
    # Use '>>' to append the new section to the end of the file.
    # Add a newline before the section for clean formatting.
    printf "\n[Socks5]\nBindAddress = 0.0.0.0:%s\n" "${SOCKS5_PORT}" >> "${PROFILE_FILE}"
    echo "Section appended."
else
    echo "'[Socks5]' section already exists. No changes made."
fi

# --- Start WireProxy ---
echo "Starting wireproxy using the profile at ${PROFILE_FILE}..."
# Use 'su-exec' for proper signal handling as a non-root user.
# The command now correctly uses the wgcf-profile.conf file.
exec su-exec appuser wireproxy -c "${PROFILE_FILE}" 2>&1 | tee "${LOG_FILE}"