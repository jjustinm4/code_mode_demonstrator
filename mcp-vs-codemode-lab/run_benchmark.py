#!/usr/bin/env python3
"""mcp-vs-codemode-lab: benchmark native MCP-style tool calling vs Code Mode on a local LLM.

    python run_benchmark.py            # full benchmark
    python run_benchmark.py --demo     # 2 scenarios x 2 modes x 1 repeat, for screenshots

Checks llama-server, brings up the Docker stack, injects each failure, runs both modes, scores,
resets, shows a live dashboard and exports results/ (csv, md, txt, png, transcripts).
"""
import argparse
import datetime as dt
import io
import json
import statistics
import sys
import time
from pathlib import Path

import httpx
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

import harness
import injector
import scoring
import tools
from config import CFG, PROJECT_DIR

WIDTH = 110
B = CFG["benchmark"]
CSV_COLUMNS = ["scenario", "mode", "extra_tools", "repeat", "prompt_tokens_total", "completion_tokens_total",
               "reasoning_tokens", "peak_prompt_tokens", "peak_context_percent", "round_trips", "tool_calls",
               "tool_definition_tokens", "seconds", "correct", "error"]
MODES = ["native", "code"]


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--demo", action="store_true", help="fast demo: 2 scenarios x 2 modes x 1 repeat")
    p.add_argument("--scenarios", help=f"comma list from {','.join(injector.SCENARIOS)}")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--extra-tools", type=int, default=0, help="pad the tool list with N dummy tools")
    p.add_argument("--with-fix", action="store_true",
                   help=f"also run {','.join(B['fix_scenarios'])} letting the model fix via the approval gate")
    p.add_argument("--no-scaling", action="store_true", help="skip the tool-definition scaling measurement")
    p.add_argument("--export-dir", default="results")
    p.add_argument("--plain", action="store_true", help="no colour output")
    p.add_argument("--rescore", action="store_true",
                   help="re-score the last run's saved answers with the current scoring.py and re-export (no LLM runs)")
    a = p.parse_args()
    if a.scenarios:
        a.scenarios = [s.strip() for s in a.scenarios.split(",") if s.strip()]
        bad = [s for s in a.scenarios if s not in injector.SCENARIOS]
        if bad:
            p.error(f"unknown scenario(s): {bad}")
    else:
        a.scenarios = B["demo_scenarios"] if a.demo else B["scenarios"]
    if a.demo:
        a.repeats = 1
    return a


# ----------------------------------------------------------------------------- formatting helpers


def ctx_bar(pct: float, width: int = 8) -> str:
    filled = round(min(pct, 100) / 100 * width)
    return "█" * filled + "░" * (width - filled) + f" {pct:3.0f}%"


def fmt_s(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}m{sec % 60:02d}s" if sec >= 60 else f"{sec}s"


def label(r: harness.RunResult) -> str:
    return r.scenario + (f" +{r.extra_tools}t" if r.extra_tools else "") + (f" #{r.repeat}" if r.repeat > 1 else "")


def result_cell(r: harness.RunResult) -> Text:
    if r.error.startswith("skipped"):
        return Text("skipped", style="dim")
    if r.error == "context_overflow":
        return Text("overflow", style="bold yellow")
    mark = Text("✓", style="bold green") if r.correct else Text("✗", style="bold red")
    if r.fixed is not None:
        mark.append(" fixed" if r.fixed else " not fixed", style="green" if r.fixed else "red")
    if r.error:
        mark.append(f" {r.error[:18]}", style="yellow")
    return mark


# ----------------------------------------------------------------------------- live dashboard


class Dashboard:
    def __init__(self, info: dict, args, n_runs: int, est_s: float):
        self.info, self.args, self.est_s = info, args, est_s
        self.t0 = time.time()
        self.results: list[harness.RunResult] = []
        self.current: harness.RunResult | None = None
        self.current_event = ""
        self.progress = Progress(SpinnerColumn(), TextColumn("[bold]{task.description}", justify="left"),
                                 BarColumn(bar_width=30), MofNCompleteColumn(), TimeElapsedColumn(),
                                 expand=False)
        self.task = self.progress.add_task("starting", total=n_runs)

    def step(self, text: str):
        self.progress.update(self.task, description=f"{text:<44.44}")

    def run_done(self, r: harness.RunResult):
        self.results.append(r)
        self.current = None
        self.progress.advance(self.task)

    def on_update(self, r: harness.RunResult, event: str):
        self.current, self.current_event = r, event

    def header(self) -> Panel:
        a, i = self.args, self.info
        n_sc = len(a.scenarios)
        t = Table.grid(padding=(0, 2))
        t.add_column(style="dim")
        t.add_column()
        t.add_column(style="dim")
        t.add_column()
        t.add_row("Model", i["model"], "Date", dt.date.today().isoformat())
        t.add_row("Context", f"{i['n_ctx']:,} tokens / slot", "Plan",
                  f"{n_sc} scenarios × 2 modes × {a.repeats} repeat{'s' if a.repeats > 1 else ''}"
                  + (f" + fix runs" if a.with_fix else "") + (f", +{a.extra_tools} tools" if a.extra_tools else ""))
        t.add_row("Tools", f"{i['n_tools']} native tools vs 2 (search_tools, execute)", "Elapsed",
                  f"{fmt_s(time.time() - self.t0)}  (est. ~{fmt_s(self.est_s)})")
        return Panel(t, title="[bold]MCP vs Code Mode[/] · local LLM benchmark", title_align="left",
                     border_style="cyan", width=WIDTH)

    def table(self) -> Table:
        return results_table(self.results, self.info["n_ctx"])

    def live_panel(self) -> Panel:
        r = self.current
        if r is None:
            body = Text("idle", style="dim")
        else:
            body = Text()
            body.append(f"{r.scenario} · {r.mode}", style="bold magenta")
            body.append(f"   round trips {r.round_trips}   tool calls {r.tool_calls}   ")
            body.append(f"prompt {r.prompt_tokens_total:,}  completion {r.completion_tokens_total:,}  ")
            body.append(f"total {r.total_tokens:,}", style="bold")
            body.append("\n")
            pct = 100 * r.peak_prompt_tokens / self.info["n_ctx"]
            body.append("peak context ")
            body.append(ctx_bar(pct, 16), style="red" if pct > 90 else "yellow" if pct > 60 else "green")
            body.append(f"  ({r.peak_prompt_tokens:,} tok)   {fmt_s(r.seconds)}   ")
            body.append(self.current_event, style="dim")
        return Panel(body, title="current run", title_align="left", border_style="dim", width=WIDTH)

    def __rich__(self):
        return Group(self.header(), self.table(), self.progress, self.live_panel())


def results_table(results: list, n_ctx: int) -> Table:
    t = Table(box=box.SIMPLE_HEAVY, width=WIDTH, header_style="bold cyan", pad_edge=False)
    t.add_column("Scenario", min_width=22)
    t.add_column("Mode", width=7)
    t.add_column("Total tokens", justify="right", width=13)
    t.add_column("Peak context", width=16)
    t.add_column("Round trips", justify="right", width=11)
    t.add_column("Time", justify="right", width=7)
    t.add_column("Result", min_width=12)
    for r in results:
        skipped = r.error.startswith("skipped")
        pct = r.peak_context_percent
        t.add_row(label(r), r.mode, "-" if skipped else f"{r.total_tokens:,}",
                  "-" if skipped else Text(ctx_bar(pct), style="red" if pct > 90 else "yellow" if pct > 60 else "green"),
                  "-" if skipped else str(r.round_trips), "-" if skipped else fmt_s(r.seconds), result_cell(r))
    return t


# ----------------------------------------------------------------------------- summaries


def scored(results):
    return [r for r in results if not r.error.startswith("skipped")]


def per_scenario(results: list) -> list[dict]:
    rows = []
    order = list(dict.fromkeys(r.scenario for r in results))
    for sc in order:
        by_mode = {m: [r for r in scored(results) if r.scenario == sc and r.mode == m] for m in MODES}
        if not all(by_mode.values()):
            rows.append({"scenario": sc, "skipped": True,
                         "reason": next((r.error for r in results if r.scenario == sc), "")})
            continue
        row = {"scenario": sc, "skipped": False}
        for m, rs in by_mode.items():
            row[f"{m}_tokens"] = round(statistics.mean(r.total_tokens for r in rs))
            row[f"{m}_peak"] = max(r.peak_context_percent for r in rs)
            row[f"{m}_correct"] = sum(bool(r.correct) for r in rs)
            row[f"{m}_n"] = len(rs)
            row[f"{m}_overflow"] = sum(r.error == "context_overflow" for r in rs)
            row[f"{m}_secs"] = statistics.mean(r.seconds for r in rs)
        row["delta_pct"] = 100 * (row["code_tokens"] - row["native_tokens"]) / row["native_tokens"] \
            if row["native_tokens"] else 0.0
        rows.append(row)
    return rows


def headline(rows: list, results: list) -> list[str]:
    done = [r for r in rows if not r["skipped"]]
    if not done:
        return ["No scenario completed in both modes; nothing to compare."]
    nat = sum(r["native_tokens"] for r in done)
    code = sum(r["code_tokens"] for r in done)
    k_nat = sum(r.error == "context_overflow" for r in scored(results) if r.mode == "native")
    k_code = sum(r.error == "context_overflow" for r in scored(results) if r.mode == "code")
    acc_n = sum(r["native_correct"] for r in done), sum(r["native_n"] for r in done)
    acc_c = sum(r["code_correct"] for r in done), sum(r["code_n"] for r in done)
    pct = 100 * (nat - code) / nat if nat else 0.0
    word = "fewer" if pct >= 0 else "more"
    lines = [f"Code Mode used {abs(pct):.0f}% {word} tokens across {len(done)} scenario{'s' * (len(done) != 1)} "
             f"({code:,} vs {nat:,}); native overflowed context in {k_nat} run{'s' * (k_nat != 1)}"
             + (f", Code Mode in {k_code}" if k_code else "") + "."]
    lines.append(f"Correct diagnoses: native {acc_n[0]}/{acc_n[1]}, Code Mode {acc_c[0]}/{acc_c[1]}.")
    worse = [r["scenario"] for r in done if r["code_tokens"] > r["native_tokens"]]
    if worse:
        lines.append("Code Mode used MORE tokens in: " + ", ".join(worse) + ".")
    lost = [r["scenario"] for r in done if r["code_correct"] < r["native_correct"]]
    if lost:
        lines.append("Code Mode was less accurate in: " + ", ".join(lost) + ".")
    code_err = sorted({f"{r.scenario} ({r.error})" for r in scored(results) if r.mode == "code" and r.error})
    if code_err:
        lines.append("Code Mode errors: " + ", ".join(code_err) + ".")
    if k_nat:
        lines.append("Note: overflowed native runs stopped early, so their token totals are lower bounds.")
    for r in rows:
        if r["skipped"]:
            lines.append(f"Not compared: {r['scenario']} ({r['reason']}).")
    return lines


def summary_table(rows: list) -> Table:
    t = Table(title="Per-scenario summary (mean over repeats)", title_justify="left", title_style="bold",
              box=box.SIMPLE_HEAVY, width=WIDTH, header_style="bold cyan", pad_edge=False)
    t.add_column("Scenario", min_width=20, no_wrap=True)
    for col, j in [("Native tokens", "right"), ("Code tokens", "right"), ("Δ tokens", "right"),
                   ("Ctx native", "right"), ("Ctx code", "right"), ("Native ✓", "center"), ("Code ✓", "center")]:
        t.add_column(col, justify=j, no_wrap=True)
    for r in rows:
        if r["skipped"]:
            t.add_row(r["scenario"], Text("skipped", style="dim"), "", "", "", "", "", "")
            continue
        d = r["delta_pct"]
        t.add_row(r["scenario"], f"{r['native_tokens']:,}", f"{r['code_tokens']:,}",
                  Text(f"{d:+.0f}%", style="green" if d < 0 else "red"),
                  f"{r['native_peak']:.0f}%" + (" ⚠" if r["native_overflow"] else ""),
                  f"{r['code_peak']:.0f}%" + (" ⚠" if r["code_overflow"] else ""),
                  f"{r['native_correct']}/{r['native_n']}", f"{r['code_correct']}/{r['code_n']}")
    return t


def scaling_table(scaling: list, n_ctx: int) -> Table:
    t = Table(title="Tool-definition tokens vs tool count (sent with every request)", title_justify="left",
              title_style="bold", box=box.SIMPLE_HEAVY, width=WIDTH, header_style="bold cyan", pad_edge=False)
    for col in ["Extra tools", "Native tools", "Native def tokens", "% of context", "Code Mode def tokens",
                "% of context"]:
        t.add_column(col, justify="right")
    for s in scaling:
        t.add_row(str(s["extra_tools"]), str(s["native_n_tools"]), f"{s['native_tokens']:,}",
                  f"{100 * s['native_tokens'] / n_ctx:.0f}%", f"{s['code_tokens']:,}",
                  f"{100 * s['code_tokens'] / n_ctx:.1f}%")
    return t


def markdown_summary(info, args, results, rows, scaling, lines) -> str:
    md = [f"# MCP vs Code Mode results", "",
          f"- Model: `{info['model']}`, context {info['n_ctx']:,} tokens/slot",
          f"- Date: {dt.datetime.now().isoformat(timespec='minutes')}",
          f"- Scenarios: {', '.join(args.scenarios)}; repeats: {args.repeats}; extra tools: {args.extra_tools}",
          "", "## Headline", ""] + [f"- {l}" for l in lines]
    md += ["", "## Runs", "", "| Scenario | Mode | Total tokens | Peak context | Round trips | Time | Result |",
           "|---|---|---:|---:|---:|---:|---|"]
    for r in results:
        res = result_cell(r).plain
        md.append(f"| {label(r)} | {r.mode} | {r.total_tokens:,} | {r.peak_context_percent:.0f}% | {r.round_trips} "
                  f"| {fmt_s(r.seconds)} | {res} |")
    md += ["", "## Per-scenario summary", "",
           "| Scenario | Native tokens | Code tokens | Δ | Peak ctx native | Peak ctx code | Native ✓ | Code ✓ |",
           "|---|---:|---:|---:|---:|---:|:-:|:-:|"]
    for r in rows:
        if r["skipped"]:
            md.append(f"| {r['scenario']} | {r['reason']} | | | | | | |")
        else:
            md.append(f"| {r['scenario']} | {r['native_tokens']:,} | {r['code_tokens']:,} | {r['delta_pct']:+.0f}% | "
                      f"{r['native_peak']:.0f}% | {r['code_peak']:.0f}% | {r['native_correct']}/{r['native_n']} | "
                      f"{r['code_correct']}/{r['code_n']} |")
    if scaling:
        md += ["", "## Tool-definition scaling", "",
               "| Extra tools | Native tools | Native def tokens | % ctx | Code Mode def tokens | % ctx |",
               "|---:|---:|---:|---:|---:|---:|"]
        for s in scaling:
            md.append(f"| {s['extra_tools']} | {s['native_n_tools']} | {s['native_tokens']:,} | "
                      f"{100 * s['native_tokens'] / info['n_ctx']:.0f}% | {s['code_tokens']:,} | "
                      f"{100 * s['code_tokens'] / info['n_ctx']:.1f}% |")
    md += ["", "## Final answers", ""]
    for r in results:
        if r.final_answer:
            md += [f"**{label(r)} / {r.mode}** ({'correct' if r.correct else 'incorrect'})", "",
                   "> " + r.final_answer.strip().replace("\n", "\n> "), ""]
    return "\n".join(md) + "\n"


def write_chart(path: Path, rows: list, scaling: list, n_ctx: int):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    done = [r for r in rows if not r["skipped"]]
    fig, axes = plt.subplots(1, 2 if scaling else 1, figsize=(13 if scaling else 8, 4.8))
    axes = axes if scaling else [axes]
    ax = axes[0]
    xs = range(len(done))
    w = 0.38
    ax.bar([x - w / 2 for x in xs], [r["native_tokens"] for r in done], w, label="native (all tools)",
           color="#4C72B0")
    ax.bar([x + w / 2 for x in xs], [r["code_tokens"] for r in done], w, label="Code Mode", color="#DD8452")
    for x, r in zip(xs, done):
        if r["native_overflow"]:
            ax.text(x - w / 2, r["native_tokens"], "overflow", ha="center", va="bottom", fontsize=8, color="#B00")
    ax.set_xticks(list(xs), [r["scenario"] for r in done], rotation=20, ha="right")
    ax.set_ylabel("total tokens per run (prompt + completion)")
    ax.set_title("Total tokens per scenario")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    if scaling:
        ax2 = axes[1]
        n = [s["native_n_tools"] for s in scaling]
        ax2.plot(n, [s["native_tokens"] for s in scaling], "o-", label="native tool definitions", color="#4C72B0")
        ax2.plot(n, [s["code_tokens"] for s in scaling], "o-", label="Code Mode tool definitions", color="#DD8452")
        ax2.axhline(n_ctx, ls="--", color="#B00", lw=1, label=f"context window ({n_ctx:,})")
        ax2.set_xlabel("number of tools available")
        ax2.set_ylabel("tokens")
        ax2.set_title("Tool-definition tokens vs tool count")
        ax2.legend()
        ax2.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)


# ----------------------------------------------------------------------------- main


def server_alive(llm) -> bool:
    try:
        llm.http.get(f"{llm.base_url}/health", timeout=5).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


def main():
    args = parse_args()
    console = Console(width=WIDTH, no_color=args.plain, highlight=False)
    out_dir = (PROJECT_DIR / args.export_dir).resolve()
    tdir = out_dir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    if args.rescore:
        return rescore(console, args, out_dir)

    # 1. llama-server
    llm = harness.LLM()
    try:
        srv = llm.probe()
    except (httpx.HTTPError, KeyError, IndexError) as e:
        console.print(f"[bold red]llama-server is not reachable at {llm.base_url}[/] ({type(e).__name__}: {e})")
        console.print("Start it yourself (this tool never starts it), e.g.: "
                      "llama-server -m gpt-oss-20b-MXFP4.gguf --jinja --port 8080")
        sys.exit(1)

    registry = tools.get_registry(args.extra_tools)
    info = {**srv, "n_tools": len(registry)}

    # plan
    plan = [(sc, m, r, False) for sc in args.scenarios for r in range(1, args.repeats + 1) for m in MODES]
    if args.with_fix:
        plan += [(sc, m, 1, True) for sc in B["fix_scenarios"] for m in MODES]
    n_native = sum(p[1] == "native" for p in plan)
    est = n_native * B["est_native_s"] + (len(plan) - n_native) * B["est_code_s"] + 2 * len(plan) * B["est_reset_s"]

    console.print(f"[dim]llama-server ok: {info['model']}, n_ctx {info['n_ctx']}. "
                  f"{len(plan)} runs planned, estimated runtime ~{fmt_s(est)}.[/]")
    for old in tdir.glob("*.jsonl"):
        old.unlink()

    dash = Dashboard(info, args, len(plan), est)
    scaling: list[dict] = []
    interrupted = False
    with Live(dash, console=console, refresh_per_second=4, transient=False, vertical_overflow="visible") as live:
        try:
            dash.step("bringing up docker stack (build on first run)")
            injector.up()

            if not args.no_scaling:
                dash.step("measuring tool-definition scaling")
                for n in B["scaling_extra_tools"]:
                    reg = tools.get_registry(n)
                    probe = [{"role": "user", "content": harness.USER_PROMPT}]
                    scaling.append({"extra_tools": n, "native_n_tools": len(reg),
                                    "native_tokens": llm.tool_definition_tokens(harness.native_tool_defs(reg), probe),
                                    "code_tokens": llm.tool_definition_tokens(harness.CODE_TOOL_DEFS, probe)})

            for sc, mode, rep, fix in plan:
                tag = f"{sc}{'+fix' if fix else ''} · {mode}"
                if sc == "oom" and injector.mem_available_gb() < CFG["injection"]["oom_min_free_ram_gb"]:
                    msg = (f"skipped: host MemAvailable {injector.mem_available_gb():.1f} GB < "
                           f"{CFG['injection']['oom_min_free_ram_gb']} GB")
                    live.console.print(f"[yellow]WARNING[/] oom {mode}: {msg}")
                    dash.run_done(harness.RunResult(scenario=sc, mode=mode, repeat=rep, extra_tools=args.extra_tools,
                                                    error=msg))
                    continue
                dash.step(f"{tag}: resetting stack")
                if not injector.reset():
                    live.console.print(f"[red]stack did not become healthy before {tag}; recording error[/]")
                    dash.run_done(harness.RunResult(scenario=sc, mode=mode, repeat=rep, extra_tools=args.extra_tools,
                                                    error="reset_failed"))
                    continue
                dash.step(f"{tag}: injecting failure")
                try:
                    injector.inject(sc)
                except injector.SkipScenario as e:
                    dash.run_done(harness.RunResult(scenario=sc, mode=mode, repeat=rep, extra_tools=args.extra_tools,
                                                    error=f"skipped: {e}"))
                    continue
                dash.step(f"{tag}: running {mode}")
                fname = f"{sc}{'_fix' if fix else ''}_{mode}_x{args.extra_tools}_r{rep}.jsonl"
                res = harness.run_agent(llm, mode, sc, registry, extra_tools=args.extra_tools, repeat=rep,
                                        with_fix=fix, transcript_path=tdir / fname, on_update=dash.on_update)
                dash.step(f"{tag}: scoring")
                res.correct = scoring.score(sc, res.final_answer)
                if fix:
                    res.fixed = injector.http_status() == 200
                dash.run_done(res)
                if res.error.startswith(("ConnectError", "RemoteProtocolError", "ReadError")) and not server_alive(llm):
                    live.console.print(f"[bold red]llama-server at {llm.base_url} stopped responding during {tag}. "
                                       "Stopping the benchmark and exporting partial results; restart llama-server "
                                       "and re-run.[/]")
                    break
                dash.step(f"{tag}: resetting stack")
                injector.reset()
            dash.step("done")
        except KeyboardInterrupt:
            interrupted = True
            dash.step("interrupted, restoring stack")
        finally:
            try:
                ok = injector.reset()
            except KeyboardInterrupt:
                ok = False
            dash.step("stack restored" if ok else "WARNING: stack reset failed")

    if interrupted:
        console.print("[yellow]Interrupted: exporting partial results.[/]")
    export(console, args, info, dash.results, scaling, out_dir)


def load_results(out_dir: Path) -> tuple[dict, list]:
    """Results of the last run: from run_meta.json, or rebuilt from the transcripts' end records."""
    meta_path = out_dir / "run_meta.json"
    fields = harness.RunResult.__dataclass_fields__
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        return meta, [harness.RunResult(**{k: v for k, v in r.items() if k in fields}) for r in meta["results"]]
    results = []
    for f in (out_dir / "transcripts").glob("*.jsonl"):
        for line in f.open():
            e = json.loads(line)
            if e["type"] == "end":
                results.append(harness.RunResult(**{k: v for k, v in e["result"].items() if k in fields}))
    order = {s: i for i, s in enumerate(injector.SCENARIOS)}
    results.sort(key=lambda r: (order.get(r.scenario.split("+")[0], 99), "+fix" in r.scenario, r.repeat,
                                MODES.index(r.mode)))
    return {}, results


def rescore(console, args, out_dir: Path):
    meta, results = load_results(out_dir)
    if not results:
        console.print(f"[red]no saved results in {out_dir}[/]")
        sys.exit(1)
    changed = []
    for r in results:
        if r.error.startswith("skipped"):
            continue
        new = scoring.score(r.scenario, r.final_answer)
        if new != r.correct:
            changed.append(f"{r.scenario}/{r.mode}: {r.correct} -> {new}")
        r.correct = new
    info, scaling = meta.get("info"), meta.get("scaling", [])
    if not info:  # older run without run_meta.json: take model/n_ctx from the live server if it is up
        llm = harness.LLM()
        try:
            info = llm.probe()
            m = [{"role": "user", "content": harness.USER_PROMPT}]
            for n in B["scaling_extra_tools"]:
                reg = tools.get_registry(n)
                scaling.append({"extra_tools": n, "native_n_tools": len(reg),
                                "native_tokens": llm.tool_definition_tokens(harness.native_tool_defs(reg), m),
                                "code_tokens": llm.tool_definition_tokens(harness.CODE_TOOL_DEFS, m)})
        except httpx.HTTPError:
            r0 = next(r for r in results if r.peak_context_percent)
            info = {"model": "unknown", "n_ctx": round(r0.peak_prompt_tokens * 100 / r0.peak_context_percent)}
        info["n_tools"] = len(tools.get_registry(results[0].extra_tools))
    args.scenarios = meta.get("scenarios") or list(dict.fromkeys(r.scenario.split("+")[0] for r in results))
    args.repeats = meta.get("repeats") or max(r.repeat for r in results)
    args.extra_tools = results[0].extra_tools
    console.print(f"Re-scored {len(results)} runs with the current scoring.py: "
                  + ("; ".join(changed) if changed else "no changes"))
    console.print(results_table(results, info["n_ctx"]))
    export(console, args, info, results, scaling, out_dir)


def export(console, args, info, results, scaling, out_dir: Path):
    import pandas as pd

    rows = per_scenario(results)
    lines = headline(rows, results)
    renderables = [summary_table(rows)]
    if scaling:
        renderables.append(scaling_table(scaling, info["n_ctx"]))
    head = Panel("\n".join(lines), title="[bold]Headline (computed from this run)", title_align="left",
                 border_style="green", width=WIDTH)

    console.print()
    for r in renderables:
        console.print(r)
    console.print(head)

    # plain-text copy of the final run table + summaries
    buf = Console(file=io.StringIO(), width=WIDTH, no_color=True, color_system=None, highlight=False)
    buf.print(f"MCP vs Code Mode · {info['model']} · n_ctx {info['n_ctx']} · {dt.date.today().isoformat()}")
    buf.print(results_table(results, info["n_ctx"]))
    for r in renderables:
        buf.print(r)
    buf.print(Panel("\n".join(lines), title="Headline", title_align="left", box=box.ASCII, width=WIDTH))

    paths = {
        "csv": out_dir / "results.csv",
        "summary": out_dir / "summary.md",
        "text": out_dir / "terminal_summary.txt",
        "chart": out_dir / "chart.png",
        "transcripts": out_dir / "transcripts",
    }
    df = pd.DataFrame([r.row() for r in results], columns=list(harness.RunResult.__dataclass_fields__))
    df[CSV_COLUMNS].to_csv(paths["csv"], index=False)
    paths["summary"].write_text(markdown_summary(info, args, results, rows, scaling, lines))
    (out_dir / "run_meta.json").write_text(json.dumps(
        {"info": info, "scenarios": args.scenarios, "repeats": args.repeats, "scaling": scaling,
         "results": [harness.asdict(r) for r in results]}, indent=1, default=str))
    paths["text"].write_text(buf.file.getvalue())
    try:
        write_chart(paths["chart"], rows, scaling, info["n_ctx"])
    except Exception as e:
        console.print(f"[yellow]chart not written: {e}[/]")

    t = Table.grid(padding=(0, 2))
    for k, p in paths.items():
        t.add_row(Text(k, style="dim"), str(p.relative_to(PROJECT_DIR)) + ("/" if p.is_dir() else ""))
    console.print(Panel(t, title="Exports", title_align="left", border_style="dim", width=WIDTH))


if __name__ == "__main__":
    main()
