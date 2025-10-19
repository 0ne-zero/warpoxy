import json
import os
import sys
from jinja2 import Environment, FileSystemLoader
import subprocess
import logging
import socket
import time
import shutil

# --- Constants ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = "templates"
WARP_DIR_NAME = "warp"
API_DIR_NAME = "api"
COMPOSE_TEMPLATE_FILE = "docker-compose.yml.j2"
HAPROXY_TEMPLATE_FILE = "haproxy.cfg.j2"
COMPOSE_OUTPUT_FILE = "docker-compose.yml"
HAPROXY_OUTPUT_FILE = "haproxy.cfg"
CONFIG_FILE = "config.json"

# Configure detailed logging for development
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# --- CONFIGURATION & VALIDATION ---

def load_config():
    """Loads and validates the config.json file."""
    config_path = os.path.join(SCRIPT_DIR, CONFIG_FILE)
    logger.debug("Attempting to load config from: %s", config_path)
    try:
        with open(config_path, "r") as f:
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
        logger.error("Configuration error in '%s': %s", config_path, e)
        sys.exit(1)

# --- DOCKER & SYSTEM UTILITIES ---

def run_command(cmd, description):
    """Runs a command, handling errors and logging its full output."""
    logger.debug("Running command to %s: %s", description, " ".join(cmd))
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        if result.stdout:
            logger.debug("Command stdout:\n---\n%s---", result.stdout.strip())
        if result.stderr:
            logger.debug("Command stderr:\n---\n%s---", result.stderr.strip())
        return result
    except FileNotFoundError:
        logger.error("Command '%s' not found. Is Docker installed and in your PATH?", cmd[0])
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to %s.", description)
        if e.stdout:
            logger.error("Command stdout:\n---\n%s---", e.stdout.strip())
        if e.stderr:
            logger.error("Command stderr:\n---\n%s---", e.stderr.strip())
        log_docker_ps()
        sys.exit(1)

def wait_for_healthy_containers(container_names, timeout=300):
    """Polls containers until they are all healthy or a timeout is reached."""
    start_time = time.time()
    logger.info("Waiting for WARP containers to become healthy...")
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
            bar = '█' * filled_length + '-' * (bar_length - filled_length)
            sys.stdout.write(f"\rProgress: [{bar}] {healthy_count}/{len(container_names)} healthy")
            sys.stdout.flush()

            if healthy_count == len(container_names):
                sys.stdout.write("\n")
                logger.info("All WARP containers are healthy.")
                return True
        except (subprocess.CalledProcessError, IndexError) as e:
            logger.debug("Could not inspect containers yet (they may be starting). Error: %s", e)
            pass
        time.sleep(5)

    sys.stdout.write("\n")
    logger.error("Timeout reached while waiting for containers to become healthy.")
    return False

def log_docker_ps():
    """Logs the output of 'docker ps -a' for debugging failures."""
    logger.info("Dumping container status ('docker ps -a')...")
    try:
        result = subprocess.run(["docker", "ps", "-a"], check=True, capture_output=True, text=True)
        sys.stderr.write("--- DOCKER PS -A ---\n")
        sys.stderr.write(result.stdout + "\n")
        sys.stderr.write("--------------------\n")
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        sys.stderr.write(f"Could not execute 'docker ps -a': {e}\n")

# --- FILE GENERATION ---

def generate_files_from_templates(config):
    """Generates docker-compose.yml and haproxy.cfg from external templates."""
    template_path = os.path.join(SCRIPT_DIR, TEMPLATES_DIR)
    logger.debug("Initializing Jinja2 environment with template path: %s", template_path)
    env = Environment(loader=FileSystemLoader(template_path))

    # --- Prepare context for docker-compose.yml ---
    logger.debug("Preparing context for docker-compose template...")
    endpoint = config["country_endpoints"].get(config["country"].upper(), config["default_endpoint"])
    
    # WARP services context
    warp_services = {}
    for i in range(1, config["num_tunnels"] + 1):
        warp_services[f"warp{i}"] = {
            "build": {
                "context": SCRIPT_DIR,
                "dockerfile": os.path.join(WARP_DIR_NAME, "Dockerfile.warp")
            },
            "container_name": f"{config['project_name']}_warp{i}",
            "restart": "always",
            "environment": {"ENDPOINT": endpoint, "SOCKS5_PORT": str(config["warp_socks_port"])},
            "healthcheck": {
                "test": ["CMD-SHELL", "curl -x socks5h://127.0.0.1:1080 --silent --fail --connect-timeout 3 https://1.1.1.1 || exit 1"],
                "interval": "30s", "timeout": "10s", "retries": "3", "start_period": "180s"
            },
            "volumes": [f"{os.path.join(SCRIPT_DIR, WARP_DIR_NAME, f'warp{i}_config')}:/config"]
        }

    # API service context
    api_service = {
        "build": {
            "context": os.path.join(SCRIPT_DIR, API_DIR_NAME),
            "dockerfile": "Dockerfile.api"
        },
        "container_name": f"{config['project_name']}_api",
        "restart": "always"
    }

    try:
        compose_template = env.get_template(COMPOSE_TEMPLATE_FILE)
        compose_content = compose_template.render(
            warp_services=warp_services,
            api_service=api_service,
            SCRIPT_DIR=SCRIPT_DIR,
            **config
        )
        logger.debug("--- Generated %s content ---\n%s\n--------------------", COMPOSE_OUTPUT_FILE, compose_content)
        with open(COMPOSE_OUTPUT_FILE, "w") as f:
            f.write(compose_content)
        logger.debug("Successfully wrote %s", COMPOSE_OUTPUT_FILE)
    except Exception as e:
        logger.error("Failed to generate %s: %s", COMPOSE_OUTPUT_FILE, e)
        sys.exit(1)

    # --- Generate haproxy.cfg ---
    logger.debug("Preparing context for haproxy template...")
    backends = []
    for i in range(1, config["num_tunnels"] + 1):
        backends.append({"name": f"warp{i}", "port": config['warp_socks_port']})

    if os.path.isdir(HAPROXY_OUTPUT_FILE):
        logger.warning("Removing leftover '%s' directory from a previous failed run.", HAPROXY_OUTPUT_FILE)
        shutil.rmtree(HAPROXY_OUTPUT_FILE)

    try:
        haproxy_template = env.get_template(HAPROXY_TEMPLATE_FILE)
        haproxy_content = haproxy_template.render(backends=backends, **config)
        logger.debug("--- Generated %s content ---\n%s\n--------------------", HAPROXY_OUTPUT_FILE, haproxy_content)
        with open(HAPROXY_OUTPUT_FILE, "w") as f:
            f.write(haproxy_content)
        logger.debug("Successfully wrote %s", HAPROXY_OUTPUT_FILE)
    except Exception as e:
        logger.error("Failed to generate %s: %s", HAPROXY_OUTPUT_FILE, e)
        sys.exit(1)

# --- MAIN EXECUTION ---

if __name__ == "__main__":
    logger.info("--- Starting WARPoxy Setup ---")

    # Step 1: Load configuration from file
    logger.info("Step 1: Loading configuration from %s...", CONFIG_FILE)
    config = load_config()
    logger.info("Configuration loaded: %d tunnels in %s.", config["num_tunnels"], config["country"])
    logger.debug("Full configuration: \n%s", json.dumps(config, indent=2))
    os.environ['COMPOSE_PROJECT_NAME'] = config["project_name"]

    # Step 2: Prepare Environment
    logger.info("Step 2: Preparing environment and generating config files...")
    run_command(["docker", "pull", "haproxy:2.8"], "pull haproxy:2.8 image")

    for i in range(1, config["num_tunnels"] + 1):
        dir_path = os.path.join(SCRIPT_DIR, WARP_DIR_NAME, f"warp{i}_config")
        logger.debug("Ensuring directory exists: %s", dir_path)
        os.makedirs(dir_path, exist_ok=True)

    generate_files_from_templates(config)
    logger.info("Generated %s and %s.", COMPOSE_OUTPUT_FILE, HAPROXY_OUTPUT_FILE)

    # Step 3: Start all services
    logger.info("Step 3: Building and starting services with Docker Compose...")
    compose_cmd = ["docker-compose", "-f", COMPOSE_OUTPUT_FILE, "up", "-d", "--build", "--remove-orphans"]
    run_command(compose_cmd, "start services")
    logger.info("Services started successfully.")

    # Step 4: Verify setup by checking health
    logger.info("Step 4: Verifying service health...")
    warp_container_names = [f"{config['project_name']}_warp{i}" for i in range(1, config["num_tunnels"] + 1)]
    if not wait_for_healthy_containers(warp_container_names):
        logger.error("One or more WARP containers failed to become healthy. Check logs above.")
        log_docker_ps()
        sys.exit(1)

    haproxy_port = config['haproxy_port']
    logger.debug("Verifying HAProxy port %d is open on 127.0.0.1...", haproxy_port)
    if socket.socket().connect_ex(('127.0.0.1', haproxy_port)) != 0:
        logger.error("Verification failed: HAProxy is not responding on port %d.", haproxy_port)
        sys.exit(1)

    logger.info("--- Setup Complete ---")
    logger.info("Proxy is available at: 127.0.0.1:%d (SOCKS5)", config["haproxy_port"])
    logger.info("API is available at: http://127.0.0.1:%d", config["fastapi_port"])