"""Deterministic diagnosis scoring: keyword groups per scenario, no LLM judge.

An answer is correct when, for every group of a scenario, at least one keyword of that group
appears in the final answer (case-insensitive substring match), and none of the scenario's
negative keywords appear (used for the healthy control, where any invented failure is wrong).
"""

ROOT_CAUSES = {
    "control": [["healthy", "no issue", "no problem", "nothing wrong", "no error", "operating normally",
                 "working normally", "running normally", "functioning normally", "working as expected",
                 "no fault", "no root cause"]],
    # crash = an external SIGKILL (docker kill), NOT an OOM kill: the answer must say so
    "crash": [["lab-app", "app container", "application", "web app", "shop-api", "the app"],
              ["killed", "exited", "not running", "stopped", "down", "crash", "sigkill", "137", "terminated",
               "dead"],
              ["sigkill", "signal 9", "external", "manual", "docker kill", "not oom", "not due to oom",
               "not killed by the kernel oom", "not killed by the oom", "oomkilled: false", "oomkilled false",
               "not an oom", "not out of memory", "not due to an out-of-memory", "not due to memory"]],
    "oom": [["oom", "out of memory", "out-of-memory", "memory limit"]],
    "db_conn_exhaustion": [["connection"],
                           ["max_connections", "too many", "exhaust", "slots", "connection limit", "reserved",
                            "saturat", "leak", "idle in transaction", "maximum"]],
    "slow_query": [["lock", "pg_sleep", "long-running", "long running", "blocking", "blocked"]],
    "bad_config": [["db_host", "db-primary", "database host", "hostname", "host name"],
                   ["wrong", "incorrect", "invalid", "misconfig", "resolve", "unknown", "not known", "bad",
                    "translate", "dns", "not found", "does not exist", "nonexistent"]],
}


NEGATIVE = {
    "control": ["oomkilled", "out of memory", "exhausted", "too many connections", "crash loop", "was killed",
                "not running", "misconfigured", "lock contention"],
}


def score(scenario: str, answer: str) -> bool:
    base = scenario.split("+")[0]
    text = (answer or "").lower()
    if not text.strip():
        return False
    if any(k in text for k in NEGATIVE.get(base, [])):
        return False
    return all(any(k in text for k in group) for group in ROOT_CAUSES[base])


def keywords(scenario: str) -> list:
    return ROOT_CAUSES[scenario.split("+")[0]]
