import json
import os
import pathlib
import sys
import argparse
from typing import List, Dict, Any
import subprocess
import logging
import shutil
import glob

# --- Constants ---
SCRIPT_DIR = pathlib.Path(__file__).parent.resolve()
CONFIG_FILE = SCRIPT_DIR / "config.json"
COMPOSE_FILE = SCRIPT_DIR / "docker-compose.yml"
GENERATED_FILES = ["docker-compose.yml", "haproxy.cfg", "current_index.json"]
WARP_CONFIG_DIR_PATTERN = str(SCRIPT_DIR / "warp" / "warp*_config")
DEFAULT_PROJECT_NAME = "warp_proxy"

# --- Logger Setup ---
logger = logging.getLogger(__name__)


def setup_logging(verbose: bool) -> None:
    """Configure logging to the console with a clean format."""
    level = logging.DEBUG if verbose else logging.INFO
    
    # Configure logging to only use the console (stdout)
    logging.basicConfig(
        level=level,
        format="[%(levelname)s] %(message)s",
        stream=sys.stdout,
    )
    logger.info("Logging initialized to console.")


def load_config() -> Dict[str, Any]:
    """
    Load project_name from config.json with simplified error handling.
    """
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
        if "project_name" not in config:
            raise ValueError("Missing 'project_name' key in config.json")
        logger.debug("Successfully loaded configuration from %s", CONFIG_FILE)
        return config
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        logger.warning("Could not load config (%s). Falling back to default project name '%s'.", e, DEFAULT_PROJECT_NAME)
        return {"project_name": DEFAULT_PROJECT_NAME}


def run_command(cmd: List[str], description: str) -> bool:
    """
    Run a shell command and handle errors gracefully.
    """
    logger.debug("Running command to %s: %s", description, " ".join(cmd))
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        if result.stdout:
            logger.debug("Command stdout:\n---\n%s---", result.stdout.strip())
        if result.stderr:
            logger.debug("Command stderr:\n---\n%s---", result.stderr.strip())
        return True
    except FileNotFoundError:
        logger.warning("Command not found: '%s'. Skipping.", cmd[0])
        return False
    except subprocess.CalledProcessError as e:
        logger.warning("Failed to %s. Stderr:\n%s", description, e.stderr.strip())
        return False


def stop_and_remove_containers(project_name: str) -> None:
    """
    Stops and removes containers, volumes, and networks for the project.
    """
    os.environ["COMPOSE_PROJECT_NAME"] = project_name

    # --- BUG FIX: Build the commands dynamically based on file existence ---
    cmd_v1 = ["docker-compose"]
    cmd_v2 = ["docker", "compose"]

    if COMPOSE_FILE.exists():
        logger.debug("Found %s, using it for cleanup.", COMPOSE_FILE.name)
        cmd_v1.extend(["-f", str(COMPOSE_FILE)])
        cmd_v2.extend(["-f", str(COMPOSE_FILE)])
    else:
        logger.warning("%s not found. Attempting cleanup using project name only.", COMPOSE_FILE.name)

    # Add the 'down' command and its flags
    cmd_v1.extend(["down", "-v", "--remove-orphans"])
    # BUG FIX: Remove --remove-orphans for older docker compose v2 compatibility
    cmd_v2.extend(["down", "-v"])

    compose_commands_to_try = [cmd_v1, cmd_v2]

    success = False
    for cmd in compose_commands_to_try:
        if run_command(cmd, f"run '{' '.join(cmd)}'"):
            logger.info("✅ Successfully shut down services with '%s'.", cmd[0])
            success = True
            break
    if not success:
        logger.error("❌ Failed to stop services. They may already be stopped or a manual cleanup might be needed.")


def remove_images(project_name: str) -> None:
    """
    Dynamically finds and removes all Docker images associated with the project.
    """
    logger.info("Finding Docker images for project '%s'...", project_name)
    
    find_cmd = [
        "docker", "images",
        "--filter", f"label=com.docker.compose.project={project_name}",
        "--format", "{{.Repository}}:{{.Tag}}"
    ]
    
    try:
        result = subprocess.run(find_cmd, check=True, capture_output=True, text=True)
        project_images = {img for img in result.stdout.strip().split('\n') if img and img != "<none>:<none>"}
        images_to_remove = project_images.union({"haproxy:2.8"})
        
        if not project_images:
             logger.info("No project-specific images found to remove.")
             return

        logger.info("The following images will be removed: %s", list(images_to_remove))
        for image in images_to_remove:
            run_command(["docker", "rmi", "-f", image], f"remove image '{image}'")
        logger.info("✅ Image removal complete.")

    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.error("❌ Failed to find project images. Error: %s", e)


def remove_generated_files() -> None:
    """
    Removes generated configuration files and directories safely.
    """
    for file_name in GENERATED_FILES:
        file_path = SCRIPT_DIR / file_name
        try:
            if file_path.is_file():
                file_path.unlink()
                logger.info("Removed file: %s", file_path)
        except OSError as e:
            logger.error("Failed to remove file %s: %s", file_path, e)

    warp_dirs = glob.glob(WARP_CONFIG_DIR_PATTERN)
    if not warp_dirs:
        logger.info("No WARP configuration directories found to remove.")
    else:
        for dir_path in warp_dirs:
            try:
                shutil.rmtree(dir_path)
                logger.info("Removed directory: %s", dir_path)
            except OSError as e:
                logger.error("Failed to remove directory %s: %s", dir_path, e)
    logger.info("✅ Generated files and directories removed.")


def main() -> None:
    """
    Main cleanup function, orchestrates all cleanup tasks.
    """
    parser = argparse.ArgumentParser(description="Clean up the WARPoxy project environment.")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose debug logging to the console."
    )
    parser.add_argument(
        "--remove-images",
        action="store_true",
        help="Also remove the Docker images. This is a destructive action.",
    )
    args = parser.parse_args()
    
    setup_logging(args.verbose)

    try:
        logger.info("--- 🧹 Starting WARPoxy Project Cleanup 🧹 ---")
        config = load_config()
        project_name = config["project_name"]

        # Step 1: Stop and remove containers
        logger.info("[STEP 1/3] Stopping and removing containers and network...")
        stop_and_remove_containers(project_name)

        # Step 2: Remove generated files
        logger.info("[STEP 2/3] Removing generated files...")
        remove_generated_files()

        # Step 3: Optionally remove images
        logger.info("[STEP 3/3] Handling Docker images...")
        if args.remove_images:
            logger.info("`--remove-images` flag detected. Proceeding with image removal.")
            remove_images(project_name)
        else:
            logger.info("Image removal not requested. Skipping.")
            logger.info("To remove images, run again with the --remove-images flag.")

        logger.info("--- ✅ Cleanup Completed Successfully ---")

    except Exception as e:
        logger.error("❌ An unexpected error occurred during cleanup: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()