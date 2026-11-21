# MCP vs Code Mode results

- Model: `ggml-org/gpt-oss-20b-GGUF`, context 8,192 tokens/slot
- Date: 2026-11-21T16:08
- Scenarios: control, crash, oom, db_conn_exhaustion, slow_query, bad_config; repeats: 1; extra tools: 0

## Headline

- Code Mode used 26% fewer tokens across 6 scenarios (129,074 vs 174,774); native overflowed context in 0 runs.
- Correct diagnoses: native 6/6, Code Mode 4/6.
- Code Mode used MORE tokens in: oom, slow_query.
- Code Mode was less accurate in: control, crash.
- Code Mode errors: control (max_turns), oom (max_turns).

## Runs

| Scenario | Mode | Total tokens | Peak context | Round trips | Time | Result |
|---|---|---:|---:|---:|---:|---|
| control | native | 40,004 | 48% | 13 | 52s | ✓ max_turns |
| control | code | 24,731 | 40% | 13 | 1m19s | ✗ max_turns |
| crash | native | 40,247 | 49% | 13 | 1m05s | ✓ max_turns |
| crash | code | 19,466 | 33% | 12 | 1m01s | ✗ |
| oom | native | 20,473 | 37% | 8 | 33s | ✓ |
| oom | code | 23,868 | 38% | 13 | 1m02s | ✓ max_turns |
| db_conn_exhaustion | native | 24,364 | 58% | 7 | 40s | ✓ |
| db_conn_exhaustion | code | 15,455 | 34% | 9 | 51s | ✓ |
| slow_query | native | 23,054 | 51% | 7 | 39s | ✓ |
| slow_query | code | 25,976 | 69% | 10 | 1m22s | ✓ |
| bad_config | native | 26,632 | 52% | 8 | 48s | ✓ |
| bad_config | code | 19,578 | 40% | 10 | 1m01s | ✓ |

## Per-scenario summary

| Scenario | Native tokens | Code tokens | Δ | Peak ctx native | Peak ctx code | Native ✓ | Code ✓ |
|---|---:|---:|---:|---:|---:|:-:|:-:|
| control | 40,004 | 24,731 | -38% | 48% | 40% | 1/1 | 0/1 |
| crash | 40,247 | 19,466 | -52% | 49% | 33% | 1/1 | 0/1 |
| oom | 20,473 | 23,868 | +17% | 37% | 38% | 1/1 | 1/1 |
| db_conn_exhaustion | 24,364 | 15,455 | -37% | 58% | 34% | 1/1 | 1/1 |
| slow_query | 23,054 | 25,976 | +13% | 51% | 69% | 1/1 | 1/1 |
| bad_config | 26,632 | 19,578 | -26% | 52% | 40% | 1/1 | 1/1 |

## Tool-definition scaling

| Extra tools | Native tools | Native def tokens | % ctx | Code Mode def tokens | % ctx |
|---:|---:|---:|---:|---:|---:|
| 0 | 50 | 1,826 | 22% | 122 | 1.5% |
| 25 | 75 | 3,260 | 40% | 122 | 1.5% |
| 100 | 150 | 7,766 | 95% | 122 | 1.5% |

## Final answers

**control / native** (correct)

> ROOT CAUSE:  
> The stack is running correctly – both the web application and PostgreSQL containers are healthy, the app can connect to the database, and HTTP health checks return 200. No errors appear in the application or database logs, and connection counts are within limits. Therefore, there is no underlying issue to report; the system is functioning as expected.

**control / code** (incorrect)

> ROOT CAUSE:  
> The application is failing to connect to the PostgreSQL database because the credentials supplied to the web‑app container do not match the credentials configured in the database container.  
> - The compose file sets the app’s environment variables to `DB_USER=appuser` and `DB_PASSWORD=apppass`.  
> - The database container is initialized with `POSTGRES_USER=lab` and `POSTGRES_PASSWORD=lab`.  
> - The app logs show “database connection OK”, but this is likely a misleading success message; the actual connection attempt would fail due to the missing `appuser` role, causing the application to be unable to perform database operations.  
> 
> This mismatch between the app’s expected database user and the database’s configured user is the root cause of the observed application problems.

**crash / native** (correct)

> ROOT CAUSE: The application container (`lab-app`) exited immediately after starting up with exit code 137 (SIGKILL). The container’s logs show it successfully started the server and handled a couple of health‑check requests, but then the process was terminated. The `oom_status` indicates it was not killed by the kernel OOM killer, and the container has no restart policy. The most likely explanation is that the container’s main process (the Python script started by `python boot.py`) was terminated by an external signal—most commonly by Docker itself when the container’s memory limit is exceeded or when the host sends a SIGKILL. Since the container’s memory limit is 256 MiB and the host has ample free memory, the termination is probably due to Docker’s default behavior of killing a container that exceeds its memory quota. Thus, the root cause is that the application process is being killed for exceeding its allocated memory limit.

**crash / code** (incorrect)

> ROOT CAUSE:  
> The web‑application container (`lab-app`) is being killed by the kernel for exceeding its memory limit.  
> Evidence:  
> 
> * `tools.container_state('lab-app')` reports `ExitCode: 137` – the standard exit code for an OOM kill.  
> * `tools.container_resource_limits('lab-app')` shows a hard memory cap of **256 MiB** (`Memory: 268435456`).  
> * The container’s logs (`tools.get_logs('lab-app')`) show it starts, connects to the database, serves a few requests, and then exits almost immediately, consistent with an OOM event.  
> 
> Thus the stack fails because the application container does not have enough memory allocated to run, causing it to be terminated by the Docker engine.

**oom / native** (correct)

> ROOT CAUSE:  
> The **lab‑app** container was killed by the kernel OOM killer (exit code 137).  
> Its memory limit is only **80 MiB** (`Memory: 83886080`), but the application attempts to build an in‑memory report that requires roughly **400 MiB** (`building in-memory report (~400 MB)` in the logs).  
> Because the process exceeded the 80 MiB limit, Docker terminated it, leaving the app in an exited state while the database remains healthy.

**oom / code** (correct)

> ROOT CAUSE:  
> The web‑application container (`lab-app`) was killed by the Docker engine due to an out‑of‑memory (OOM) condition.  
> Evidence:  
> * `tools.container_state('lab-app')` shows `Status: 'exited'`, `OOMKilled: True`, and `ExitCode: 137`.  
> * The container had a memory limit of 80 MiB (`Memory: 83886080` bytes) while the application was allocating roughly 400 MB of in‑memory data (as indicated by the log line “building in-memory report (~400 MB)”).  
> * The application started successfully, handled a few requests, then terminated almost immediately, matching the OOM kill timing.  
> 
> Thus the root cause of the application’s failure is insufficient memory allocation for the container.

**db_conn_exhaustion / native** (correct)

> **ROOT CAUSE:**  
> The application is exhausting PostgreSQL’s connection pool.  
> The logs show repeated `OperationalError: connection to server … FATAL: remaining connection slots are reserved for roles with the SUPERUSER attribute`.  
> `db_connection_count` reports 18 active connections against a `max_connections` of 20, with 17 of them in the state *idle in transaction*.  
> `db_activity` lists 17 sessions all idle in transaction, meaning the app has opened a connection for each request and never closed or committed it.  
> Because PostgreSQL reserves 3 slots for superusers, the 17 idle connections leave only 0 usable slots, causing the connection errors and the 503/500 responses.  
> 
> In short, the app leaks database connections, filling the pool and preventing new connections. No other resource limits or network issues are involved.

**db_conn_exhaustion / code** (correct)

> ROOT CAUSE:  
> The PostgreSQL container is running out of available connection slots.  
> * `tools.db_max_connections()` shows the database is configured for **20 max connections** with **3 reserved for superusers**, leaving only **17 normal connections**.  
> * `tools.db_connection_count()` reports **18 total connections**: 17 are from the application user (`appuser`) and are all **“idle in transaction”**.  
> * The web‑app logs (`tools.get_logs('lab-app')`) contain repeated errors:  
>   ```
>   OperationalError: connection to server at "db" … FATAL:  remaining connection slots are reserved for roles with the SUPERUSER attribute
>   ```
>   indicating the app cannot acquire a new connection because the pool has reached its limit.
> 
> Thus the application is exhausting the PostgreSQL connection pool (likely due to a connection‑leak or an overly large pool size) while the database is configured with a too‑low `max_connections` value. This mismatch causes the app to fail with 503/500 responses.

**slow_query / native** (correct)

> **ROOT CAUSE:**  
> The application is timing out because a background maintenance process (`nightly_maintenance`) is holding an **AccessExclusiveLock** on the `items` table. This lock blocks all read queries (`SELECT … FROM items …`) that the web app issues, causing them to wait until the statement timeout is reached. The app logs show repeated `QueryCanceled: canceling statement due to statement timeout` errors, and the PostgreSQL logs confirm that each query is waiting for the lock held by process 50, which is the maintenance job. The lock is held for an extended period (over 30 s), so the app’s queries exceed the configured timeout and fail.

**slow_query / code** (correct)

> ROOT CAUSE:  
> The web application is timing‑out because a long‑running maintenance script is holding an **ACCESS EXCLUSIVE lock** on the `items` table in PostgreSQL.  
> 
> **Evidence**
> 
> 1. **PostgreSQL logs**  
>    ```
>    2026-11-21 10:26:19.349 UTC [58] LOG:  process 58 still waiting for AccessShareLock on relation 16387 of database 16384 after 1000.104 ms
>    2026-11-21 10:26:19.349 UTC [58] DETAIL:  Process holding the lock: 50
>    2026-11-21 10:26:21.349 UTC [58] ERROR:  canceling statement due to statement timeout
>    ```
>    The queries from the web app (process 58, 73, 82, 90) are blocked by a lock held by process 50 and are being cancelled after the statement timeout.
> 
> 2. **Container process list**  
>    ```
>    root 25528 25285 0 15:56 ? 00:00:00 sh -c : lab-inject; (echo 'BEGIN;'; echo 'LOCK TABLE items IN ACCESS EXCLUSIVE MODE;'; echo 'SELECT pg_sleep(3600);') | PGAPPNAME=nightly_maintenance psql -U lab -d lab -q >/dev/null 2>&1
>    root 25535 25528 0 15:56 ? 00:00:00 psql -U lab -d lab -q
>    70 25536 25309 0 15:56 ? 00:00:00 postgres: lab lab [local] SELECT
>    ```
>    The script started at 15:56 (`lab-inject`) opens a transaction, locks the `items` table in **ACCESS EXCLUSIVE** mode, and then sleeps for an hour (`pg_sleep(3600)`). This is the process that PostgreSQL reports as holding the lock (process 50 in the logs).
> 
> 3. **Resulting behavior**  
>    The web app’s `SELECT` statements on `items` are blocked, hit the statement timeout, and return 500 errors.
> 
> **Conclusion**  
> The root cause of the application’s failures is the scheduled maintenance script that locks the `items` table for an extended period, causing lock contention and query timeouts. The issue is not with the application code or the database configuration itself, but with the long‑running exclusive lock held by the maintenance job.

**bad_config / native** (correct)

> ROOT CAUSE:  
> The application container is unable to resolve the database host because the `DB_HOST` environment variable is set to **`db-primary.internal`**, which does not exist in the Docker network.  
> The Compose file correctly defines `DB_HOST=db`, but the running container’s environment shows `DB_HOST=db-primary.internal` (see `get_env` output). Consequently, the app repeatedly fails to resolve the hostname and exits with “cannot reach database” errors (see `get_logs`).  
> 
> The database container is healthy and reachable via the alias `db` (verified by `container_networks` for `lab-db`). The mismatch in the environment variable is the root cause of the application’s failure.

**bad_config / code** (correct)

> ROOT CAUSE:  
> The web‑app container is trying to connect to the database at the hostname **db‑primary.internal**, which does not exist on the Docker network.  
> Evidence:  
> 
> * `tools.get_logs('lab-app')` shows repeated errors:  
>   ```
>   database connection attempt … failed: could not translate host name "db-primary.internal" to address: Name or service not known
>   ```
> * `tools.compare_env_to_compose('lab-app')` reveals a mismatch between the environment variable declared in `docker‑compose.yml` (`DB_HOST=db`) and the value actually running in the container (`DB_HOST=db-primary.internal`).  
> * The PostgreSQL container is healthy (`tools.container_health_status('lab-db')` → status `healthy`) and its network alias is `db` (`tools.container_networks('lab-db')` → alias `db`).  
> 
> Thus the application cannot resolve the database host because the `DB_HOST` environment variable is incorrectly set to a non‑existent hostname. This misconfiguration prevents the app from establishing a database connection.

