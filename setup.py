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

# --- Constants ---
SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()
TEMPLATES_DIR = SCRIPT_DIR / "templates"
WARP_DIR = SCRIPT_DIR / "warp"
API_DIR = SCRIPT_DIR / "api"
COMPOSE_TEMPLATE_FILE = "docker-compose.yml.j2"
HAPROXY_TEMPLATE_FILE = "haproxy.cfg.j2"
API_DIR_HAPROXY_TEMPLATE_FILE = API_DIR / "haproxy.cfg.j2"
COMPOSE_OUTPUT_FILE = SCRIPT_DIR / "docker-compose.yml"
HAPROXY_OUTPUT_FILE = SCRIPT_DIR / "haproxy.cfg"
CONFIG_FILE = SCRIPT_DIR / "config.json"
API_DIR_CONFIG_FILE = API_DIR / "config.json"

# --- Logger Setup ---
# This will be configured in main()
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool) -> None:
    """Configure logging to console and a file with beautiful output."""
    console_level = logging.DEBUG if verbose else logging.INFO
    
    # Console Handler (for beautiful, high-level output)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_formatter = logging.Formatter("[%(levelname)s] %(message)s")
    console_handler.setFormatter(console_formatter)


    # Configure the root logger
    logging.basicConfig(level=logging.DEBUG, handlers=[console_handler])
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
            "haproxy_port": int, "fastapi_port": int, "warp_socks_port": int,
            "country_endpoints": dict, "default_endpoint": str
        }
        for key, key_type in required_keys.items():
            if key not in config or not isinstance(config[key], key_type):
                raise ValueError(f"Invalid or missing key in config.json: '{key}'")
        if config["num_tunnels"] < 1:
            raise ValueError("num_tunnels must be an integer >= 1")
        if len(config["country"]) != 2:
            raise ValueError("country must be a 2-letter ISO code")

        logger.debug("Config validation passed.")
        return config
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logger.error("Configuration error in '%s': %s", CONFIG_FILE, e)
        sys.exit(1)


def run_command(cmd: List[str], description: str) -> None:
    """Runs a command, handling errors and logging its full output to the log file."""
    logger.debug("Running command to %s: %s", description, " ".join(cmd))
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=SCRIPT_DIR)
        # Log stdout/stderr to the debug log file for troubleshooting
        if result.stdout:
            logger.debug("Command stdout:\n---\n%s---", result.stdout.strip())
        if result.stderr:
            logger.debug("Command stderr:\n---\n%s---", result.stderr.strip())
    except FileNotFoundError:
        logger.error("Command '%s' not found. Is Docker installed and in your PATH?", cmd[0])
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to %s.", description)
        # Log detailed error output before exiting
        if e.stdout:
            logger.error("Command stdout:\n---\n%s---", e.stdout.strip())
        if e.stderr:
            logger.error("Command stderr:\n---\n%s---", e.stderr.strip())
        log_docker_ps()
        sys.exit(1)


def wait_for_healthy_containers(container_names: List[str], timeout: int = 300) -> bool:
    """Polls containers until they are all healthy or a timeout is reached."""
    logger.info("Waiting for WARP containers to become healthy...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Name}} {{.State.Health.Status}}", *container_names],
                check=True, capture_output=True, text=True
            )
            statuses = {line.split()[0].lstrip('/'): line.split()[1] for line in result.stdout.strip().split('\n')}
            logger.debug("Current health statuses: %s", statuses)
            healthy_count = list(statuses.values()).count("healthy")

            progress = healthy_count / len(container_names)
            bar_length = 30
            filled_length = int(bar_length * progress)
            bar = '█' * filled_length + '─' * (bar_length - filled_length)
            # Use a carriage return to keep the progress bar on one line
            sys.stdout.write(f"\rProgress: [{bar}] {healthy_count}/{len(container_names)} healthy")
            sys.stdout.flush()

            if healthy_count == len(container_names):
                sys.stdout.write("\n")  # Move to the next line after completion
                return True
        except (subprocess.CalledProcessError, IndexError) as e:
            logger.debug("Could not inspect containers yet (they may be starting). Error: %s", e)
        time.sleep(5)

    sys.stdout.write("\n")
    logger.error("Timeout reached while waiting for containers to become healthy.")
    return False


def log_docker_ps() -> None:
    """Logs the output of 'docker ps -a' for debugging failures."""
    logger.info("Dumping container status ('docker ps -a')...")
    try:
        result = subprocess.run(["docker", "ps", "-a"], check=True, capture_output=True, text=True)
        # Log to the main logger, which will go to the file and console
        logger.debug("--- DOCKER PS -A ---\n%s\n--------------------", result.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error("Could not execute 'docker ps -a': %s", e)


def generate_files_from_templates(config: Dict[str, Any]) -> None:
    """Generates docker-compose.yml and haproxy.cfg from external templates."""
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)

    # --- Prepare context for docker-compose.yml ---
    endpoint = config["country_endpoints"].get(config["country"].upper(), config["default_endpoint"])
    warp_services = {
        f"warp{i}": {
            "warp_host_port": config["warp_host_port_base"] + i,
            "build": {"context": SCRIPT_DIR, "dockerfile": WARP_DIR / "Dockerfile.warp"},
            "container_name": f"{config['project_name']}_warp{i}",
            "restart": "always",
            "environment": {"ENDPOINT": endpoint, "SOCKS5_PORT": str(config["warp_socks_port"])},
            "healthcheck": {
                "test": ["CMD-SHELL", "curl -x socks5h://127.0.0.1:1080 --silent --fail --connect-timeout 3 https://1.1.1.1 || exit 1"],
                "interval": "30s", "timeout": "10s", "retries": "3", "start_period": "180s"
            },
            "volumes": [f"{WARP_DIR / f'warp{i}_config'}:/config"]
        } for i in range(1, config["num_tunnels"] + 1)
    }
    api_service = {
        "build": {"context": API_DIR, "dockerfile": "Dockerfile.api"},
        "container_name": f"{config['project_name']}_api",
        "restart": "always"
    }

    try:
        compose_template = env.get_template(COMPOSE_TEMPLATE_FILE)
        compose_content = compose_template.render(
            warp_services=warp_services, api_service=api_service, SCRIPT_DIR=SCRIPT_DIR, **config
        )
        with open(COMPOSE_OUTPUT_FILE, "w") as f:
            f.write(compose_content)
        logger.debug("Successfully wrote %s", COMPOSE_OUTPUT_FILE)
    except Exception as e:
        logger.error("Failed to generate %s: %s", COMPOSE_OUTPUT_FILE, e)
        sys.exit(1)

    # --- Generate haproxy.cfg ---
    backends = [
        {
            "name": f"warp{i}",
            "port": config['warp_socks_port'],
            "weight": 100 if (i - 1) == 0 else 1 # Give first one highest weight by default
        }
        for i in range(1, config["num_tunnels"] + 1)
    ]
    if HAPROXY_OUTPUT_FILE.is_dir():
        logger.warning("Removing leftover '%s' directory from a previous failed run.", HAPROXY_OUTPUT_FILE)
        shutil.rmtree(HAPROXY_OUTPUT_FILE)
    try:
        haproxy_template = env.get_template(str(HAPROXY_TEMPLATE_FILE))
        haproxy_content = haproxy_template.render(backends=backends, **config)
        with open(HAPROXY_OUTPUT_FILE, "w") as f:
            f.write(haproxy_content)
        logger.debug("Successfully wrote %s", HAPROXY_OUTPUT_FILE)
    except Exception as e:
        logger.error("Failed to generate %s: %s", HAPROXY_OUTPUT_FILE, e)
        sys.exit(1)


def main() -> None:
    """Main orchestration function for the setup process."""
    parser = argparse.ArgumentParser(description="Setup script for the WARPoxy project.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging to the console.")
    args = parser.parse_args()
    
    setup_logging(args.verbose)
    
    try:
        logger.info("--- 🚀 Starting WARPoxy Setup 🚀 ---")

        # Step 1: Load configuration
        logger.info("[STEP 1/4] Loading configuration...")
        config = load_config()
        logger.info("✅ Configuration loaded: %d tunnels in %s.", config["num_tunnels"], config["country"])
        os.environ['COMPOSE_PROJECT_NAME'] = config["project_name"]

        # Step 2: Prepare Environment
        logger.info("[STEP 2/4] Preparing environment and generating config files...")
        run_command(["docker", "pull", "haproxy:2.8"], "pull haproxy:2.8 image")
        for i in range(1, config["num_tunnels"] + 1):
            (WARP_DIR / f"warp{i}_config").mkdir(exist_ok=True)
        generate_files_from_templates(config)
        logger.info("✅ Generated %s and %s.", COMPOSE_OUTPUT_FILE.name, HAPROXY_OUTPUT_FILE.name)

        # Step 3: Start all services
        logger.info("[STEP 3/4] Building and starting services with Docker Compose...")
        compose_cmd = ["docker-compose", "-f", str(COMPOSE_OUTPUT_FILE), "up", "-d", "--build", "--remove-orphans"]
        run_command(compose_cmd, "start services")
        logger.info("✅ Services started successfully.")

        # Step 4: Verify setup by checking health
        logger.info("[STEP 4/4] Verifying service health...")
        warp_container_names = [f"{config['project_name']}_warp{i}" for i in range(1, config["num_tunnels"] + 1)]
        if not wait_for_healthy_containers(warp_container_names):
            log_docker_ps()
            raise RuntimeError("One or more WARP containers failed to become healthy.")

        haproxy_port = config['haproxy_port']
        if socket.socket().connect_ex(('127.0.0.1', haproxy_port)) != 0:
            raise RuntimeError(f"Verification failed: HAProxy is not responding on port {haproxy_port}.")
        logger.info("✅ Health verification complete.")

        logger.info("--- 🎉 Setup Complete 🎉 ---")
        logger.info("Proxy is available at: 127.0.0.1:%d (SOCKS5)", config["haproxy_port"])
        logger.info("API is available at: http://127.0.0.1:%d", config["fastapi_port"])

    except Exception as e:
        logger.error("❌ An unexpected error occurred during setup: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()