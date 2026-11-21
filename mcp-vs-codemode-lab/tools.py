"""Single source of truth for every real operation against the lab stack.

Both benchmark modes call exactly these functions: native mode exposes each one as an
OpenAI-format tool, Code Mode exposes them as the `tools` module inside the sandbox.
All functions return JSON-serialisable data (dict / list / str). Errors are returned as
{"error": "..."} rather than raised, so the model can read them.
"""
import inspect
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request

from config import CFG, LAST_RESET_FILE

S = CFG["stack"]
APP, DB = S["app_container"], S["db_container"]
LAB_CONTAINERS = (APP, DB)
APP_URL = f"http://localhost:{S['app_port']}"

# ----------------------------------------------------------------------------- helpers


def _run(args: list, timeout: int = 30) -> tuple[int, str, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout}s"


def _docker(*args, timeout: int = 30):
    code, out, err = _run(["docker", *args], timeout)
    if code != 0:
        return {"error": (err or out).strip()}
    return out


def _check_name(name: str):
    if name not in LAB_CONTAINERS:
        return {"error": f"unknown container {name!r}; lab containers are {list(LAB_CONTAINERS)}"}
    return None


def _inspect(name: str):
    if err := _check_name(name):
        return err
    out = _docker("inspect", name)
    if isinstance(out, dict):
        return out
    return json.loads(out)[0]


def _psql_json(sql: str, read_only: bool = True, timeout_ms: int = 10000):
    """Run SQL as the DB superuser (local socket, uses reserved slots) and return rows as a list of dicts."""
    sql = sql.strip().rstrip(";")
    wrapped = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t"
    opts = f"-c statement_timeout={timeout_ms}" + (" -c default_transaction_read_only=on" if read_only else "")
    code, out, err = _run(["docker", "exec", "-e", f"PGOPTIONS={opts}", DB, "psql", "-U", S["db_user"],
                           "-d", S["db_name"], "-XAtc", wrapped], timeout=timeout_ms // 1000 + 15)
    if code != 0:
        return {"error": (err or out).strip()}
    return json.loads(out or "[]")


def _http(path: str, timeout: float = 5.0) -> dict:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(APP_URL + path, timeout=timeout) as r:
            body = r.read().decode(errors="replace")
            status = r.status
    except urllib.error.HTTPError as e:
        body, status = e.read().decode(errors="replace"), e.code
    except Exception as e:
        return {"url": APP_URL + path, "status": None, "error": f"{type(e).__name__}: {e}",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000)}
    return {"url": APP_URL + path, "status": status, "body": body[:2000],
            "elapsed_ms": round((time.perf_counter() - t0) * 1000)}


# ----------------------------------------------------------------------------- core read-only tools


def list_containers() -> list:
    """List all containers of the lab stack with their state and status.

    Returns:
        A list of {name, image, state, status} dicts.
    """
    out = _docker("ps", "-a", "--filter", f"label=com.docker.compose.project={S['project']}",
                  "--format", "{{json .}}")
    if isinstance(out, dict):
        return [out]
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    return [{"name": r["Names"], "image": r["Image"], "state": r["State"], "status": r["Status"]} for r in rows]


def inspect_container(name: str) -> dict:
    """Return the full `docker inspect` JSON document for a container.

    Args:
        name: Container name, e.g. "lab-app" or "lab-db".
    """
    return _inspect(name)


def get_logs(name: str, tail: int = 200) -> str:
    """Return the most recent log lines (stdout and stderr) of a container.

    Args:
        name: Container name.
        tail: Number of lines from the end of the log.
    """
    if err := _check_name(name):
        return err
    code, out, err = _run(["docker", "logs", "--tail", str(tail), name])
    return (out + err) if code == 0 else {"error": err.strip()}


def get_container_stats() -> list:
    """Return a one-shot resource usage snapshot (CPU %, memory usage/limit, net and block IO, PIDs) of lab containers."""
    out = _docker("stats", "--no-stream", "--format", "{{json .}}", *LAB_CONTAINERS)
    if isinstance(out, dict):
        return [out]
    return [json.loads(line) for line in out.splitlines() if line.strip()]


_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|vacuum|lock|call|do|"
    r"set|reset|comment|cluster|reindex|refresh|listen|notify|prepare|execute|begin|commit|rollback|"
    r"pg_terminate_backend|pg_cancel_backend|pg_sleep|pg_reload_conf|lo_import|lo_export)\b", re.I)


def query_db(sql: str) -> list:
    """Run a read-only SQL query against the application database (PostgreSQL) and return rows as dicts.

    Only a single SELECT (or WITH ... SELECT) statement is allowed. System views such as
    pg_stat_activity, pg_locks and pg_settings are available.

    Args:
        sql: A single SELECT statement.
    """
    s = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", s, re.I):
        return {"error": "only SELECT / WITH queries are allowed"}
    if ";" in s:
        return {"error": "only a single statement is allowed"}
    if m := _SQL_FORBIDDEN.search(s):
        return {"error": f"forbidden keyword in read-only query: {m.group(0)}"}
    return _psql_json(s)


def db_connection_count() -> dict:
    """Return current client connection usage of PostgreSQL versus max_connections, grouped by user, application and state."""
    rows = _psql_json("SELECT usename, application_name, state, count(*) AS n FROM pg_stat_activity "
                      "WHERE backend_type = 'client backend' GROUP BY 1, 2, 3 ORDER BY n DESC")
    settings = _psql_json("SELECT name, setting FROM pg_settings WHERE name IN "
                          "('max_connections', 'superuser_reserved_connections')")
    if isinstance(rows, dict) or isinstance(settings, dict):
        return rows if isinstance(rows, dict) else settings
    return {"total": sum(r["n"] for r in rows), "settings": {r["name"]: int(r["setting"]) for r in settings},
            "by_user_app_state": rows}


def check_health(path: str = "/health") -> dict:
    """HTTP GET an endpoint of the web app from the host and report status code, body and latency.

    Args:
        path: URL path, default "/health". Other endpoints: "/", "/items".
    """
    return _http(path)


def get_env(name: str) -> dict:
    """Return the environment variables configured for a container (from docker inspect).

    Args:
        name: Container name.
    """
    info = _inspect(name)
    if "error" in info:
        return info
    return dict(e.split("=", 1) for e in info["Config"]["Env"])


def disk_usage() -> dict:
    """Return docker disk usage (images, containers, volumes, build cache) and free space on the host root filesystem."""
    out = _docker("system", "df", "--format", "{{json .}}")
    total, used, free = shutil.disk_usage("/")
    return {"docker": [json.loads(l) for l in out.splitlines()] if isinstance(out, str) else out,
            "host_root_fs": {"total_gb": round(total / 1e9, 1), "used_gb": round(used / 1e9, 1),
                             "free_gb": round(free / 1e9, 1)}}


# ----------------------------------------------------------------------------- extra observability tools


def restart_count(name: str) -> dict:
    """Return how many times Docker has restarted a container and its restart policy.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    return i if "error" in i else {"name": name, "restart_count": i["RestartCount"],
                                   "restart_policy": i["HostConfig"]["RestartPolicy"]}


def exit_code(name: str) -> dict:
    """Return a container's last exit code, error message, and start/finish timestamps.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    if "error" in i:
        return i
    st = i["State"]
    return {"name": name, "status": st["Status"], "exit_code": st["ExitCode"], "state_error": st["Error"],
            "started_at": st["StartedAt"], "finished_at": st["FinishedAt"]}


def oom_status(name: str) -> dict:
    """Report whether a container was killed by the kernel OOM killer, plus its memory limit.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    if "error" in i:
        return i
    return {"name": name, "oom_killed": i["State"]["OOMKilled"], "memory_limit_bytes": i["HostConfig"]["Memory"],
            "memory_swap_limit_bytes": i["HostConfig"]["MemorySwap"], "status": i["State"]["Status"]}


def container_state(name: str) -> dict:
    """Return the compact State block of a container (status, running, paused, restarting, pid, health).

    Args:
        name: Container name.
    """
    i = _inspect(name)
    return i if "error" in i else i["State"]


def container_networks(name: str) -> dict:
    """Return the Docker networks a container is attached to with IP addresses and DNS aliases.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    if "error" in i:
        return i
    return {n: {"ip": v["IPAddress"], "aliases": v.get("Aliases") or v.get("DNSNames")}
            for n, v in i["NetworkSettings"]["Networks"].items()}


def container_volumes(name: str) -> list:
    """Return volume and bind mounts of a container.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    return i if "error" in i else [{"type": m["Type"], "source": m.get("Source"), "destination": m["Destination"],
                                    "rw": m["RW"]} for m in i["Mounts"]]


def container_ports(name: str) -> dict:
    """Return the published port mappings of a container.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    return i if "error" in i else {"ports": i["NetworkSettings"]["Ports"],
                                   "port_bindings": i["HostConfig"]["PortBindings"]}


def image_info(name: str) -> dict:
    """Return image name, id, creation date, size and entrypoint/cmd of a container's image.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    if "error" in i:
        return i
    out = _docker("image", "inspect", i["Image"])
    if isinstance(out, dict):
        return out
    img = json.loads(out)[0]
    return {"image": i["Config"]["Image"], "id": img["Id"][:19], "created": img["Created"],
            "size_mb": round(img["Size"] / 1e6, 1), "entrypoint": img["Config"].get("Entrypoint"),
            "cmd": img["Config"].get("Cmd")}


def container_uptime(name: str) -> dict:
    """Return when a container was created and last started, and whether it is currently running.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    return i if "error" in i else {"created": i["Created"], "started_at": i["State"]["StartedAt"],
                                   "running": i["State"]["Running"]}


def container_processes(name: str) -> str:
    """Return the process list running inside a container (docker top).

    Args:
        name: Container name.
    """
    if err := _check_name(name):
        return err
    return _docker("top", name)


def container_health_status(name: str) -> dict:
    """Return the Docker HEALTHCHECK status and recent probe results of a container, if one is configured.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    if "error" in i:
        return i
    h = i["State"].get("Health")
    return {"configured": h is not None, "status": h and h["Status"],
            "recent": h and [{"exit": l["ExitCode"], "output": l["Output"][:200]} for l in h["Log"][-3:]]}


def container_resource_limits(name: str) -> dict:
    """Return CPU and memory limits configured on a container.

    Args:
        name: Container name.
    """
    i = _inspect(name)
    if "error" in i:
        return i
    hc = i["HostConfig"]
    return {k: hc.get(k) for k in ("Memory", "MemorySwap", "MemoryReservation", "NanoCpus", "CpuShares",
                                   "PidsLimit")}


def container_events(since: str = "") -> list:
    """Return Docker engine events (start, die, kill, oom, restart, ...) for lab containers.

    Args:
        since: Look-back window such as "15m"; empty means since the stack was last deployed.
    """
    try:
        deployed_at = int(LAST_RESET_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        deployed_at = 0
    since = since or (str(deployed_at) if deployed_at else "15m")
    out = _docker("events", "--since", since, "--until", "0s", "--format", "{{json .}}",
                  "--filter", f"label=com.docker.compose.project={S['project']}", timeout=20)
    if isinstance(out, dict):
        return [out]
    events = [json.loads(l) for l in out.splitlines() if l.strip()]
    # drop healthcheck exec noise, and anything from before the stack was (re)deployed by the benchmark's reset
    events = [e for e in events if not e.get("Action", "").startswith("exec_") and e.get("time", 0) >= deployed_at]
    return [{"time": e.get("time"), "type": e.get("Type"), "action": e.get("Action"),
             "name": e.get("Actor", {}).get("Attributes", {}).get("name"),
             "exitCode": e.get("Actor", {}).get("Attributes", {}).get("exitCode")} for e in events]


def compose_services() -> dict:
    """Return the docker-compose service definitions of the lab stack (resolved config)."""
    from injector import compose
    try:
        return json.loads(compose("config", "--format", "json").stdout)["services"]
    except Exception as e:
        return {"error": str(e)}


def compare_env_to_compose(name: str = APP) -> dict:
    """Compare a container's running environment with the defaults declared in docker-compose.yml.

    Args:
        name: Container name, default the app container.
    """
    running = get_env(name)
    if "error" in running:
        return running
    from injector import compose
    try:
        services = json.loads(compose("config", "--format", "json", overrides={}).stdout)["services"]
    except Exception as e:
        return {"error": str(e)}
    svc = next((s for s in services.values() if s.get("container_name") == name), {})
    declared = svc.get("environment", {}) or {}
    return {k: {"declared": declared.get(k), "running": running.get(k)}
            for k in sorted(set(declared) | set(running)) if declared.get(k) != running.get(k)
            and k in declared}


def dns_resolve(hostname: str) -> dict:
    """Resolve a hostname from inside the app container's network (getent hosts).

    Args:
        hostname: Hostname to resolve, e.g. "db".
    """
    code, out, err = _run(["docker", "run", "--rm", "--network", f"{S['project']}_default", "busybox:1.36",
                           "nslookup", hostname], timeout=30)
    return {"hostname": hostname, "resolved": code == 0, "output": (out + err).strip()[:800]}


def check_port(host: str, port: int) -> dict:
    """Test a TCP connection from inside the stack's network to host:port.

    Args:
        host: Hostname or IP, e.g. "db".
        port: TCP port, e.g. 5432.
    """
    code, out, err = _run(["docker", "run", "--rm", "--network", f"{S['project']}_default", "busybox:1.36",
                           "nc", "-z", "-w", "3", host, str(port)], timeout=30)
    return {"host": host, "port": port, "open": code == 0, "detail": (out + err).strip()[:300]}


def app_endpoint_latency(path: str = "/items", samples: int = 3) -> dict:
    """Call an app endpoint several times and report status codes and latency in ms.

    Args:
        path: URL path such as "/items".
        samples: Number of requests (max 5).
    """
    res = [_http(path, timeout=6) for _ in range(min(samples, 5))]
    return {"path": path, "statuses": [r["status"] for r in res], "latency_ms": [r["elapsed_ms"] for r in res],
            "last_error": next((r.get("error") or r.get("body") for r in reversed(res)
                                if r["status"] != 200), None)}


def host_memory() -> dict:
    """Return host memory usage in MB from /proc/meminfo."""
    info = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":")
        if k in ("MemTotal", "MemFree", "MemAvailable", "SwapTotal", "SwapFree", "Cached"):
            info[k] = int(v.split()[0]) // 1024
    return info


def host_load() -> dict:
    """Return host load averages and CPU count."""
    l1, l5, l15 = os.getloadavg()
    return {"load_1m": l1, "load_5m": l5, "load_15m": l15, "cpus": os.cpu_count()}


def host_disk() -> dict:
    """Return free and used space of the host root filesystem in GB."""
    total, used, free = shutil.disk_usage("/")
    return {"total_gb": round(total / 1e9, 1), "used_gb": round(used / 1e9, 1), "free_gb": round(free / 1e9, 1)}


def db_activity() -> list:
    """Return all non-idle PostgreSQL sessions with user, application, state, wait event, duration and query text."""
    return _psql_json("SELECT pid, usename, application_name, client_addr, state, wait_event_type, wait_event, "
                      "round(extract(epoch FROM now() - query_start)) AS query_seconds, left(query, 200) AS query "
                      "FROM pg_stat_activity WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() "
                      "ORDER BY query_start")


def db_locks() -> list:
    """Return PostgreSQL locks (relation, mode, granted) held or awaited by sessions."""
    return _psql_json("SELECT l.pid, a.application_name, l.locktype, l.relation::regclass::text AS relation, "
                      "l.mode, l.granted FROM pg_locks l JOIN pg_stat_activity a USING (pid) "
                      "WHERE l.locktype = 'relation' AND l.relation::regclass::text NOT LIKE 'pg_%' "
                      "AND a.pid <> pg_backend_pid()")


def db_blocking_queries() -> list:
    """Return sessions that are blocked by other sessions, with the blocking pid and its query."""
    return _psql_json("SELECT a.pid AS blocked_pid, a.application_name AS blocked_app, left(a.query, 120) AS "
                      "blocked_query, b.pid AS blocking_pid, b.application_name AS blocking_app, "
                      "left(b.query, 120) AS blocking_query, b.state AS blocking_state "
                      "FROM pg_stat_activity a JOIN LATERAL unnest(pg_blocking_pids(a.pid)) bp(pid) ON true "
                      "JOIN pg_stat_activity b ON b.pid = bp.pid")


def db_slow_queries(min_seconds: int = 2) -> list:
    """Return currently running PostgreSQL queries that have been running longer than a threshold.

    Args:
        min_seconds: Minimum runtime in seconds.
    """
    return _psql_json("SELECT pid, application_name, state, round(extract(epoch FROM now() - query_start)) AS "
                      "seconds, left(query, 200) AS query FROM pg_stat_activity WHERE state <> 'idle' AND "
                      f"pid <> pg_backend_pid() AND now() - query_start > interval '{int(min_seconds)} seconds' "
                      "ORDER BY seconds DESC")


def db_long_transactions(min_seconds: int = 10) -> list:
    """Return PostgreSQL transactions open longer than a threshold, including idle-in-transaction sessions.

    Args:
        min_seconds: Minimum transaction age in seconds.
    """
    return _psql_json("SELECT pid, usename, application_name, state, round(extract(epoch FROM now() - xact_start)) "
                      "AS xact_seconds, left(query, 120) AS last_query FROM pg_stat_activity WHERE xact_start IS NOT "
                      f"NULL AND pid <> pg_backend_pid() AND now() - xact_start > interval '{int(min_seconds)} "
                      "seconds' ORDER BY xact_seconds DESC")


def db_table_sizes() -> list:
    """Return the size and estimated row count of user tables in the database."""
    return _psql_json("SELECT relname AS table, n_live_tup AS rows, pg_size_pretty(pg_total_relation_size(relid)) "
                      "AS total_size FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC")


def db_database_size() -> list:
    """Return the size of each PostgreSQL database."""
    return _psql_json("SELECT datname, pg_size_pretty(pg_database_size(datname)) AS size FROM pg_database "
                      "WHERE NOT datistemplate")


def db_settings(pattern: str = "") -> list:
    """Return PostgreSQL configuration settings whose name contains a pattern.

    Args:
        pattern: Substring of the setting name, e.g. "connection" or "timeout". Empty returns common settings.
    """
    if pattern:
        where = f"name LIKE '%{re.sub(r'[^a-z_]', '', pattern.lower())}%'"
    else:
        where = ("name IN ('max_connections', 'superuser_reserved_connections', 'shared_buffers', 'work_mem', "
                 "'statement_timeout', 'idle_in_transaction_session_timeout', 'lock_timeout')")
    return _psql_json(f"SELECT name, setting, unit, source FROM pg_settings WHERE {where} ORDER BY name")


def db_max_connections() -> dict:
    """Return max_connections, reserved superuser slots and the number of currently used connections."""
    r = _psql_json("SELECT current_setting('max_connections')::int AS max_connections, "
                   "current_setting('superuser_reserved_connections')::int AS reserved_superuser, "
                   "(SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend') AS used")
    return r if isinstance(r, dict) else r[0]


def db_index_usage() -> list:
    """Return sequential vs index scan counts for user tables."""
    return _psql_json("SELECT relname AS table, seq_scan, idx_scan, n_tup_ins, n_tup_upd, n_tup_del "
                      "FROM pg_stat_user_tables")


def db_vacuum_stats() -> list:
    """Return dead tuples and last (auto)vacuum/analyze times for user tables."""
    return _psql_json("SELECT relname AS table, n_dead_tup, last_vacuum, last_autovacuum, last_analyze, "
                      "last_autoanalyze FROM pg_stat_user_tables")


def db_replication_status() -> dict:
    """Return whether this PostgreSQL is a primary or replica and any replication connections."""
    r = _psql_json("SELECT pg_is_in_recovery() AS is_replica, (SELECT count(*) FROM pg_stat_replication) "
                   "AS replicas")
    return r if isinstance(r, dict) else r[0]


def db_errors_in_logs(tail: int = 300) -> list:
    """Return ERROR/FATAL/LOG lines about locks and timeouts from the PostgreSQL container log.

    Args:
        tail: Number of log lines to scan.
    """
    logs = get_logs(DB, tail)
    if isinstance(logs, dict):
        return [logs]
    return [l for l in logs.splitlines() if re.search(r"ERROR|FATAL|PANIC|still waiting|deadlock|timeout", l)]


def app_error_summary(tail: int = 300) -> dict:
    """Count ERROR/CRITICAL lines in the app log and return the most recent distinct error messages.

    Args:
        tail: Number of log lines to scan.
    """
    logs = get_logs(APP, tail)
    if isinstance(logs, dict):
        return logs
    errs = [l for l in logs.splitlines() if " ERROR " in l or " CRITICAL " in l]
    distinct = list(dict.fromkeys(re.sub(r"^\S+ \S+ ", "", l) for l in errs))
    return {"error_lines": len(errs), "recent_distinct": distinct[-5:]}


def network_list() -> list:
    """Return Docker networks of the lab project."""
    out = _docker("network", "ls", "--filter", f"label=com.docker.compose.project={S['project']}",
                  "--format", "{{json .}}")
    return [out] if isinstance(out, dict) else [json.loads(l) for l in out.splitlines() if l.strip()]


def volume_list() -> list:
    """Return Docker volumes of the lab project."""
    out = _docker("volume", "ls", "--filter", f"label=com.docker.compose.project={S['project']}",
                  "--format", "{{json .}}")
    return [out] if isinstance(out, dict) else [json.loads(l) for l in out.splitlines() if l.strip()]


def docker_version() -> dict:
    """Return Docker engine version information."""
    out = _docker("version", "--format", "{{json .Server}}")
    return out if isinstance(out, dict) else json.loads(out)


# ----------------------------------------------------------------------------- mutating tools (approval gate)


def restart_container(name: str) -> dict:
    """MUTATING: restart a container (docker restart). Requires approval.

    Args:
        name: Container name.
    """
    if err := _check_name(name):
        return err
    out = _docker("restart", name, timeout=60)
    return out if isinstance(out, dict) else {"restarted": name}


def set_env_and_restart(name: str, key: str, value: str) -> dict:
    """MUTATING: set an environment variable on the app container and recreate it. Requires approval.

    Args:
        name: Container name (only the app container is supported).
        key: Environment variable name, e.g. "DB_HOST".
        value: New value.
    """
    if name != APP:
        return {"error": f"only {APP} supports env changes"}
    from injector import load_runtime_env, recreate_app
    env = load_runtime_env()
    env[key] = value
    try:
        recreate_app(env)
    except Exception as e:
        return {"error": str(e)}
    return {"recreated": name, "env_overrides": env}


def update_memory_limit(name: str, memory: str) -> dict:
    """MUTATING: change a container's memory limit (docker update). Requires approval.

    Args:
        name: Container name.
        memory: New limit, e.g. "256m".
    """
    if err := _check_name(name):
        return err
    out = _docker("update", "--memory", memory, "--memory-swap", memory, name)
    return out if isinstance(out, dict) else {"updated": name, "memory": memory}


MUTATING = {"restart_container", "set_env_and_restart", "update_memory_limit"}

# ----------------------------------------------------------------------------- registry, approval gate, padding

# "deny" for diagnosis-only runs, "auto" for --with-fix runs. Every mutating call is logged either way.
APPROVAL_POLICY = "deny"
AUDIT_LOG: list[dict] = []



def _real_tools() -> dict:
    return {n: f for n, f in globals().items()
            if inspect.isfunction(f) and not n.startswith("_") and f.__module__ == __name__
            and n not in {"get_registry", "call_tool", "make_dummy_tools"}}


_DUMMY_SERVICES = ["billing", "search", "auth", "payments", "inventory", "shipping", "notifications", "catalog",
                   "recommendations", "analytics", "cdn", "mailer", "scheduler", "ledger", "fraud", "loyalty",
                   "pricing", "reviews", "media", "gateway"]
_DUMMY_METRICS = [("queue_depth", "Return the current message queue depth"),
                  ("cache_hit_ratio", "Return the cache hit ratio over the last 5 minutes"),
                  ("error_rate", "Return the HTTP 5xx error rate over the last 5 minutes"),
                  ("p99_latency", "Return the p99 request latency in milliseconds"),
                  ("feature_flags", "Return the active feature flags")]


def make_dummy_tools(n: int) -> dict:
    """Padding tools for the scaling test: plausible observability endpoints for services not in this stack."""
    tools = {}
    for i in range(n):
        svc = _DUMMY_SERVICES[i % len(_DUMMY_SERVICES)]
        metric, desc = _DUMMY_METRICS[(i // len(_DUMMY_SERVICES)) % len(_DUMMY_METRICS)]
        name = f"{svc}_{metric}" if i < len(_DUMMY_SERVICES) * len(_DUMMY_METRICS) else f"{svc}_{metric}_{i}"

        def fn(region: str = "eu-west-1", window_minutes: int = 5, _svc=svc):
            return {"error": f"service {_svc!r} is not deployed in this environment"}

        fn.__name__ = name
        fn.__doc__ = (f"{desc} of the {svc} service.\n\n    Args:\n        region: Deployment region.\n"
                      f"        window_minutes: Aggregation window in minutes.\n")
        fn.__signature__ = inspect.Signature([
            inspect.Parameter("region", inspect.Parameter.POSITIONAL_OR_KEYWORD, default="eu-west-1",
                              annotation=str),
            inspect.Parameter("window_minutes", inspect.Parameter.POSITIONAL_OR_KEYWORD, default=5,
                              annotation=int)], return_annotation=dict)
        tools[name] = fn
    return tools


def get_registry(extra_tools: int = 0) -> dict:
    """All callable tools by name: the real ones plus `extra_tools` padding tools."""
    reg = _real_tools()
    reg.update(make_dummy_tools(extra_tools))
    return reg


def call_tool(registry: dict, name: str, args: list | None = None, kwargs: dict | None = None):
    """Execute a tool by name through the approval gate. Used by both modes."""
    args, kwargs = args or [], kwargs or {}
    if name not in registry:
        return {"error": f"unknown tool {name!r}"}
    if name in MUTATING:
        approved = APPROVAL_POLICY == "auto"
        AUDIT_LOG.append({"time": time.time(), "tool": name, "args": args, "kwargs": kwargs, "approved": approved})
        if not approved:
            return {"error": "denied by approval gate: this is a diagnosis-only run, mutating tools are disabled"}
    try:
        return registry[name](*args, **kwargs)
    except TypeError as e:
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
