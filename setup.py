import json
import os
import sys
from jinja2 import Template
import subprocess
import logging
import socket
import time

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def load_config():
    config_file = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_file, "r") as f:
            config = json.load(f)
        required_keys = ["project_name", "num_tunnels", "country", "haproxy_port", "fastapi_port", "warp_socks_port", "country_endpoints", "default_endpoint"]
        for key in required_keys:
            if key not in config:
                raise ValueError(f"Missing key in config.json: {key}")
        if not isinstance(config["num_tunnels"], int) or config["num_tunnels"] < 1:
            raise ValueError("num_tunnels must be an integer >= 1")
        if not isinstance(config["country"], str) or len(config["country"]) != 2:
            raise ValueError("country must be a 2-letter ISO code")
        if not isinstance(config["haproxy_port"], int) or config["haproxy_port"] < 1:
            raise ValueError("haproxy_port must be a positive integer")
        if not isinstance(config["fastapi_port"], int) or config["fastapi_port"] < 1:
            raise ValueError("fastapi_port must be a positive integer")
        if not isinstance(config["warp_socks_port"], int) or config["warp_socks_port"] < 1:
            raise ValueError("warp_socks_port must be a positive integer")
        if config["haproxy_port"] == config["fastapi_port"]:
            raise ValueError("haproxy_port and fastapi_port must be different")
        return config
    except FileNotFoundError:
        logger.error("config.json not found at %s", config_file)
        sys.exit(1)
    except json.JSONDecodeError:
        logger.error("Invalid JSON in config.json")
        sys.exit(1)
    except ValueError as e:
        logger.error("Config error: %s", e)
        sys.exit(1)


def validate_haproxy_cfg(file_path):
    abs_path = os.path.abspath(file_path)
    # Log haproxy.cfg content for debugging
    with open(abs_path, "r") as f:
        cfg_content = f.read()
        logger.info("Generated haproxy.cfg content:\n%s", cfg_content)
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
                time.sleep(1)  # Retry after delay
            else:
                return False
        except FileNotFoundError:
            logger.error("Docker not found or haproxy:2.8 image unavailable")
            return False


def generate_docker_compose(config):
    endpoint = config["country_endpoints"].get(config["country"].upper(), config["default_endpoint"])
    services = {}
    for i in range(1, config["num_tunnels"] + 1):
        services[f"warp{i}"] = {
            "image": "ghcr.io/kingcc/warproxy:latest",
            "restart": "always",
            "environment": {
                "ENDPOINT": endpoint,
                "SOCKS5_PORT": str(config["warp_socks_port"])
            },
            "networks": ["warpnet"],
            "healthcheck": {
                "test": f"netstat -tuln | grep :{config['warp_socks_port']} || exit 1",
                "interval": "30s",
                "timeout": "10s",
                "retries": 3,
                "start_period": "180s"
            }
        }

    compose_template = """
services:
  {% for name, config in services.items() %}
  {{ name }}:
    image: {{ config.image }}
    restart: {{ config.restart }}
    environment:
{% for k, v in config.environment.items() %}
      {{ k }}: {{ v }}
{% endfor %}
    networks:
      - warpnet
    healthcheck:
      test: {{ config.healthcheck.test }}
      interval: {{ config.healthcheck.interval }}
      timeout: {{ config.healthcheck.timeout }}
      retries: {{ config.healthcheck.retries }}
      start_period: {{ config.healthcheck.start_period }}
  {% endfor %}
  
  haproxy:
    image: haproxy:2.8
    restart: always
    ports:
      - "{{ haproxy_port }}:{{ haproxy_port }}"
    volumes:
      - {{ cwd }}/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro
    networks:
      - warpnet
  
  fastapi:
    build:
      context: {{ cwd }}
      dockerfile: Dockerfile
    restart: always
    ports:
      - "{{ fastapi_port }}:{{ fastapi_port }}"
    volumes:
      - {{ cwd }}:/app
      - /var/run/docker.sock:/var/run/docker.sock:ro
    environment:
      CONFIG_PATH: /app/config.json
    command: uvicorn main:app --host 0.0.0.0 --port {{ fastapi_port }} --log-level info
    networks:
      - warpnet
    depends_on:
      {% for name in services.keys() %}
      - {{ name }}
      {% endfor %}
      - haproxy

networks:
  warpnet:
    driver: bridge
"""
    template = Template(compose_template)
    cwd = os.path.abspath(os.path.dirname(__file__))
    with open("docker-compose.yml", "w") as f:
        f.write(template.render(services=services, haproxy_port=config["haproxy_port"], fastapi_port=config["fastapi_port"], cwd=cwd))


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
    haproxy_file = "haproxy.cfg"
    with open(haproxy_file, "w") as f:
        f.write(cfg)

    if not validate_haproxy_cfg(haproxy_file):
        logger.error("Invalid haproxy.cfg, aborting setup")
        sys.exit(1)


def check_port(port):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex(('localhost', port))
        sock.close()
        return result == 0
    except socket.error as e:
        logger.error("Port check failed for %s: %s", port, e)
        return False


def get_container_logs(container_name, lines=10):
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), container_name],
            check=True, capture_output=True, text=True
        )
        logger.info("Logs for %s:\n%s", container_name, result.stdout)
        return result.stdout
    except subprocess.CalledProcessError as e:
        logger.error("Failed to get logs for %s: %s", container_name, e.stderr)
        return ""


if __name__ == "__main__":
    config = load_config()
    os.environ['COMPOSE_PROJECT_NAME'] = config["project_name"]
    os.environ['COMPOSE_DOCKER_CLI_BUILD'] = '0'  # Disable Buildx

    generate_docker_compose(config)
    generate_haproxy_cfg(config)

    try:
        # Try docker-compose first, fallback to docker compose
        cmd = ["docker-compose", "up", "-d", "--build"]
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            logger.info("Docker Compose output: %s", result.stdout)
        except FileNotFoundError:
            cmd = ["docker", "compose", "up", "-d", "--build"]
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            logger.info("Docker Compose output: %s", result.stdout)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to start containers: %s", e.stderr)
        sys.exit(1)
    except FileNotFoundError:
        logger.error("Neither 'docker-compose' nor 'docker compose' found. Please install Docker Compose.")
        sys.exit(1)

    # Wait for containers to stabilize
    time.sleep(10)

    # Check HAProxy port
    if check_port(config["haproxy_port"]):
        logger.info("HAProxy is listening on port %s", config["haproxy_port"])
    else:
        logger.error("HAProxy is not listening on port %s. Check logs: docker logs %s-haproxy-1",
                     config["haproxy_port"], config["project_name"])
        get_container_logs(f"{config['project_name']}-haproxy-1")

    # Check WARP container health
    for i in range(1, config["num_tunnels"] + 1):
        container_name = f"{config['project_name']}-warp{i}-1"
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Health.Status}}", container_name],
                check=True, capture_output=True, text=True
            )
            status = result.stdout.strip()
            logger.info("Container %s health: %s", container_name, status)
            if status != "healthy":
                logger.warning("Container %s is not healthy. Check logs:", container_name)
                get_container_logs(container_name)
        except subprocess.CalledProcessError as e:
            logger.error("Failed to inspect %s: %s", container_name, e.stderr)

    logger.info("Setup complete: %s tunnels to %s (endpoint: %s)",
                config["num_tunnels"],
                config["country"],
                config["country_endpoints"].get(config["country"].upper(), config["default_endpoint"]))
    logger.info("Proxy available at localhost:%s (SOCKS5)", config["haproxy_port"])
    logger.info("FastAPI at http://localhost:%s", config["fastapi_port"])
