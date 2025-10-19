import json
import os
import subprocess
import sys
import logging
import shutil
import glob
import argparse
from typing import List, Dict, Any

# --- Constants ---
# Using constants makes the script easier to read and modify.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
GENERATED_FILES = ["docker-compose.yml", "haproxy.cfg", "current_index.json"]
WARP_CONFIG_DIR_PATTERN = os.path.join(SCRIPT_DIR, "warp", "warp*_config")
DEFAULT_PROJECT_NAME = "warp_proxy"

# --- Logging Configuration ---
# A more detailed format can be useful for debugging.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(funcName)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def load_config() -> Dict[str, Any]:
    """
    Load project_name from config.json with simplified error handling.

    Returns:
        A dictionary containing the configuration. Falls back to a default
        project name if the file is missing, invalid, or incomplete.
    """
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
        if "project_name" not in config:
            raise ValueError("Missing 'project_name' key in config.json")
        logger.info("Successfully loaded configuration from %s", CONFIG_FILE)
        return config
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logger.warning("Could not load config (%s). Falling back to default project name '%s'.", e, DEFAULT_PROJECT_NAME)
        return {"project_name": DEFAULT_PROJECT_NAME}


def run_command(cmd: List[str], description: str) -> bool:
    """
    Run a shell command and handle errors gracefully.

    Args:
        cmd: The command to run as a list of strings.
        description: A brief description of what the command does for logging.

    Returns:
        True if the command succeeded, False otherwise.
    """
    logger.info("Running command to %s: %s", description, " ".join(cmd))
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except FileNotFoundError:
        logger.warning("Command not found: '%s'. Skipping.", cmd[0])
        return False
    except subprocess.CalledProcessError as e:
        # Log stderr as a warning, as it often contains useful info even on failure.
        logger.warning("Failed to %s. Stderr:\n%s", description, e.stderr.strip())
        return False


def stop_and_remove_containers(project_name: str) -> None:
    """
    Stops and removes containers, volumes, and networks for the project.
    Tries `docker-compose` (v1) and falls back to `docker compose` (v2).
    """
    logger.info("Stopping and removing containers, volumes, and networks...")
    os.environ["COMPOSE_PROJECT_NAME"] = project_name

    # Define commands to try in order of preference.
    # The flags ensure a thorough cleanup.
    compose_commands = [
        ["docker-compose", "down", "-v", "--remove-orphans"],
        ["docker", "compose", "down", "-v", "--remove-orphans"],
    ]

    success = False
    for cmd in compose_commands:
        if run_command(cmd, f"run '{' '.join(cmd)}'"):
            logger.info("Successfully shut down services with '%s'.", cmd[0])
            success = True
            break  # Exit the loop on first success

    if not success:
        logger.error("Failed to stop services with all available docker compose commands.")


def remove_images(project_name: str) -> None:
    """
    Removes Docker images used by the project.

    Args:
        project_name: The name of the project to dynamically find related images.
    """
    logger.info("Removing Docker images...")
    # The API image name is derived from the project name and service name ('api').
    images_to_remove = [
        "haproxy:2.8",
        f"{project_name}_api", # Dynamically generated image name
    ]
    
    for image in images_to_remove:
        run_command(["docker", "rmi", "-f", image], f"remove image '{image}'")


def remove_generated_files() -> None:
    """
    Removes generated configuration files and directories safely.
    """
    logger.info("Removing generated files and directories...")
    # Remove individual files
    for file_name in GENERATED_FILES:
        file_path = os.path.join(SCRIPT_DIR, file_name)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info("Removed file: %s", file_path)
        except OSError as e:
            logger.error("Failed to remove file %s: %s", file_path, e)

    # Safely remove warp config directories using glob and shutil
    logger.info("Removing WARP configuration directories...")
    warp_dirs = glob.glob(WARP_CONFIG_DIR_PATTERN)
    if not warp_dirs:
        logger.info("No WARP configuration directories found to remove.")
        return
        
    for dir_path in warp_dirs:
        try:
            shutil.rmtree(dir_path)
            logger.info("Removed directory: %s", dir_path)
        except OSError as e:
            logger.error("Failed to remove directory %s: %s", dir_path, e)


def main() -> None:
    """
    Main cleanup function, orchestrates all cleanup tasks.
    """
    parser = argparse.ArgumentParser(description="Clean up the WARPoxy project environment.")
    parser.add_argument(
        "--remove-images",
        action="store_true",
        help="Also remove the Docker images. This is a destructive action.",
    )
    args = parser.parse_args()

    logger.info("--- Starting WARPoxy Project Cleanup ---")
    config = load_config()
    project_name = config["project_name"]

    stop_and_remove_containers(project_name)
    remove_generated_files()

    if args.remove_images:
        remove_images(project_name)
    else:
        logger.info("Skipping image removal. To remove images, run with the --remove-images flag.")

    logger.info("--- Cleanup Completed Successfully ---")


if __name__ == "__main__":
    main()