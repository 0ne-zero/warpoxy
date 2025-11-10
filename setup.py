import json
import os
import pathlib
import sys
import argparse
from typing import List, Dict, Any, Optional
from jinja2 import Environment, FileSystemLoader
import subprocess
import logging
import socket
import time

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
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="[%(levelname)s] %(message)s", stream=sys.stdout)
    logger.info("Logging initialized.")

def _bool_env_default(cfg: Dict[str, Any], key: str, default: bool) -> bool:
    val = cfg.get(key, default)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("1", "true", "yes", "on")
    return bool(val)

def load_config() -> Dict[str, Any]:
    """Loads and validates config.json (warp-plus edition)."""
    logger.debug("Attempting to load config from: %s", CONFIG_FILE)
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)

        # Required keys for your provided config
        required = {
            "project_name": str,
            "num_tunnels": int,
            "country": str,
            "haproxy_host": str,
            "haproxy_port": int,
            "fastapi_host": str,
            "fastapi_port": int,
            "warp_socks_port": int,
            "warp_host_port_base": int,
        }
        for k, t in required.items():
            if k not in config:
                raise ValueError(f"Missing required key in config.json: '{k}'")
            if not isinstance(config[k], t):
                raise ValueError(f"Key '{k}' has incorrect type. Expected {t}.")

        # Optional knobs for warp-plus
        # - scan: let warp-plus auto-scan endpoints
        # - cfon: enable Psiphon (country exit hint)
        # - verbose: extra logs from warp-plus
        # - endpoint: explicit endpoint host:port (if you don't want scan/cfon)
        # - warp_key: device key (optional)
        config.setdefault("scan", True)
        config.setdefault("cfon", True)
        config.setdefault("verbose", True)
        # Normalize potential stringy booleans
        config["scan"] = _bool_env_default(config, "scan", True)
        config["cfon"] = _bool_env_default(config, "cfon", False)
        config["verbose"] = _bool_env_default(config, "verbose", False)

        # Sanity checks
        if config["num_tunnels"] < 1:
            raise ValueError("num_tunnels must be >= 1")
        if not (1 <= config["haproxy_port"] <= 65535):
            raise ValueError("haproxy_port must be a valid TCP port")
        if not (1 <= config["warp_socks_port"] <= 65535):
            raise ValueError("warp_socks_port must be a valid TCP port")
        if not (1 <= config["fastapi_port"] <= 65535):
            raise ValueError("fastapi_port must be a valid TCP port")

        logger.debug("Config validation passed.")
        return config
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logger.error("Configuration error in '%s': %s", CONFIG_FILE, e)
        sys.exit(1)

def run_command(cmd: List[str], description: str) -> None:
    """Runs a command and streams its output in real-time."""
    logger.debug("Running command to %s: %s", description, " ".join(map(str, cmd)))
    try:
        process = subprocess.Popen(
            list(map(str, cmd)),
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

def wait_for_healthy_containers(container_names: List[str], timeout: int = 90) -> bool:
    """Polls WARP containers until they are all healthy."""
    logger.info("Waiting for WARP containers to become healthy...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Name}} {{.State.Health.Status}}", *container_names],
                check=True, capture_output=True, text=True
            )
            lines = [ln for ln in result.stdout.strip().split('\n') if ln.strip()]
            statuses = {line.split()[0].lstrip('/'): line.split()[1] for line in lines}
            healthy = sum(1 for v in statuses.values() if v == "healthy")
            total = len(container_names)
            bar_len = 30
            filled = int(bar_len * healthy / max(1, total))
            bar = '█' * filled + '─' * (bar_len - filled)
            sys.stdout.write(f"\rProgress: [{bar}] {healthy}/{total} healthy")
            sys.stdout.flush()
            if healthy == total:
                sys.stdout.write("\n")
                return True
        except (subprocess.CalledProcessError, IndexError) as e:
            logger.debug("Could not inspect containers yet (they may be starting). Error: %s", e)
        time.sleep(3)
    sys.stdout.write("\n")
    logger.error("Timeout reached while waiting for containers to become healthy.")
    return False

def log_docker_ps() -> None:
    logger.info("Dumping container status ('docker ps -a')...")
    try:
        result = subprocess.run(["docker", "ps", "-a"], check=True, capture_output=True, text=True)
        logger.debug("--- DOCKER PS -A ---\n%s\n--------------------", result.stdout)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error("Could not execute 'docker ps -a': %s", e)

def generate_files_from_templates(config: Dict[str, Any]) -> None:
    """Generates docker-compose.yml and haproxy.cfg from templates (warp-plus)."""
    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)

    warp_services: Dict[str, Dict[str, Any]] = {}
    for i in range(1, config["num_tunnels"] + 1):
        name = f"warp{i}"
        host_port = config["warp_host_port_base"] + (i - 1)

        service_env = {
            "SOCKS5_PORT": str(config["warp_socks_port"]),
            "SCAN": "true" if config.get("scan", True) else "false",
            "CFON": "true" if config.get("cfon", False) else "false",
            "COUNTRY": config["country"],
            "VERBOSE": "true" if config.get("verbose", False) else "false",
        }
        if "endpoint" in config and isinstance(config["endpoint"], str) and config["endpoint"].strip():
            service_env["ENDPOINT"] = config["endpoint"].strip()
        if "warp_key" in config and isinstance(config["warp_key"], str) and config["warp_key"].strip():
            service_env["WARP_KEY"] = config["warp_key"].strip()

        warp_services[name] = {
            "build": {
                "context": str(SCRIPT_DIR),
                "dockerfile": str(WARP_DIR / "Dockerfile.warp"),
            },
            "container_name": f"{config['project_name']}_{name}",
            "restart": "always",
            "warp_host_port": host_port,
            "environment": service_env,
            "healthcheck": {
                # OLD (stable) approach: egress check via curl through SOCKS5.
                # Requires 'curl' to be present in the warp container image.
                "test": [
                    "CMD-SHELL",
                    f"curl -x socks5h://127.0.0.1:{config['warp_socks_port']} "
                    "--silent --fail --connect-timeout 5 https://1.1.1.1 || exit 1"
                ],
                "interval": "30s",
                "timeout": "10s",
                "retries": "3",
                "start_period": "60s",
            },
            "volumes": [f"{WARP_DIR / f'{name}_config'}:/config"],
        }

    api_service = {
        "build": {"context": str(SCRIPT_DIR), "dockerfile": str(API_DIR / "Dockerfile.api")},
        "container_name": f"{config['project_name']}_api",
        "restart": "always",
    }

    # render docker-compose.yml
    compose_template = env.get_template(COMPOSE_TEMPLATE_FILE)
    compose_content = compose_template.render(
        warp_services=warp_services, api_service=api_service, SCRIPT_DIR=str(SCRIPT_DIR), **config
    )
    with open(COMPOSE_OUTPUT_FILE, "w") as f:
        f.write(compose_content)

    # render haproxy.cfg
    backends = [
        {"name": f"warp{i}", "port": config["warp_socks_port"], "weight": (100 if i == 1 else 1)}
        for i in range(1, config["num_tunnels"] + 1)
    ]
    haproxy_template = env.get_template(HAPROXY_TEMPLATE_FILE)
    haproxy_content = haproxy_template.render(backends=backends, **config)
    with open(HAPROXY_OUTPUT_FILE, "w") as f:
        f.write(haproxy_content)

def main() -> None:
    parser = argparse.ArgumentParser(description="Setup script for the WARPoxy project (warp-plus).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging.")
    args = parser.parse_args()
    setup_logging(args.verbose)

    try:
        logger.info("--- 🚀 Starting WARPoxy Setup (warp-plus) 🚀 ---")

        logger.info("[STEP 1/5] Loading configuration...")
        config = load_config()
        logger.info("✅ Configuration loaded: %d tunnels via country=%s.", config["num_tunnels"], config["country"])
        os.environ["COMPOSE_PROJECT_NAME"] = config["project_name"]

        logger.info("[STEP 2/5] Preparing environment and generating config files...")
        subprocess.run(["docker", "pull", "haproxy:2.8"], capture_output=True, check=True)

        # Ensure per-tunnel config dirs exist
        for i in range(1, config["num_tunnels"] + 1):
            (WARP_DIR / f"warp{i}_config").mkdir(parents=True, exist_ok=True)

        # Initialize rotation state
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
        if not wait_for_healthy_containers(warp_container_names, timeout=120):
            log_docker_ps()
            raise RuntimeError("One or more WARP containers failed to become healthy.")

        # Quick HAProxy socket check
        sock_check = socket.socket()
        sock_check.settimeout(3)
        try:
            sock_check.connect((config["haproxy_host"], config["haproxy_port"]))
        finally:
            sock_check.close()

        logger.info("✅ Health verification complete.")
        logger.info("--- 🎉 Setup Complete 🎉 ---")
        logger.info("Proxy is available at: %s:%d (SOCKS5)", config["haproxy_host"], config["haproxy_port"])
        logger.info("API is available at: http://%s:%d", config["fastapi_host"], config["fastapi_port"])

    except Exception as e:
        logger.error("❌ An unexpected error occurred during setup: %s", e)
        sys.exit(1)

if __name__ == "__main__":
    main()
