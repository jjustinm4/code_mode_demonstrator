"""Agent loop for both modes against llama-server's OpenAI-compatible chat completions API.

native : every tools.py function is passed as an OpenAI tool; outputs are returned in full.
code   : only search_tools(query) and execute(code) are passed; code runs in sandbox.py.
"""
import inspect
import json
import re
import time
import typing
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

import sandbox
import tools
from config import CFG

L = CFG["llm"]
SB = CFG["sandbox"]

SYSTEM_PROMPT = """You are an SRE assistant investigating a small Docker Compose stack running on this host: \
a web application container and a PostgreSQL database container.
Use the tools to gather evidence. Be efficient: do not repeat calls that already gave you the answer.
{tool_section}
When you have enough evidence, stop calling tools and reply with your final answer. \
Start it with "ROOT CAUSE:" followed by a short explanation of what is wrong and why, citing the evidence."""

NATIVE_SECTION = "Call the provided functions directly to inspect the stack."

CODE_SECTION = """You do not call inspection functions directly. Instead there is a Python module `tools` with many \
functions for inspecting the containers, logs, host and database.
- search_tools(query): keyword search over the `tools` module; returns matching function signatures and docs.
- execute(code): runs Python code with `tools` already available (no import needed), e.g. \
`print(tools.check_health())`. You can call several functions in one execute. \
Functions return Python dicts/lists/strings. Only what you print() is returned to you (max {cap} characters), \
so filter and summarise inside the code instead of printing large objects. Imports are limited to json, re, \
collections, itertools, datetime, math and similar pure modules."""

USER_PROMPT = "The application seems to be having problems. Find out what is wrong and explain the root cause. Do not fix anything."
FINAL_NUDGE = ("Tool budget exhausted. Do not call any more tools. Give your final answer now, starting with "
               "\"ROOT CAUSE:\", based on the evidence you have.")
USER_PROMPT_FIX = ("The application seems to be having problems. Find out what is wrong, explain the root cause, "
                   "and then fix it using the available tools. Verify that the fix worked.")


# ----------------------------------------------------------------------------- LLM client


class ContextOverflow(Exception):
    def __init__(self, n_prompt_tokens: int | None, message: str):
        super().__init__(message)
        self.n_prompt_tokens = n_prompt_tokens


class LLM:
    def __init__(self, base_url: str = L["base_url"]):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(timeout=L["request_timeout_s"])
        self.model = None
        self.n_ctx = None

    def probe(self) -> dict:
        """GET /props and /v1/models; raises httpx errors if the server is unreachable."""
        props = self.http.get(f"{self.base_url}/props", timeout=5).raise_for_status().json()
        models = self.http.get(f"{self.base_url}/v1/models", timeout=5).raise_for_status().json()
        self.n_ctx = props.get("default_generation_settings", {}).get("n_ctx") or models["data"][0]["meta"]["n_ctx"]
        self.model = models["data"][0]["id"]
        return {"model": self.model, "n_ctx": self.n_ctx, "slots": props.get("total_slots"),
                "build": props.get("build_info")}

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        r = self.http.post(f"{self.base_url}/tokenize", json={"content": text}).raise_for_status()
        return len(r.json()["tokens"])

    def tool_definition_tokens(self, tool_defs: list, messages: list) -> int:
        """Tokens the tool list adds to the prompt as the chat template actually renders it."""
        url = f"{self.base_url}/apply-template"
        with_tools = self.http.post(url, json={"messages": messages, "tools": tool_defs}).raise_for_status()
        without = self.http.post(url, json={"messages": messages}).raise_for_status()
        return self.count_tokens(with_tools.json()["prompt"]) - self.count_tokens(without.json()["prompt"])

    def chat(self, messages: list, tool_defs: list, timeout: float, tool_choice: str = "auto") -> dict:
        body = {"model": self.model, "messages": messages, "tools": tool_defs, "tool_choice": tool_choice,
                "temperature": L["temperature"], "max_tokens": L["max_tokens"]}
        r = self.http.post(f"{self.base_url}/v1/chat/completions", json=body, timeout=timeout)
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {"message": r.text}
            msg = err.get("message", r.text)
            if err.get("type") == "exceed_context_size_error" or "exceeds the available context" in msg:
                n = err.get("n_prompt_tokens")
                if n is None and (m := re.search(r"(\d+) tokens", msg)):
                    n = int(m.group(1))
                raise ContextOverflow(n, msg)
            raise RuntimeError(f"llama-server HTTP {r.status_code}: {msg[:300]}")
        return r.json()


# ----------------------------------------------------------------------------- tool schemas

_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def _param_docs(doc: str) -> dict:
    out, in_args = {}, False
    for line in (doc or "").splitlines():
        s = line.strip()
        if s == "Args:":
            in_args = True
            continue
        if in_args:
            if not s or s.endswith(":") and " " not in s:
                break
            if m := re.match(r"(\w+):\s*(.*)", s):
                out[m.group(1)] = m.group(2)
    return out


def _summary(doc: str) -> str:
    doc = inspect.cleandoc(doc or "")
    return doc.split("\n    Args:")[0].split("\nArgs:")[0].strip()


def function_schema(name: str, fn) -> dict:
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn) if not hasattr(fn, "__signature__") else {}
    pdocs = _param_docs(fn.__doc__)
    props, required = {}, []
    for p in sig.parameters.values():
        if p.name.startswith("_"):
            continue
        ann = hints.get(p.name, p.annotation)
        prop = {"type": _PY_TO_JSON.get(ann, "string")}
        if p.name in pdocs:
            prop["description"] = pdocs[p.name]
        if p.default is inspect.Parameter.empty:
            required.append(p.name)
        else:
            prop["default"] = p.default
        props[p.name] = prop
    return {"type": "function", "function": {"name": name, "description": _summary(fn.__doc__),
                                             "parameters": {"type": "object", "properties": props,
                                                            "required": required}}}


def native_tool_defs(registry: dict) -> list:
    return [function_schema(n, f) for n, f in registry.items()]


CODE_TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "search_tools",
        "description": "Keyword search over the `tools` Python module. Returns signatures and docstrings of "
                       "matching functions.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Keywords, e.g. 'logs container' or 'database connections'"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "execute",
        "description": "Run Python code with the `tools` module available. Returns only printed stdout "
                       "(truncated) or a traceback.",
        "parameters": {"type": "object", "properties": {
            "code": {"type": "string", "description": "Python source code to run"}},
            "required": ["code"]}}},
]


def search_tools(registry: dict, query: str, limit: int = 8) -> str:
    aliases = {"database": "db", "postgres": "db", "postgresql": "db", "memory": "oom", "log": "logs"}
    words = set()
    for w in re.findall(r"[a-z0-9]+", query.lower()):
        if len(w) > 1:
            words.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)  # crude stemming: connections -> connection
            if w in aliases:
                words.add(aliases[w])
    scored = []
    for name, fn in registry.items():
        hay_name, hay_doc = name.lower(), (fn.__doc__ or "").lower()
        score = sum(3 * (w in hay_name) + (w in hay_doc) for w in words)
        if score:
            scored.append((score, name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    if not scored:
        return f"No functions match {query!r}. Try broader keywords such as 'container', 'logs', 'db', 'health'."
    out = []
    for _, name in scored[:limit]:
        fn = registry[name]
        sig = str(inspect.signature(fn)).replace("'", '"')
        out.append(f"tools.{name}{sig}\n    {_summary(fn.__doc__).splitlines()[0]}")
    more = f"\n({len(scored) - limit} more matches; refine the query)" if len(scored) > limit else ""
    return "\n".join(out) + more


# ----------------------------------------------------------------------------- agent loop


@dataclass
class RunResult:
    scenario: str
    mode: str
    extra_tools: int = 0
    repeat: int = 1
    prompt_tokens_total: int = 0
    completion_tokens_total: int = 0
    reasoning_tokens: int = 0
    peak_prompt_tokens: int = 0
    peak_context_percent: float = 0.0
    round_trips: int = 0
    tool_calls: int = 0
    tool_definition_tokens: int = 0
    seconds: float = 0.0
    correct: bool | None = None
    error: str = ""
    final_answer: str = ""
    inner_tool_calls: int = 0
    malformed_tool_calls: int = 0
    mutating_calls: list = field(default_factory=list)
    fixed: bool | None = None

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens_total + self.completion_tokens_total

    def row(self) -> dict:
        d = asdict(self)
        d.pop("final_answer"); d.pop("mutating_calls")
        return d


_TEXT_TOOLCALL = re.compile(r"<\|call\|>|to=functions\.|<\|channel\|>|\"name\"\s*:\s*\"\w+\"\s*,\s*\"arguments\"")


def run_agent(llm: LLM, mode: str, scenario: str, registry: dict, *, extra_tools: int = 0, repeat: int = 1,
              with_fix: bool = False, transcript_path: Path | None = None, on_update=None) -> RunResult:
    """Run one diagnosis episode. `on_update(result, event)` is called after every model call / tool call."""
    res = RunResult(scenario=scenario + ("+fix" if with_fix else ""), mode=mode, extra_tools=extra_tools,
                    repeat=repeat)
    if mode == "native":
        tool_defs = native_tool_defs(registry)
        section = NATIVE_SECTION
    else:
        tool_defs = CODE_TOOL_DEFS
        section = CODE_SECTION.format(cap=SB["stdout_cap"])
    messages = [{"role": "system", "content": SYSTEM_PROMPT.format(tool_section=section)},
                {"role": "user", "content": USER_PROMPT_FIX if with_fix else USER_PROMPT}]
    tools.APPROVAL_POLICY = "auto" if with_fix else "deny"
    audit_start = len(tools.AUDIT_LOG)

    tf = open(transcript_path, "w") if transcript_path else None

    def log(event: dict):
        if tf:
            tf.write(json.dumps({"t": round(time.time() - t0, 2), **event}, default=str) + "\n")
            tf.flush()

    def notify(event: str):
        res.seconds = round(time.time() - t0, 1)
        if on_update:
            on_update(res, event)

    t0 = time.time()
    deadline = t0 + L["run_timeout_s"]
    log({"type": "start", "scenario": res.scenario, "mode": mode, "extra_tools": extra_tools,
         "tool_definition_tokens": res.tool_definition_tokens, "n_tools_defined": len(tool_defs),
         "messages": messages})
    try:
        res.tool_definition_tokens = llm.tool_definition_tokens(tool_defs, messages)
        # turns 1..max_turns may call tools; one extra forced turn (tool_choice=none) asks for the answer
        for turn in range(1, L["max_turns"] + 2):
            forced = turn > L["max_turns"]
            remaining = deadline - time.time()
            if remaining <= 0:
                res.error = "timeout"
                break
            if forced:
                res.error = "max_turns"
                messages.append({"role": "user", "content": FINAL_NUDGE})
            notify(f"turn {turn}: waiting for model" + (" (forced answer)" if forced else ""))
            try:
                resp = llm.chat(messages, tool_defs, timeout=min(remaining, L["request_timeout_s"]),
                                tool_choice="none" if forced else "auto")
            except ContextOverflow as e:
                res.error = "context_overflow"
                if e.n_prompt_tokens:
                    res.peak_prompt_tokens = max(res.peak_prompt_tokens, e.n_prompt_tokens)
                log({"type": "context_overflow", "turn": turn, "n_prompt_tokens": e.n_prompt_tokens,
                     "message": str(e)})
                break
            except httpx.TimeoutException:
                res.error = "timeout"
                break
            res.round_trips += 1
            usage = resp.get("usage", {})
            res.prompt_tokens_total += usage.get("prompt_tokens", 0)
            res.completion_tokens_total += usage.get("completion_tokens", 0)
            res.peak_prompt_tokens = max(res.peak_prompt_tokens, usage.get("prompt_tokens", 0))
            choice = resp["choices"][0]
            msg = choice["message"]
            reasoning = msg.get("reasoning_content") or ""
            res.reasoning_tokens += llm.count_tokens(reasoning)
            calls = msg.get("tool_calls") or []
            log({"type": "assistant", "turn": turn, "usage": usage, "finish_reason": choice.get("finish_reason"),
                 "timings": resp.get("timings"), "reasoning": reasoning, "content": msg.get("content"),
                 "tool_calls": calls})
            notify(f"turn {turn}: model replied")

            if not calls or forced:
                content = msg.get("content") or ""
                if _TEXT_TOOLCALL.search(content):
                    log({"type": "anomaly", "kind": "tool_call_as_text", "content": content[:2000]})
                if choice.get("finish_reason") == "length" and not res.error:
                    res.error = "max_tokens"
                res.final_answer = content
                break

            assistant = {"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls}
            if reasoning:
                assistant["reasoning_content"] = reasoning
            messages.append(assistant)
            for call in calls:
                res.tool_calls += 1
                name = call["function"]["name"]
                raw_args = call["function"].get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be a JSON object")
                except ValueError as e:
                    res.malformed_tool_calls += 1
                    output = f"error: could not parse arguments as JSON ({e}): {raw_args[:200]}"
                    log({"type": "anomaly", "kind": "malformed_arguments", "tool": name, "raw": raw_args})
                else:
                    notify(f"turn {turn}: running {name}")
                    output = _execute(mode, name, args, registry, res)
                log({"type": "tool", "turn": turn, "name": name, "args": raw_args, "output_chars": len(output),
                     "output": output})
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "name": name,
                                 "content": output})
                notify(f"turn {turn}: {name} done")
    except Exception as e:
        res.error = f"{type(e).__name__}: {str(e)[:200]}"
        log({"type": "exception", "error": res.error})
    finally:
        res.seconds = round(time.time() - t0, 1)
        res.peak_context_percent = round(100 * res.peak_prompt_tokens / llm.n_ctx, 1) if llm.n_ctx else 0.0
        res.mutating_calls = tools.AUDIT_LOG[audit_start:]
        log({"type": "end", "result": asdict(res)})
        if tf:
            tf.close()
        tools.APPROVAL_POLICY = "deny"
    return res


def _execute(mode: str, name: str, args: dict, registry: dict, res: RunResult) -> str:
    if mode == "native":
        res.inner_tool_calls += 1
        out = tools.call_tool(registry, name, kwargs=args)
        return out if isinstance(out, str) else json.dumps(out, default=str)  # full output, no truncation
    if name == "search_tools":
        return search_tools(registry, str(args.get("query", "")))
    if name == "execute":
        r = sandbox.run_code(str(args.get("code", "")), registry, tools.call_tool, timeout_s=SB["timeout_s"],
                             stdout_cap=SB["stdout_cap"], traceback_cap=SB["traceback_cap"],
                             memory_limit_mb=SB["memory_limit_mb"])
        res.inner_tool_calls += len(r["tool_calls"])
        return r["output"]
    return f"error: unknown tool {name!r}; available tools are search_tools and execute"
