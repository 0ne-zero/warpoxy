import json
import os
import pathlib
import sys
import requests
import logging
from typing import Dict, Any, List

# --- Constants ---
SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.json"
CLOUDFLARE_TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

# --- Logging Configuration ---
# A simple formatter for clean, professional output
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)


def _print_status(message: str, success: bool):
    """Helper function to print formatted status messages."""
    symbol = "✅" if success else "❌"
    log_func = logger.info if success else logger.error
    log_func(f"{symbol} {message}")


def load_config() -> Dict[str, Any]:
    """Loads the project configuration from config.json."""
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Configuration file not found at {CONFIG_FILE}")
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def check_haproxy(config: Dict[str, Any]) -> bool:
    """
    Performs a deep health check on the HAProxy service.

    It sends a request through the proxy to Cloudflare's trace endpoint
    and verifies that the connection is routed through the WARP network.
    """
    logger.info("--- Checking HAProxy Service ---")
    host = config.get("haproxy_host", "127.0.0.1")
    port = config.get("haproxy_port")
    proxy_url = f"socks5h://{host}:{port}"
    
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        response = requests.get(CLOUDFLARE_TRACE_URL, proxies=proxies, timeout=15)
        response.raise_for_status()
        
        trace_output = response.text
        if "warp=on" in trace_output:
            _print_status("HAProxy is correctly routing traffic through WARP.", True)
            return True
        else:
            _print_status("HAProxy is responding, but not routing through WARP.", False)
            logger.debug("Trace output:\n%s", trace_output)
            return False
            
    except requests.exceptions.ProxyError:
        _print_status(f"Failed to connect to HAProxy at {proxy_url}.", False)
        return False
    except requests.exceptions.RequestException as e:
        _print_status(f"Request through proxy failed: {e}", False)
        return False


def check_api(config: Dict[str, Any]) -> bool:
    """
    Performs a full operational health check on the API service.

    Tests the /list, /current, and /rotate endpoints to ensure the API
    can read state, modify it, and interact with Docker.
    """
    logger.info("--- Checking API Service ---")
    host = config.get("fastapi_host", "127.0.0.1")
    port = config.get("fastapi_port")
    base_url = f"http://{host}:{port}"
    
    try:
        # Test 1: /list endpoint
        response_list = requests.get(f"{base_url}/list", timeout=10)
        response_list.raise_for_status()
        tunnels = response_list.json()
        if isinstance(tunnels, list) and len(tunnels) > 0:
            _print_status("API endpoint '/list' is operational.", True)
        else:
            _print_status("API endpoint '/list' returned invalid data.", False)
            return False

        # Test 2: /current endpoint (before rotation)
        response_current_before = requests.get(f"{base_url}/current", timeout=10)
        response_current_before.raise_for_status()
        current_before = response_current_before.json()
        if current_before.get("isActive"):
            _print_status("API endpoint '/current' is operational.", True)
            original_active_tunnel = current_before['name']
        else:
            _print_status("API endpoint '/current' returned invalid data.", False)
            return False

        # Test 3: /rotate endpoint
        response_rotate = requests.post(f"{base_url}/rotate", timeout=15)
        response_rotate.raise_for_status()
        if response_rotate.json().get("status") == "rotated":
            _print_status("API endpoint '/rotate' responded successfully.", True)
        else:
            _print_status("API endpoint '/rotate' returned an unexpected status.", False)
            return False

        # Test 4: Verify rotation was successful
        response_current_after = requests.get(f"{base_url}/current", timeout=10)
        response_current_after.raise_for_status()
        current_after = response_current_after.json()
        new_active_tunnel = current_after.get('name')

        if new_active_tunnel and new_active_tunnel != original_active_tunnel:
            _print_status(f"API rotation successfully verified (rotated from {original_active_tunnel} to {new_active_tunnel}).", True)
        else:
            _print_status("API rotation failed verification.", False)
            return False

        return True

    except requests.exceptions.ConnectionError:
        _print_status(f"Failed to connect to API at {base_url}.", False)
        return False
    except requests.exceptions.RequestException as e:
        _print_status(f"An error occurred while testing the API: {e}", False)
        return False


def main():
    """Main function to orchestrate the health checks."""
    logger.info("🚀 Starting WARPoxy Health Check 🚀")
    failures = []
    
    try:
        config = load_config()
        
        if not check_haproxy(config):
            failures.append("HAProxy Service")
            
        if not check_api(config):
            failures.append("API Service")

    except FileNotFoundError as e:
        _print_status(str(e), False)
        failures.append("Configuration")
    except Exception as e:
        logger.error("An unexpected error occurred: %s", e)
        failures.append("Unexpected Error")

    print("-" * 40)
    if not failures:
        logger.info("🎉 All systems operational. Health check passed. 🎉")
        sys.exit(0)
    else:
        logger.error("🔥 Health check FAILED. The following components reported errors: %s", ", ".join(failures))
        sys.exit(1)


if __name__ == "__main__":
    main()