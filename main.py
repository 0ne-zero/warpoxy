from fastapi import FastAPI, HTTPException
import os
import json
import requests
import docker
import docker.errors
import time
import logging
import subprocess

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config.json")
CURRENT_INDEX_FILE = "/app/current_index.json"
client = docker.from_env()


def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        required_keys = ["project_name", "num_tunnels", "warp_socks_port", "haproxy_port"]
        for key in required_keys:
            if key not in config:
                raise ValueError(f"Missing key in config.json: {key}")
        if not isinstance(config["num_tunnels"], int) or config["num_tunnels"] < 1:
            raise ValueError("num_tunnels must be an integer >= 1")
        if not isinstance(config["warp_socks_port"], int) or config["warp_socks_port"] < 1:
            raise ValueError("warp_socks_port must be a positive integer")
        if not isinstance(config["haproxy_port"], int) or config["haproxy_port"] < 1:
            raise ValueError("haproxy_port must be a positive integer")
        return config
    except FileNotFoundError:
        logger.error("config.json not found at %s", CONFIG_PATH)
        raise HTTPException(status_code=500, detail="config.json not found")
    except json.JSONDecodeError:
        logger.error("Invalid JSON in config.json")
        raise HTTPException(status_code=500, detail="Invalid config.json")
    except ValueError as e:
        logger.error("Config validation error: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


def load_current_index():
    if os.path.exists(CURRENT_INDEX_FILE):
        try:
            with open(CURRENT_INDEX_FILE, "r") as f:
                return json.load(f).get("index", 0)
        except (json.JSONDecodeError, IOError) as e:
            logger.error("Failed to read current_index.json: %s", e)
            return 0
    return 0


def save_current_index(index):
    try:
        with open(CURRENT_INDEX_FILE, "w") as f:
            json.dump({"index": index}, f)
    except IOError as e:
        logger.error("Failed to save current_index.json: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to save current index: {e}")


def validate_haproxy_cfg(file_path):
    abs_path = os.path.abspath(file_path)
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["docker", "run", "--rm", "-v", f"{abs_path}:/usr/local/etc/haproxy/haproxy.cfg:ro",
                 "haproxy:2.8", "haproxy", "-c", "-f", "/usr/local/etc/haproxy/haproxy.cfg", "-dr"],
                check=True, capture_output=True, text=True
            )
            logger.info("HAProxy config validation: %s", result.stdout.strip())
            return True
        except subprocess.CalledProcessError as e:
            logger.error("HAProxy config validation failed (attempt %d): %s", attempt + 1, e.stderr)
            if attempt < 2:
                time.sleep(1)
            else:
                return False
        except FileNotFoundError:
            logger.error("Docker not found or haproxy:2.8 image unavailable")
            return False


def generate_haproxy_cfg(config, current_index=0):
    backends = []
    for i in range(1, config["num_tunnels"] + 1):
        weight = 100 if (i - 1) == current_index else 1
        backends.append(f"    server warp{i} warp{i}:{config['warp_socks_port']} check weight {weight}")

    cfg = f"""
global
    log stdout format raw local0 info

defaults
    log global
    mode tcp
    timeout connect 10s
    timeout client 60s
    timeout server 60s

frontend socks5_front
    bind *:{config['haproxy_port']}
    default_backend socks5_back

backend socks5_back
    balance roundrobin
    option httpchk GET /cdn-cgi/trace
{"\n".join(backends)}\n
"""
    haproxy_file = "/app/haproxy.cfg"
    try:
        with open(haproxy_file, "w") as f:
            f.write(cfg)
        if not validate_haproxy_cfg(haproxy_file):
            raise HTTPException(status_code=500, detail="Invalid haproxy.cfg generated")
    except IOError as e:
        logger.error("Failed to write haproxy.cfg: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to write haproxy.cfg: {e}")


def get_public_ip(tunnel_name, socks_port, retries=3, delay=2):
    proxies = {
        "http": f"socks5://{tunnel_name}:{socks_port}",
        "https": f"socks5://{tunnel_name}:{socks_port}"
    }
    for attempt in range(retries):
        try:
            response = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
            response.raise_for_status()
            logger.info("Fetched public IP for %s: %s", tunnel_name, response.text.strip())
            return response.text.strip()
        except requests.RequestException as e:
            logger.warning("Attempt %d failed to fetch IP for %s: %s", attempt + 1, tunnel_name, e)
            if attempt == retries - 1:
                return "N/A"
            time.sleep(delay)
    return "N/A"


def get_tunnel_status(config, i):
    container_name = f"{config['project_name']}-warp{i}-1"
    try:
        container = client.containers.get(container_name)
        return container.status
    except docker.errors.NotFound:  # type: ignore
        logger.error("Container %s not found", container_name)
        return "not_found"
    except docker.errors.APIError as e:  # type: ignore
        logger.error("Docker API error for %s: %s", container_name, e)
        return "error"


@app.get("/current")
def get_current():
    config = load_config()
    current_index = load_current_index()
    tunnels = []
    for i in range(1, config["num_tunnels"] + 1):
        name = f"warp{i}"
        status = get_tunnel_status(config, i)
        ip = get_public_ip(name, config["warp_socks_port"]) if status == "running" else "N/A"
        tunnels.append({
            "name": name,
            "status": status,
            "public_ip": ip,
            "active": (i - 1) == current_index
        })
    return {"tunnels": tunnels}


@app.post("/rotate")
def rotate():
    config = load_config()
    current_index = load_current_index()
    new_index = (current_index + 1) % config["num_tunnels"]
    save_current_index(new_index)

    generate_haproxy_cfg(config, new_index)

    haproxy_container = f"{config['project_name']}-haproxy-1"
    try:
        container = client.containers.get(haproxy_container)
        container.kill(signal="HUP")
        logger.info("Rotated to warp%s", new_index + 1)
        return {"status": "rotated", "new_active": f"warp{new_index + 1}"}
    except docker.errors.NotFound:  # type: ignore
        logger.error("HAProxy container %s not found", haproxy_container)
        raise HTTPException(status_code=500, detail="HAProxy container not found")
    except docker.errors.APIError as e:  # type: ignore
        logger.error("Failed to reload HAProxy %s: %s", haproxy_container, e)
        raise HTTPException(status_code=500, detail=f"Failed to reload HAProxy: {e}")
