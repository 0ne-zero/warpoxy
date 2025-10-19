import json
import os
import subprocess
import sys
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def load_config():
    """Load project_name from config.json."""
    config_file = os.path.join(os.path.dirname(__file__), "config.json")
    try:
        with open(config_file, "r") as f:
            config = json.load(f)
        if "project_name" not in config:
            raise ValueError("Missing project_name in config.json")
        return config
    except FileNotFoundError:
        logger.error("config.json not found at %s", config_file)
        return {"project_name": "warp_proxy"}  # Fallback default
    except json.JSONDecodeError:
        logger.error("Invalid JSON in config.json")
        return {"project_name": "warp_proxy"}
    except ValueError as e:
        logger.error("Config error: %s", e)
        return {"project_name": "warp_proxy"}


def run_command(cmd, error_msg):
    """Run a shell command and handle errors."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        logger.info("Executed: %s", " ".join(cmd))
    except subprocess.CalledProcessError as e:
        logger.error("%s: %s", error_msg, e.stderr)
        return False
    except FileNotFoundError:
        logger.error("Command not found: %s", cmd[0])
        return False
    return True


def stop_and_remove_containers(project_name):
    """Stop and remove containers and network."""
    # Try docker-compose first, then docker compose
    for cmd_base in (["docker-compose", "down"], ["docker", "compose", "down"]):
        logger.info("Attempting to stop and remove containers with %s", cmd_base[0])
        if run_command(
            cmd_base,
            f"Failed to run {cmd_base[0]} down"
        ):
            break
    else:
        logger.error("Failed to stop containers with both docker-compose and docker compose")
        return False

    # Explicitly remove network
    network_name = f"{project_name}_warpnet"
    if run_command(
        ["docker", "network", "rm", network_name],
        f"Failed to remove network {network_name}"
    ):
        logger.info("Removed network %s", network_name)
    return True


def remove_images():
    """Remove Docker images used by the project."""
    images = [
        "ghcr.io/kingcc/warproxy:latest",
        "haproxy:2.8",
        "warp_proxy-fastapi"
    ]
    for image in images:
        if run_command(
            ["docker", "rmi", "-f", image],
            f"Failed to remove image {image}"
        ):
            logger.info("Removed image %s", image)


def remove_generated_files():
    """Remove generated configuration files."""
    files = ["docker-compose.yml", "haproxy.cfg", "current_index.json"]
    for file in files:
        file_path = os.path.join(os.path.dirname(__file__), file)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info("Removed file %s", file_path)
            else:
                logger.info("File %s does not exist, skipping", file_path)
        except OSError as e:
            logger.error("Failed to remove %s: %s", file_path, e)


def main():
    """Main cleanup function."""
    logger.info("Starting cleanup of WARP Proxy Project")
    config = load_config()
    project_name = config["project_name"]
    os.environ['COMPOSE_PROJECT_NAME'] = project_name

    # Stop and remove containers and network
    logger.info("Stopping and removing containers and network")
    stop_and_remove_containers(project_name)

    # Remove images
    # logger.info("Removing Docker images")
    # remove_images()

    # Remove generated files
    logger.info("Removing generated files")
    remove_generated_files()

    logger.info("Cleanup completed successfully")


if __name__ == "__main__":
    main()
