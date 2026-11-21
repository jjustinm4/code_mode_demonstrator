"""Loads config.yaml once; every module reads settings from CFG."""
from pathlib import Path

import yaml

PROJECT_DIR = Path(__file__).resolve().parent
CFG = yaml.safe_load((PROJECT_DIR / "config.yaml").read_text())
RUNTIME_ENV_FILE = PROJECT_DIR / ".runtime_env.json"  # app env overrides currently applied
LAST_RESET_FILE = PROJECT_DIR / ".last_reset"  # unix time of the last stack reset


def compose_env() -> dict:
    """Env vars that feed docker-compose.yml interpolation from config.yaml."""
    s = CFG["stack"]
    return {
        "APP_PORT": str(s["app_port"]),
        "APP_MEMORY": s["app_memory"],
        "DB_MEMORY": s["db_memory"],
        "DB_MAX_CONNECTIONS": str(s["db_max_connections"]),
    }
