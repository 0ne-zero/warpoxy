import json
import os
import pathlib
import sys
import argparse
from typing import List, Dict, Any
from jinja2 import Environment, FileSystemLoader
import subprocess
import logging
import socket
import time
import shutil
import random # Import the random module

# --- Constants ---
SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()
TEMPLATES_DIR = SCRIPT_DIR / "templates"
WARP_DIR = SCRIPT_DIR / "warp"
API_DIR = SCRIPT_DIR / "api"
COMPOSE_TEMPLATE_FILE = "docker-compose.yml.j2"
HAPROXY_TEMPLATE_FILE = "haproxy.cfg.j2"
COMPOSE_OUTPUT_FILE = SCRIPT_DIR / "docker-compose.yml"
HAPROXY_OUTPUT_FILE = SCRIPT_DIR / "haproxy.cfg"
CONFIG_FILE = SCRIPT_DIR / "config.json"
CURRENT_INDEX_FILE = SCRIPT_DIR / "current_index.json"

# --- Logger Setup ---
logger = logging.getLogger(__name__)

def setup_logging(verbose: bool) -> None:
    """Configure logging to the console."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s", stream=sys.stdout)
    logger.info("Logging initialized.")

def load_config() -> Dict[str, Any]:
    """Loads and validates the config.json file."""
    logger.debug("Attempting to load config from: %s", CONFIG_FILE)
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
        logger.debug("Config file found and parsed successfully.")
        required_keys = {
            "project_name": str, "num_tunnels": int, "country": str,
            "puid": int, "pgid": int, "timezone": str,
            "haproxy_host": str, "haproxy_port": int,
            "fastapi_host": str, "fastapi_port": int,
            "warp_socks_port": int, "warp_host_port_base": int,
            "country_endpoints": dict, "default_endpoint": str
        }
        for key, key_type in required_keys.items():
            if key not in config:
                raise ValueError(f"Missing required key in config.json: '{key}'")
            if not isinstance(config[key], key_type):
                raise ValueError(f"Key '{key}' has incorrect type. Expected {key_type}.")
        
        logger.debug("Config validation passed.")
        return config
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logger.error("Configuration error in '%s': %s", CONFIG_FILE, e)
        sys.exit(1)

def run_command(cmd: List[str], description: str) -> None:
    """Runs a command and streams its output in real-time."""
    logger.debug("Running command to %s: %s", description, " ".join(cmd))
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=SCRIPT_DIR,
            bufsize=1
        )
        if process.stdout:
            for line in iter(process.stdout.readline, ''):
                sys.stdout.write(line)
                sys.stdout.flush()
                logger.debug(line.strip())
        
        return_code = process.wait()
        if return_code != 0:
            raise subprocess.CalledProcessError(return_code, cmd)
    except FileNotFoundError:
        logger.error("❌ Command '%s' not found. Is Docker installed and in your PATH?", cmd[0])
        sys.exit(1)
    except subprocess.CalledProcessError:
        logger.error("❌ Failed to %s. See output above for details.", description)
        log_docker_ps()
        sys.exit(1)

def wait_for_healthy_containers(container_names: List[str], timeout: int = 30) -> bool:
    """Polls containers until they are all healthy."""
    logger.info("Waiting for WARP containers to become healthy...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Name}} {{.State.Health.Status}}", *container_names],
                check=True, capture_output=True, text=True
            )
            statuses = {line.split()[0].lstrip('/'): line.split()[1] for line in result.stdout.strip().split('\n')}
            healthy_count = list(statuses.values()).count("healthy")
            progress = healthy_count / len(container_names)
            bar = '█' * int(30 * progress) + '─' * (30 - int(30 * progress))
            sys.stdout.write(f"\rProgress: [{bar}] {healthy_count}/{len(container_names)} healthy")
            sys.stdout.flush()
            if healthy_count == len(container_names):
                sys.stdout.write("\n")
                return True
        except (subprocess.CalledProcessError, IndexError) as e:
            logger.debug("Could not inspect containers yet (they may be starting). Error: %s", e)
        time.sleep(5)
    sys.stdout.write("\n")
    logger.error("Timeout reached while waiting for containers to become healthy.")
    return False

def log_docker_ps() -> None:
    """Logs 'docker ps -a' for debugging failures."""
    logger.info("Dumping container status ('docker ps -a')...")
    try:
        result = subprocess.run(["docker", "ps", "-a"], check=True, capture_output=True, text=True)
        logger.debug("--- DOCKER PS -A ---\n%s\n--------------------", result.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error("Could not execute 'docker ps -a': %s", e)

def generate_files_from_templates(config: Dict[str, Any]) -> None:
    """Generates all configuration files from Jinja2 templates."""
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)

    # --- Endpoint Diversification Logic ---
    country_code = config["country"].upper()
    
    if country_code == "ALL":
        logger.info("Country is 'ALL'. Creating a diverse, random pool from all available endpoints.")
        # Flatten all lists of endpoints into one master list
        all_endpoints = [
            endpoint for country_list in config["country_endpoints"].values() for endpoint in country_list
        ]
        
        num_tunnels = config["num_tunnels"]
        num_unique_endpoints = len(all_endpoints)

        if num_tunnels > num_unique_endpoints:
            logger.warning(
                "Requested %d tunnels, but only %d unique endpoints are available. "
                "Endpoints will be reused. Shuffling for maximum diversity.",
                num_tunnels, num_unique_endpoints
            )
            random.shuffle(all_endpoints)
            endpoint_pool = all_endpoints
        else:
            # We have enough, so we can guarantee uniqueness by sampling without replacement.
            logger.info("Selecting %d random unique endpoints from a pool of %d.", num_tunnels, num_unique_endpoints)
            endpoint_pool = random.sample(all_endpoints, k=num_tunnels)
    else:
        # This is the original, working logic for a specific country
        endpoint_pool = config["country_endpoints"].get(country_code, [config["default_endpoint"]])
        if not isinstance(endpoint_pool, list):
            endpoint_pool = [endpoint_pool]
        logger.info("Using endpoints for country '%s': %s", country_code, endpoint_pool)

    # --- Service Context Generation ---
    warp_services = {}
    for i in range(1, config["num_tunnels"] + 1):
        # This single line of assignment logic now works for all cases.
        specific_endpoint = endpoint_pool[(i - 1) % len(endpoint_pool)]
        logger.debug("Assigning endpoint hostname %s to warp%d", specific_endpoint, i)
        
        warp_services[f"warp{i}"] = {
            "build": {"context": SCRIPT_DIR, "dockerfile": WARP_DIR / "Dockerfile.warp"},
            "container_name": f"{config['project_name']}_warp{i}",
            "restart": "always",
            "host_port": config["warp_host_port_base"] + i,
            "environment": {
                "ENDPOINT": specific_endpoint,
                "SOCKS5_PORT": str(config["warp_socks_port"]),
                "PUID": str(config["puid"]),
                "PGID": str(config["pgid"]),
                "TZ": config["timezone"],
            },
            "healthcheck": {
                "test": ["CMD-SHELL", f"curl -x socks5h://127.0.0.1:{config['warp_socks_port']} --silent --fail --connect-timeout 3 https://1.1.1.1 || exit 1"],
                "interval": "30s", "timeout": "10s", "retries": "3", "start_period": "180s"
            },
            "volumes": [f"{WARP_DIR / f'warp{i}_config'}:/config"]
        }

    api_service = {
        "build": {"context": SCRIPT_DIR, "dockerfile": API_DIR / "Dockerfile.api"},
        "container_name": f"{config['project_name']}_api",
        "restart": "always"
    }

    # --- File Generation ---
    try:
        compose_template = env.get_template(COMPOSE_TEMPLATE_FILE)
        compose_content = compose_template.render(
            warp_services=warp_services, api_service=api_service, SCRIPT_DIR=SCRIPT_DIR, **config
        )
        with open(COMPOSE_OUTPUT_FILE, "w") as f:
            f.write(compose_content)
        logger.debug("Successfully wrote %s", COMPOSE_OUTPUT_FILE)

        backends = [{"name": f"warp{i}", "port": config['warp_socks_port'], "weight": 100 if (i - 1) == 0 else 1} for i in range(1, config["num_tunnels"] + 1)]
        haproxy_template = env.get_template(HAPROXY_TEMPLATE_FILE)
        haproxy_content = haproxy_template.render(backends=backends, **config)
        with open(HAPROXY_OUTPUT_FILE, "w") as f:
            f.write(haproxy_content)
        logger.debug("Successfully wrote %s", HAPROXY_OUTPUT_FILE)
    except Exception as e:
        logger.error("Failed to generate configuration files: %s", e)
        sys.exit(1)

def main() -> None:
    """Main orchestration function for the setup process."""
    parser = argparse.ArgumentParser(description="Setup script for the WARPoxy project.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging.")
    args = parser.parse_args()
    setup_logging(args.verbose)
    
    try:
        logger.info("--- 🚀 Starting WARPoxy Setup 🚀 ---")

        logger.info("[STEP 1/5] Loading configuration...")
        config = load_config()
        logger.info("✅ Configuration loaded: %d tunnels in %s.", config["num_tunnels"], config["country"])
        os.environ['COMPOSE_PROJECT_NAME'] = config["project_name"]

        logger.info("[STEP 2/5] Preparing environment and generating config files...")
        subprocess.run(["docker", "pull", "haproxy:2.8"], capture_output=True, check=True)
        
        for i in range(1, config["num_tunnels"] + 1):
            (WARP_DIR / f"warp{i}_config").mkdir(exist_ok=True)
        
        if not CURRENT_INDEX_FILE.exists():
            logger.info("Initializing state file: %s", CURRENT_INDEX_FILE.name)
            with open(CURRENT_INDEX_FILE, "w") as f:
                json.dump({"index": 0}, f)

        generate_files_from_templates(config)
        logger.info("✅ Generated %s and %s.", COMPOSE_OUTPUT_FILE.name, HAPROXY_OUTPUT_FILE.name)

        logger.info("[STEP 3/5] Building container images...")
        build_cmd = ["docker-compose", "-f", str(COMPOSE_OUTPUT_FILE), "build"]
        run_command(build_cmd, "build images")
        logger.info("✅ Images built successfully.")

        logger.info("[STEP 4/5] Starting services...")
        up_cmd = ["docker-compose", "-f", str(COMPOSE_OUTPUT_FILE), "up", "-d", "--remove-orphans"]
        run_command(up_cmd, "start services")
        logger.info("✅ Services started successfully.")

        logger.info("[STEP 5/5] Verifying service health...")
        warp_container_names = [f"{config['project_name']}_warp{i}" for i in range(1, config["num_tunnels"] + 1)]
        if not wait_for_healthy_containers(warp_container_names):
            log_docker_ps()
            raise RuntimeError("One or more WARP containers failed to become healthy.")

        if socket.socket().connect_ex((config["haproxy_host"], config["haproxy_port"])) != 0:
            raise RuntimeError(f"Verification failed: HAProxy is not responding.")
        logger.info("✅ Health verification complete.")

        logger.info("--- 🎉 Setup Complete 🎉 ---")
        logger.info("Proxy is available at: %s:%d (SOCKS5)", config["haproxy_host"], config["haproxy_port"])
        logger.info("API is available at: http://%s:%d", config["fastapi_host"], config["fastapi_port"])

    except Exception as e:
        logger.error("❌ An unexpected error occurred during setup: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()