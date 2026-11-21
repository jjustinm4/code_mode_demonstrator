"""Failure injection and reset for the lab stack.

CLI:
    python injector.py up                 # build + start the stack
    python injector.py inject <scenario>  # crash | oom | db_conn_exhaustion | slow_query | bad_config | control
    python injector.py reset              # restore a healthy stack, verify /health == 200
    python injector.py status
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

from config import CFG, LAST_RESET_FILE, PROJECT_DIR, RUNTIME_ENV_FILE, compose_env

S = CFG["stack"]
INJ = CFG["injection"]
APP, DB = S["app_container"], S["db_container"]
HEALTH_URL = f"http://localhost:{S['app_port']}/health"

SCENARIOS = ["control", "crash", "oom", "db_conn_exhaustion", "slow_query", "bad_config"]


class SkipScenario(Exception):
    """Raised when a scenario cannot run on this host (e.g. not enough free RAM)."""


def sh(args, timeout=120, check=True, env=None) -> subprocess.CompletedProcess:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
    return p


def compose(*args, overrides: dict | None = None, timeout=300) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("DB_HOST", "APP_RESTART")}
    env.update(compose_env())
    env.update(overrides or {})
    return sh(["docker", "compose", "-f", str(PROJECT_DIR / S["compose_file"]), *args], timeout=timeout, env=env)


def load_runtime_env() -> dict:
    try:
        return json.loads(RUNTIME_ENV_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def recreate_app(overrides: dict | None = None):
    """Recreate the app container with compose, applying env overrides (and remembering them)."""
    overrides = overrides or {}
    if overrides:
        RUNTIME_ENV_FILE.write_text(json.dumps(overrides))
    else:
        RUNTIME_ENV_FILE.unlink(missing_ok=True)
    compose("up", "-d", "--no-deps", "--force-recreate", "app", overrides=overrides)


def http_status(url=HEALTH_URL, timeout=5) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def psql_super(sql: str, timeout=20) -> str:
    return sh(["docker", "exec", DB, "psql", "-U", S["db_user"], "-d", S["db_name"], "-XAtc", sql],
              timeout=timeout, check=False).stdout


def traffic(n=6, paths=("/items", "/health", "/items?limit=50")):
    """Generate a little client traffic so the app logs show symptoms."""
    base = f"http://localhost:{S['app_port']}"
    for i in range(n):
        http_status(base + paths[i % len(paths)], timeout=6)


def mem_available_gb() -> float:
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


def container_running(name) -> bool:
    p = sh(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False)
    return p.stdout.strip() == "true"


# --------------------------------------------------------------------------- lifecycle

def up():
    compose("up", "-d", "--build", "--wait", timeout=600)
    sh(["docker", "pull", "-q", "busybox:1.36"], timeout=300)  # used by dns_resolve / check_port
    wait_healthy()


def wait_healthy(timeout=None) -> bool:
    deadline = time.time() + (timeout or S["health_timeout_s"])
    while time.time() < deadline:
        if http_status() == 200:
            return True
        time.sleep(1)
    return False


def reset(verbose=False) -> bool:
    """Restore a healthy stack: recreate both containers with defaults (clears injected sessions,
    limits and logs so no symptoms leak into the next run), then verify /health == 200."""
    log = print if verbose else (lambda *a: None)
    RUNTIME_ENV_FILE.unlink(missing_ok=True)
    log("recreating stack with default config")
    compose("up", "-d", "--force-recreate", "--wait", timeout=300)
    ok = wait_healthy()
    if not ok:
        log("app not healthy after recreate, doing full down/up")
        compose("down", timeout=180)
        compose("up", "-d", "--wait", timeout=300)
        ok = wait_healthy()
    LAST_RESET_FILE.write_text(str(int(time.time())))
    log("healthy" if ok else "STILL UNHEALTHY")
    return ok


# --------------------------------------------------------------------------- scenarios

def inject(scenario: str, verbose=False):
    log = print if verbose else (lambda *a: None)
    if scenario == "control":
        traffic()
    elif scenario == "crash":
        traffic(3)
        sh(["docker", "kill", APP])
    elif scenario == "oom":
        free = mem_available_gb()
        if free < INJ["oom_min_free_ram_gb"]:
            raise SkipScenario(f"oom skipped: host MemAvailable {free:.1f} GB < {INJ['oom_min_free_ram_gb']} GB")
        lim = INJ["oom_memory_limit"]
        sh(["docker", "update", "--memory", lim, "--memory-swap", lim, APP])
        traffic(2)
        http_status(f"http://localhost:{S['app_port']}/report?mb=400", timeout=30)
    elif scenario == "db_conn_exhaustion":
        # a "report worker" that leaks connections, each left idle in transaction
        n = INJ["conn_holders"]
        script = (f"for i in $(seq 1 {n}); do "
                  "(echo 'BEGIN;'; echo 'SELECT count(*) FROM items;'; sleep 3600) | "
                  "PGAPPNAME=report_worker psql -U appuser -d lab -q >/dev/null 2>&1 & done; wait")
        sh(["docker", "exec", "-d", DB, "sh", "-c", f": lab-inject; {script}"])
        time.sleep(3)
        traffic()
    elif scenario == "slow_query":
        # a maintenance job holds an exclusive lock on items while running a long query
        script = ("(echo 'BEGIN;'; echo 'LOCK TABLE items IN ACCESS EXCLUSIVE MODE;'; "
                  "echo 'SELECT pg_sleep(3600);') | PGAPPNAME=nightly_maintenance psql -U lab -d lab -q >/dev/null 2>&1")
        sh(["docker", "exec", "-d", DB, "sh", "-c", f": lab-inject; {script}"])
        time.sleep(2)
        traffic(6, paths=("/items", "/items?limit=50", "/health"))
    elif scenario == "bad_config":
        recreate_app({"DB_HOST": INJ["bad_db_host"], "APP_RESTART": "on-failure"})
    else:
        raise ValueError(f"unknown scenario {scenario!r}")
    log(f"injected {scenario}, settling {INJ['settle_s']}s")
    time.sleep(INJ["settle_s"])


def status() -> dict:
    p = sh(["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={S['project']}",
            "--format", "{{.Names}}\t{{.Status}}"], check=False)
    return {"containers": p.stdout.strip().splitlines(), "health": http_status(),
            "runtime_env": load_runtime_env()}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "up":
        up(); print(status())
    elif cmd == "inject":
        try:
            inject(sys.argv[2], verbose=True)
        except SkipScenario as e:
            print(f"WARNING: {e}")
        print(status())
    elif cmd == "reset":
        ok = reset(verbose=True); print(status()); sys.exit(0 if ok else 1)
    else:
        print(json.dumps(status(), indent=2))
