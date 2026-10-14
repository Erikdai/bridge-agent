"""Run the task set in tasks/*.yaml and produce a results table.

Each task runs through the normal Agent entry point, so skills, the focus
guard and the confirmation gate all apply. A task may declare:

    id, task, start_url, sites, judge, interrupt, requires, skip

judge forms:  "summary contains <text>"   (case-insensitive; $HOSTNAME expands)
              "file exists <path>"        (relative to the run directory)
              anything else / missing     -> manual

interrupt: {after_s: 6, title: ..., body: ...} fires a Windows toast mid-run
to test focus stealing.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from .agent import Agent, RunResult
from .runtime import notify


@dataclass
class EvalRow:
    id: str
    status: str
    judge: str  # pass | fail | manual | skipped
    mode: str
    replay_steps: int
    llm_steps: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    seconds: float
    summary: str
    run_dir: str
    note: str = ""


def _judge(spec: str | None, result: RunResult) -> str:
    if not spec:
        return "manual"
    spec = spec.strip().replace("$HOSTNAME", socket.gethostname())
    low = spec.lower()
    if low.startswith("summary contains "):
        needle = spec[len("summary contains ") :].strip().strip('"')
        hay = (result.summary or "").lower()
        if needle.replace(",", "").isdigit():  # numbers: ignore thousands separators (6,912 == 6912)
            hay = hay.replace(",", "")
        return "pass" if needle.lower() in hay else "fail"
    if low.startswith("file exists "):
        rel = spec[len("file exists ") :].strip()
        return "pass" if result.run_dir and (result.run_dir / rel).exists() else "fail"
    return "manual"


def _requires_ok(req: str | None) -> tuple[bool, str]:
    if not req:
        return True, ""
    if req.lower().startswith("excel"):
        import glob

        for root in (r"C:\Program Files\Microsoft Office", r"C:\Program Files (x86)\Microsoft Office"):
            if glob.glob(os.path.join(root, "**", "EXCEL.EXE"), recursive=True):
                return True, ""
        return False, "Excel not installed"
    return True, ""


def run_eval(
    agent: Agent,
    tasks_file: str | Path,
    only: list[str] | None = None,
    use_skills: bool = True,
    out_dir: str | Path = "runs",
) -> list[EvalRow]:
    tasks = yaml.safe_load(Path(tasks_file).read_text(encoding="utf-8"))
    rows: list[EvalRow] = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for t in tasks:
        tid = t["id"]
        if only and tid not in only:
            continue
        ok, why = _requires_ok(t.get("requires"))
        if t.get("skip") or not ok:
            rows.append(EvalRow(tid, "skipped", "skipped", "-", 0, 0, 0, 0, 0, 0.0, "", "", why or str(t.get("skip"))))
            print(f"--- {tid}: skipped ({why or t.get('skip')})")
            continue
        print(f"\n=== {tid} ===")
        timer = None
        if t.get("interrupt"):
            it = t["interrupt"]
            timer = threading.Timer(
                float(it.get("after_s", 6)),
                notify,
                args=(it.get("title", "BridgeAgent eval"), it.get("body", "interrupt")),
            )
            timer.daemon = True
            timer.start()
        try:
            result = agent.run(
                t["task"], sites=t.get("sites") or [], start_url=t.get("start_url"), use_skills=use_skills
            )
        except Exception as e:  # keep the batch going
            rows.append(
                EvalRow(tid, "error", "fail", "-", 0, 0, 0, 0, 0, 0.0, f"{type(e).__name__}: {str(e)[:160]}", "")
            )
            print(f"!! {tid} crashed: {type(e).__name__}: {str(e)[:160]}")
            continue
        finally:
            if timer:
                timer.cancel()
        tok = result.tokens
        rows.append(
            EvalRow(
                tid,
                result.status,
                _judge(t.get("judge"), result),
                result.mode,
                result.replay_steps,
                len(result.steps),
                result.llm_calls,
                tok["input_tokens"],
                tok["output_tokens"],
                round(result.seconds, 1),
                (result.summary or "")[:200],
                str(result.run_dir),
            )
        )
    out = Path(out_dir) / f"eval-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(
        json.dumps([asdict(r) for r in rows], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out / "results.md").write_text(markdown_table(rows), encoding="utf-8")
    print(f"\nresults -> {out}")
    return rows


def markdown_table(rows: list[EvalRow]) -> str:
    head = "| task | status | judge | mode | replayed | LLM steps | LLM calls | input tok | output tok | time (s) |\n|---|---|---|---|---|---|---|---|---|---|\n"
    body = "".join(
        f"| {r.id} | {r.status} | {r.judge} | {r.mode} | {r.replay_steps} | {r.llm_steps} | {r.llm_calls} | "
        f"{r.input_tokens} | {r.output_tokens} | {r.seconds} |\n"
        for r in rows
    )
    done = [r for r in rows if r.status not in ("skipped",)]
    passed = sum(1 for r in done if r.judge == "pass")
    judged = sum(1 for r in done if r.judge in ("pass", "fail"))
    tot_in = sum(r.input_tokens for r in done)
    tot_s = sum(r.seconds for r in done)
    foot = (
        f"\n{len(done)} tasks run, {sum(1 for r in done if r.status == 'done')} reported done, "
        f"{passed}/{judged} passed the automatic judge, {tot_in} input tokens, {tot_s:.0f} s total.\n"
    )
    return head + body + foot
