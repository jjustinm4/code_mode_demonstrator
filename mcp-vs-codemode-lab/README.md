# mcp-vs-codemode-lab

One script that benchmarks **native MCP-style tool calling** against **Code Mode** on a local LLM,
using a real Docker stack with injected failures.

- **native**: every function in `tools.py` (50 by default) is sent to the model as an OpenAI-format tool
  definition on every request; tool outputs go back to the model in full, untruncated.
- **code**: the model sees only two tools, `search_tools(query)` and `execute(code)`. It discovers
  functions by keyword search and writes Python that calls `tools.<fn>()` in a sandbox; only printed
  stdout (capped at 4,000 chars) goes back to the model.

Both modes call the exact same functions in `tools.py`, get the same system prompt (apart from the tool
section) and the same neutral user prompt:

> The application seems to be having problems. Find out what is wrong and explain the root cause. Do not fix anything.

## Requirements

- llama-server running yourself at `http://localhost:8080` with `--jinja` (tested with
  `ggml-org/gpt-oss-20b-GGUF`, 8,192 tokens context per slot). The benchmark never starts or restarts it;
  it reads the real model name and `n_ctx` from `/props` and `/v1/models`.
- Docker with Compose v2, your user in the `docker` group.
- Python 3.11+ with venv support (`sudo apt install python3.12-venv` on Ubuntu 24.04).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run

```bash
.venv/bin/python run_benchmark.py              # full benchmark (all scenarios, both modes)
.venv/bin/python run_benchmark.py --demo       # 2 scenarios x 2 modes x 1 repeat, screenshot-friendly
```

| Option | Meaning |
|---|---|
| `--scenarios crash,oom,...` | subset of `control, crash, oom, db_conn_exhaustion, slow_query, bad_config` |
| `--repeats N` | repeats per scenario and mode (default 1) |
| `--extra-tools N` | pad the tool list with N dummy tools (scaling test, e.g. 25 or 100) |
| `--with-fix` | extra runs (crash, bad_config) where the model may fix the problem through the approval gate |
| `--no-scaling` | skip the tool-definition token measurement at 0/25/100 extra tools |
| `--export-dir DIR` | where exports go (default `results/`) |
| `--plain` | no colour |

The script brings up the stack, then for every run: reset → inject failure → run the agent → score → reset.
It also resets on errors and on Ctrl+C, so the stack is not left broken. All defaults (ports, memory limits,
timeouts, `max_tokens`, turn limits) are in `config.yaml`.

## Stack and scenarios

`docker-compose.yml` runs only two containers, each limited to 256 MB: `lab-app` (a Flask "shop-api" with
`/health`, `/items` and a memory-hungry `/report`) and `lab-db` (PostgreSQL 16, `max_connections=20`).

| Scenario | Injection (`injector.py`) | Root cause the answer must name |
|---|---|---|
| control | nothing (some normal traffic) | stack is healthy, no invented failure |
| crash | `docker kill lab-app` | app container killed / not running |
| oom | app memory limit set to 80 MB, then a 400 MB `/report` | OOM / memory limit |
| db_conn_exhaustion | 17 leaked `report_worker` sessions idle in transaction | connection slots exhausted |
| slow_query | `nightly_maintenance` session holds `ACCESS EXCLUSIVE` lock on `items` and sleeps | lock / blocking long-running query |
| bad_config | app recreated with `DB_HOST=db-primary.internal` and `restart: on-failure` | wrong DB host, crash loop |

`oom` is skipped with a warning when host `MemAvailable` is below `injection.oom_min_free_ram_gb` (2 GB).
The memory pressure itself stays inside the 80 MB container cgroup.

You can drive the injector by hand:

```bash
.venv/bin/python injector.py up | inject <scenario> | reset | status
```

## Measurements

Per run (`results/results.csv`): prompt and completion tokens summed over all model calls (as reported in
`usage`), reasoning tokens, peak prompt tokens and peak context % (against the real `n_ctx`), round trips,
tool calls, tool-definition tokens, wall-clock seconds, correctness and error (`context_overflow`,
`max_turns`, `timeout`, `max_tokens`, ...).

- **Reasoning tokens**: llama-server does not split them out in `usage`, so they are counted by running
  each `reasoning_content` through `/tokenize`. They are already included in `completion_tokens`.
- **Tool-definition tokens**: the serialized tool list run through `/tokenize`.
- **prompt_tokens_total** counts the full prompt of every call, the way a hosted API would bill it.
  llama-server reuses its prompt cache, so the compute cost is lower than this number suggests.
- **Context overflow**: when a request is larger than `n_ctx`, llama-server rejects it. The run stops
  there with `error=context_overflow`, and the peak context is the rejected prompt size, so it can exceed 100%.
  Token totals of overflowed runs are therefore lower bounds.
- **Scoring** (`scoring.py`): deterministic keyword groups per scenario, applied to the final answer;
  there is no LLM judge. The keywords are in the file; read them before trusting the ✓/✗ marks.
- Outputs are `results/results.csv`, `summary.md` (all tables plus every final answer),
  `terminal_summary.txt` (plain text, paste-ready), `chart.png` and `transcripts/*.jsonl`, which hold every
  request, reasoning, tool call and tool output.

## The Code Mode sandbox is demo grade, not a security boundary

`sandbox.py` runs model code in a `python -I` subprocess in a temp dir with an empty environment, a 30 s
timeout, an address-space rlimit, restricted builtins (no `open/exec/eval/compile`), an import allowlist of
pure modules, and an AST check that rejects private and dunder attribute access. The child has no Docker or
network access of its own: `tools.<fn>()` is a proxy that sends the call over a pipe to the parent, which
runs the real function through the same approval gate as native mode.

This setup is enough to keep a well-behaved model inside the lines. It will not stop a determined attacker,
because Python-level sandboxes are escapable. A real deployment would need OS-level isolation such as a
container, gVisor, Firecracker or V8 isolates.

## Approval gate

`restart_container`, `set_env_and_restart` and `update_memory_limit` are mutating tools. They are visible in
both modes but always go through `tools.call_tool`. In diagnosis runs the gate denies them; in `--with-fix`
runs it auto-approves them. Every attempt is logged in the transcript (`mutating_calls`).

## Re-running

```bash
.venv/bin/python run_benchmark.py              # everything (about 18 min on a 16-thread laptop with gpt-oss-20b)
.venv/bin/python run_benchmark.py --demo       # about 5 min
.venv/bin/python run_benchmark.py --rescore    # re-score the saved answers after editing scoring.py; no LLM calls
```

Close memory-heavy apps first. On a 15 GB machine the model uses about 12 GB, and the host OOM killer once
killed llama-server in the middle of a run. `oom` runs only when 2 GB or more is free.

## Results (2026-11-21, gpt-oss-20b MXFP4, llama.cpp, 8,192 ctx, 50 tools, 1 repeat)

| Scenario | Native tokens | Code tokens | Δ | Peak ctx native | Peak ctx code | Native ✓ | Code ✓ |
|---|---:|---:|---:|---:|---:|:-:|:-:|
| control | 40,004 | 24,731 | -38% | 48% | 40% | 1/1 | 0/1 |
| crash | 40,247 | 19,466 | -52% | 49% | 33% | 1/1 | 0/1 |
| oom | 20,473 | 23,868 | +17% | 37% | 38% | 1/1 | 1/1 |
| db_conn_exhaustion | 24,364 | 15,455 | -37% | 58% | 34% | 1/1 | 1/1 |
| slow_query | 23,054 | 25,976 | +13% | 51% | 69% | 1/1 | 1/1 |
| bad_config | 26,632 | 19,578 | -26% | 52% | 40% | 1/1 | 1/1 |

| Tools available | Native tool-definition tokens | Code Mode tool-definition tokens |
|---:|---:|---:|
| 50 | 1,826 (22% of context) | 122 (1.5%) |
| 75 | 3,260 (40%) | 122 |
| 150 | 7,766 (95%) | 122 |

**Headline:** across 6 scenarios Code Mode used 26% fewer tokens (129,074 vs 174,774), but it diagnosed 4/6
correctly against 6/6 for native. Native did not overflow the context in this run.

What the transcripts show:

- **The whole saving comes from tool definitions.** Native re-sends about 1,822 tokens of definitions on each of
  its 56 model calls, roughly 102k of its 175k tokens. Excluding definitions, Code Mode used more tokens than
  native, because it needed more round trips (67 vs 56).
- **gpt-oss-20b never batched in Code Mode.** Not one of its 46 `execute` calls called more than one tool function.
  It used `execute` as a slower, one-call-at-a-time native mode, plus extra `search_tools` turns.
- **Code Mode's two misses.** In `control` it invented a DB-credential mismatch on a healthy stack. In `crash` it
  called a SIGKILL an OOM kill, although `OOMKilled` was `false`. In both cases native reached the right answer
  with the same tools.
- **At 150 tools**, native tool definitions alone take 95% of an 8k context. That is the case Code Mode is built
  for, but this run did not measure diagnosis quality there (`--extra-tools 100`).

Caveats: this is one repeat per cell at temperature 0, on one local model. The scoring is a deterministic keyword
rubric that I tightened once after reading the answers: `crash` now requires the answer to say the kill was external
or not OOM, which changed one Code Mode mark from ✓ to ✗. The full answers are in `results/summary.md` and
the complete runs in `results/transcripts/`.
