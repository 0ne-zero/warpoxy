import json
import os
import pathlib
import time
import logging
from typing import List, Dict, Any, Optional

import docker
import docker.errors
import requests
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field
from jinja2 import Environment, FileSystemLoader
from fastapi.middleware.cors import CORSMiddleware

# --- Constants ---
# BUG FIX: All paths are now correctly relative to the /app WORKDIR inside the container.
APP_DIR = pathlib.Path(__file__).parent.resolve()
TEMPLATES_DIR = APP_DIR / "templates"
CONFIG_PATH = APP_DIR / "config.json"
CURRENT_INDEX_FILE = APP_DIR / "current_index.json"
HAPROXY_TEMPLATE_FILE = "haproxy.cfg.j2"
HAPROXY_CONFIG_FILE = APP_DIR / "haproxy.cfg"

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s")
logger = logging.getLogger(__name__)

# --- Pydantic Models ---
class Tunnel(BaseModel):
    name: str
    status: str
    public_ip: Optional[str] = Field(None, alias="publicIP")
    is_active: bool = Field(..., alias="isActive")
    direct_access_port: Optional[int] = Field(None, alias="directAccessPort")

class RotateResponse(BaseModel):
    status: str
    new_active_tunnel: str = Field(..., alias="newActiveTunnel")

# --- FastAPI Application Setup ---
app = FastAPI(
    title="WARPoxy API",
    description="An API to manage and rotate a pool of Cloudflare WARP tunnels.",
    version="1.0.0",
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- Dependency Injection ---
def get_docker_client() -> docker.DockerClient:
    """Provides a Docker client, raising an exception if unavailable."""
    try:
        return docker.from_env()
    except docker.errors.DockerException as e:
        logger.error("Could not connect to Docker daemon. Is it running? Error: %s", e)
        raise HTTPException(status_code=503, detail="Cannot connect to Docker daemon.")

def get_config() -> Dict[str, Any]:
    """Loads and validates the main configuration file."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error("Configuration error: %s", e)
        raise HTTPException(status_code=500, detail=f"Server configuration error: {e}")

# --- State Management ---
def get_current_index() -> int:
    """Loads the index of the currently active tunnel."""
    if not CURRENT_INDEX_FILE.exists():
        return 0
    try:
        with open(CURRENT_INDEX_FILE, "r") as f:
            return json.load(f).get("index", 0)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning("Failed to read current_index.json, defaulting to 0. Error: %s", e)
        return 0

def save_current_index(index: int) -> None:
    """Saves the index of the active tunnel."""
    try:
        with open(CURRENT_INDEX_FILE, "w") as f:
            json.dump({"index": index}, f)
    except IOError as e:
        logger.error("Failed to save current_index.json: %s", e)
        raise HTTPException(status_code=500, detail="Failed to save state.")

# --- Helper Functions ---
def _get_public_ip(host: str, port: int) -> Optional[str]:
    """Fetches the public IP address through a specific SOCKS5 proxy."""
    proxies = {"http": f"socks5h://{host}:{port}", "https": f"socks5h://{host}:{port}"}
    try:
        response = requests.get("https://api.ipify.org", proxies=proxies, timeout=10)
        response.raise_for_status()
        ip = response.text.strip()
        logger.info("Fetched public IP via %s:%d: %s", host, port, ip)
        return ip
    except requests.RequestException as e:
        logger.warning("Could not fetch IP via %s:%d: %s", host, port, e)
        return None

def _get_tunnel_details(
    docker_client: docker.DockerClient,
    config: Dict[str, Any],
    tunnel_number: int,
    is_active: bool
) -> Tunnel:
    """Gathers status details for a single tunnel container."""
    tunnel_name = f"warp{tunnel_number}"
    container_name = f"{config['project_name']}_{tunnel_name}"
    status = "not_found"
    host_port_base = config.get("warp_host_port_base") # This is now guaranteed by setup.py
    direct_access_port = host_port_base + tunnel_number if host_port_base else None

    try:
        container = docker_client.containers.get(container_name)
        status = container.status
    except docker.errors.NotFound:
        logger.warning("Container %s not found.", container_name)
    except docker.errors.APIError as e:
        logger.error("Docker API error for %s: %s", container_name, e)
        status = "error"
    
    return Tunnel(
        name=tunnel_name, 
        status=status, 
        publicIP=None, # IP fetching is now on-demand
        isActive=is_active,
        directAccessPort=direct_access_port
    )

def _generate_and_reload_haproxy(config: Dict[str, Any], new_index: int) -> None:
    """Generates a new haproxy.cfg from a template and reloads the HAProxy container."""
    try:
        env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=True)
        template = env.get_template(HAPROXY_TEMPLATE_FILE)

        backends = [
            {
                "name": f"warp{i}",
                "port": config['warp_socks_port'],
                "weight": 100 if (i - 1) == new_index else 1
            }
            for i in range(1, config["num_tunnels"] + 1)
        ]
        
        haproxy_content = template.render(backends=backends, **config)
        with open(HAPROXY_CONFIG_FILE, "w") as f:
            f.write(haproxy_content)
        
        logger.info("Successfully generated new haproxy.cfg.")
        _reload_haproxy(config["project_name"])

    except Exception as e:
        logger.error("Failed to generate or reload haproxy.cfg: %s", e)
        raise HTTPException(status_code=500, detail="Failed to update HAProxy configuration.")

def _reload_haproxy(project_name: str) -> None:
    """Sends a SIGHUP signal to the HAProxy container to reload its config."""
    docker_client = get_docker_client()
    container_name = f"{project_name}_haproxy"
    try:
        haproxy_container = docker_client.containers.get(container_name)
        haproxy_container.kill(signal="SIGHUP")
        logger.info("Successfully sent reload signal to %s", container_name)
    except docker.errors.NotFound:
        raise HTTPException(status_code=404, detail=f"HAProxy container '{container_name}' not found.")
    except docker.errors.APIError as e:
        raise HTTPException(status_code=500, detail=f"Failed to reload HAProxy: {e}")

# --- API Endpoints ---
@app.get("/list", response_model=List[Tunnel], summary="List All Tunnels")
def list_all_tunnels(
    config: Dict[str, Any] = Depends(get_config),
    current_index: int = Depends(get_current_index),
    docker_client: docker.DockerClient = Depends(get_docker_client)
):
    """Retrieves the status and details for all available WARP tunnels."""
    return [
        _get_tunnel_details(docker_client, config, i, (i - 1) == current_index)
        for i in range(1, config["num_tunnels"] + 1)
    ]

@app.get("/current", response_model=Tunnel, summary="Get Current Active Tunnel")
def get_current_tunnel(
    config: Dict[str, Any] = Depends(get_config),
    current_index: int = Depends(get_current_index),
    docker_client: docker.DockerClient = Depends(get_docker_client)
):
    """Retrieves the status and details for only the currently active WARP tunnel."""
    return _get_tunnel_details(docker_client, config, current_index + 1, is_active=True)

@app.get("/tunnels/{tunnel_name}/ip", response_model=Dict[str, Optional[str]], summary="Get Public IP for a Tunnel")
def get_tunnel_ip(tunnel_name: str, config: Dict[str, Any] = Depends(get_config)):
    """
    Fetches the current public IP address for a specific tunnel via its direct access port.
    This is a slow operation and should be used on demand.
    """
    try:
        tunnel_number = int(tunnel_name.replace("warp", ""))
        if not 1 <= tunnel_number <= config["num_tunnels"]:
            raise ValueError("Tunnel number out of range.")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid tunnel name format. Use 'warp<number>'.")

    host_port_base = config.get("warp_host_port_base")
    if not host_port_base:
        raise HTTPException(status_code=404, detail="Direct access ports are not configured.")
        
    direct_access_port = host_port_base + tunnel_number
    public_ip = _get_public_ip("127.0.0.1", direct_access_port)
    
    if not public_ip:
        raise HTTPException(status_code=504, detail="Could not fetch public IP. The tunnel may be down or unresponsive.")
        
    return {"publicIP": public_ip}

@app.post("/rotate", response_model=RotateResponse, summary="Rotate to the Next Tunnel")
def rotate_tunnel(config: Dict[str, Any] = Depends(get_config)):
    """Rotates the active proxy to the next available tunnel in the pool."""
    current_index = get_current_index()
    new_index = (current_index + 1) % config["num_tunnels"]
    
    _generate_and_reload_haproxy(config, new_index)
    save_current_index(new_index)
    
    new_active_tunnel_name = f"warp{new_index + 1}"
    logger.info("Rotated active tunnel to %s", new_active_tunnel_name)
    return RotateResponse(status="rotated", newActiveTunnel=new_active_tunnel_name)