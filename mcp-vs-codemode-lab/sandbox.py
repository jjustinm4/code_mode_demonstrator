"""Code Mode sandbox (demo grade, NOT a security boundary).

The model's code runs in a separate `python -I` subprocess:
  * cwd is a fresh temp dir, environment is emptied, 30 s wall-clock timeout, address-space rlimit
  * restricted builtins (no open/exec/eval/compile/input), imports limited to a small allowlist
    of pure modules, and an AST check that rejects dunder / private attribute access
  * no network or docker access of its own: `tools.<fn>(...)` is a proxy that sends the call over
    a pipe to the parent, which executes the real function from tools.py via tools.call_tool
    (so the approval gate applies identically to both modes)
  * only stdout is returned, capped; tracebacks are returned truncated so the model can retry
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import threading

ALLOWED_MODULES = {"json", "re", "math", "collections", "itertools", "functools", "datetime", "textwrap",
                   "string", "operator", "heapq", "bisect", "decimal", "fractions", "pprint"}
BLOCKED_BUILTINS = {"open", "exec", "eval", "compile", "input", "breakpoint", "help", "exit", "quit",
                    "globals", "locals", "vars", "memoryview", "__import__", "__build_class__"}


def check_code(code: str) -> str | None:
    """Static checks before running. Returns an error message or None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError: {e}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return f"SandboxError: access to private attribute '{node.attr}' is not allowed"
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return f"SandboxError: name '{node.id}' is not allowed"
    return None


def run_code(code: str, registry: dict, call_tool, timeout_s: int = 30, stdout_cap: int = 4000,
             traceback_cap: int = 1500, memory_limit_mb: int = 512) -> dict:
    """Execute model code. Returns {"output": str, "ok": bool, "tool_calls": [names]}."""
    if err := check_code(code):
        return {"output": err, "ok": False, "tool_calls": []}

    calls: list[str] = []
    to_child_r, to_child_w = os.pipe()
    from_child_r, from_child_w = os.pipe()
    with tempfile.TemporaryDirectory(prefix="codemode-") as tmp:
        code_path = os.path.join(tmp, "main.py")
        with open(code_path, "w") as f:
            f.write(code)
        proc = subprocess.Popen(
            [sys.executable, "-I", os.path.abspath(__file__), "--child", str(to_child_r), str(from_child_w),
             code_path, ",".join(sorted(registry)), str(memory_limit_mb)],
            cwd=tmp, env={"PATH": "/usr/bin:/bin", "HOME": tmp}, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, pass_fds=(to_child_r, from_child_w), text=True)
        os.close(to_child_r)
        os.close(from_child_w)

        def serve():
            with os.fdopen(from_child_r) as rf, os.fdopen(to_child_w, "w") as wf:
                for line in rf:
                    req = json.loads(line)
                    calls.append(req["tool"])
                    result = call_tool(registry, req["tool"], req.get("args"), req.get("kwargs"))
                    try:
                        wf.write(json.dumps({"result": result}, default=str) + "\n")
                        wf.flush()
                    except BrokenPipeError:
                        return

        server = threading.Thread(target=serve, daemon=True)
        server.start()
        try:
            out, err = proc.communicate(timeout=timeout_s)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            timed_out = True
        server.join(timeout=2)

    if len(out) > stdout_cap:
        out = out[:stdout_cap] + f"\n[... stdout truncated, {len(out) - stdout_cap} more chars]"
    if timed_out:
        return {"output": out + f"\nTimeoutError: execution exceeded {timeout_s}s", "ok": False, "tool_calls": calls}
    if proc.returncode != 0:
        tb = err.strip()
        if len(tb) > traceback_cap:
            tb = "[... traceback truncated]\n" + tb[-traceback_cap:]
        return {"output": (out + "\n" if out else "") + tb, "ok": False, "tool_calls": calls}
    if not out.strip():
        out = "(no output - use print() to see results)"
    return {"output": out, "ok": True, "tool_calls": calls}


# ----------------------------------------------------------------------------- child process


def _child(argv):
    import builtins
    import resource

    rfd, wfd, code_path, names, mem_mb = int(argv[0]), int(argv[1]), argv[2], argv[3].split(","), int(argv[4])
    limit = mem_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    rf, wf = os.fdopen(rfd), os.fdopen(wfd, "w")
    code = open(code_path).read()

    class ToolError(Exception):
        pass

    class Tools:
        """Proxy: tools.<name>(*args, **kwargs) runs the real function in the parent process."""

        def __getattr__(self, name):
            if name not in names:
                raise AttributeError(f"tools has no function {name!r}; use search_tools to find functions")

            def call(*args, **kwargs):
                wf.write(json.dumps({"tool": name, "args": list(args), "kwargs": kwargs}) + "\n")
                wf.flush()
                return json.loads(rf.readline())["result"]

            call.__name__ = name
            return call

        def __dir__(self):
            return names

    real_import = builtins.__import__
    for m in ALLOWED_MODULES:
        real_import(m)

    tools_proxy = Tools()

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tools":  # `import tools` / `from tools import x` resolve to the proxy
            return tools_proxy
        if name.split(".")[0] not in ALLOWED_MODULES:
            raise ImportError(f"import of {name!r} is not allowed in the sandbox (allowed: "
                              f"{', '.join(sorted(ALLOWED_MODULES))})")
        return real_import(name, globals, locals, fromlist, level)

    safe_builtins = {k: v for k, v in vars(builtins).items() if k not in BLOCKED_BUILTINS}
    safe_builtins["__import__"] = safe_import
    safe_builtins["__build_class__"] = builtins.__build_class__  # allow class definitions
    env = {"__builtins__": safe_builtins, "__name__": "__main__", "tools": tools_proxy, "ToolError": ToolError}
    try:
        exec(compile(code, "<code>", "exec"), env)
    except BaseException as e:
        # report only frames from the model's own code, not sandbox internals
        import traceback
        src = code.splitlines()
        lines = ["Traceback (most recent call last):"]
        for fr in traceback.extract_tb(e.__traceback__):
            if fr.filename == "<code>":
                lines.append(f"  line {fr.lineno}: {src[fr.lineno - 1].strip() if fr.lineno <= len(src) else ''}")
        lines.append(f"{type(e).__name__}: {e}")
        sys.stderr.write("\n".join(lines) + "\n")
        sys.exit(1)


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--child":
    _child(sys.argv[2:])
