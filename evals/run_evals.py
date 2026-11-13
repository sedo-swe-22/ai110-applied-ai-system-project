"""Evaluation harness for the PawPal+ agent.

Runs the cases in evals/cases.yaml through the real agent and real Gemini, checks
the *final schedule* in code (not just the reply text), and prints a summary.

    python evals/run_evals.py                      # all cases, few-shot on (default)
    python evals/run_evals.py --compare-few-shot   # every case with few-shot on AND off
    python evals/run_evals.py --compare-rag        # every case with retrieval (RAG) on AND off
    python evals/run_evals.py --tag rag --compare-rag   # only the retrieval cases, both ways
    python evals/run_evals.py --tag guardrail      # only cases with this tag
    python evals/run_evals.py --only add_simple    # one case (repeatable)
    python evals/run_evals.py --resume evals/runs/<file>.json   # continue a cut-off run

Every run is saved to evals/runs/<timestamp>.json after each case (so a run cut
off by the free-tier quota can be resumed) and appended to evals/results.md.
Cases that are blocked by a guardrail never call Gemini.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

import yaml  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from agent import AgentResult, PawPalAgent, rag_enabled_by_default  # noqa: E402
from agent_tools import TOOL_REGISTRY  # noqa: E402
from llm_client import GeminiClient, LLMClient, LLMError  # noqa: E402
from logging_config import setup_agent_logger  # noqa: E402
from pawpal_cli import STARTS  # noqa: E402
from pawpal_system import Owner, Scheduler  # noqa: E402

EVALS_DIR = PROJECT_DIR / "evals"
DEFAULT_CASES = EVALS_DIR / "cases.yaml"
RESULTS_MD = EVALS_DIR / "results.md"
RUNS_DIR = EVALS_DIR / "runs"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _matches(task_view: dict, want: dict) -> bool:
    words = [w.lower() for w in want.get("description_any", [])]
    if want.get("pet") and task_view["pet"].lower() != str(want["pet"]).lower():
        return False
    if words and not any(w in task_view["description"].lower() for w in words):
        return False
    for key in ("time", "due_date", "frequency"):
        if key in want and str(want[key]) != task_view[key]:
            return False
    if "time_not" in want and task_view["time"] == str(want["time_not"]):
        return False
    if "completed" in want and bool(want["completed"]) != task_view["completed"]:
        return False
    return True


def as_list(value: Any) -> list:
    return [] if value is None else value if isinstance(value, list) else [value]


def _task_views(owner: Owner) -> list[dict]:
    return [
        {"pet": p.name, "description": t.description, "time": t.time, "due_date": t.due_date.isoformat(),
         "frequency": t.frequency, "completed": t.completed}
        for p in owner.pets for t in p.tasks
    ]


def evaluate(expect: dict, result: AgentResult, before: dict, owner: Owner, rag: bool = True) -> list[dict]:
    """Return one {check, ok, detail} entry per check. A case passes when all are ok."""
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    after = owner.to_dict()
    views = _task_views(owner)
    reply = (result.reply or "").lower()
    succeeded_writes = [s.tool for s in result.steps
                        if s.ok and s.tool in TOOL_REGISTRY and TOOL_REGISTRY[s.tool].writes]

    add("run did not fail", not result.failed, result.error or "")

    if "blocked" in expect:
        want = expect["blocked"]
        if want is False:
            add("not blocked", not result.blocked, f"blocked as {result.block_reason}" if result.blocked else "")
        else:
            add(f"blocked as {want}", result.blocked and result.block_reason == want,
                f"got {result.block_reason if result.blocked else 'not blocked'}")
    if expect.get("unchanged"):
        add("data unchanged", after == before, "pets or tasks changed" if after != before else "")
    for want in expect.get("task_exists", []) or []:
        found = any(_matches(v, want) for v in views)
        add(f"task exists {want}", found, "" if found else "no matching task")
    if "total_tasks_change" in expect:
        delta = len(views) - sum(len(p["tasks"]) for p in before["pets"])
        add(f"task count +{expect['total_tasks_change']}", delta == expect["total_tasks_change"], f"changed by {delta}")
    if expect.get("no_conflicts"):
        conflicts = Scheduler(owner).detect_conflicts()
        add("no conflicts", not conflicts, "; ".join(conflicts))
    if "pending_removal" in expect:
        want = expect["pending_removal"]
        words = [w.lower() for w in want.get("description_any", [])]
        found = any(
            a.pet_name.lower() == str(want.get("pet", a.pet_name)).lower()
            and any(w in a.task["description"].lower() for w in words)
            for a in result.pending_actions
        )
        add(f"deletion pending {want}", found, "" if found else "no matching pending deletion")
    if expect.get("no_write_tools"):
        add("no write tools succeeded", not succeeded_writes, ", ".join(succeeded_writes))
    if "reply_mentions_any" in expect:
        words = [str(w).lower() for w in expect["reply_mentions_any"]]
        add(f"reply mentions one of {expect['reply_mentions_any']}", any(w in reply for w in words))
    if expect.get("refuses_with_vet"):
        ok = result.blocked or (after == before and ("vet" in reply or "veterinar" in reply))
        add("refused and pointed to a vet", ok, "" if ok else "changed data or no vet mentioned")
    for want in expect.get("task_count_for", []) or []:
        words = [w.lower() for w in want.get("description_any", [])]
        n = sum(1 for v in views
                if v["pet"].lower() == str(want["pet"]).lower()
                and (not words or any(w in v["description"].lower() for w in words)))
        lo, hi = want.get("min", want.get("count", 0)), want.get("max", want.get("count"))
        add(f"task count for {want}", n >= lo and (hi is None or n <= hi), f"found {n}")
    # Checks that only make sense when retrieval is on (the tools do not exist otherwise).
    if rag:
        used = [s.tool for s in result.steps if s.ok]
        for name in as_list(expect.get("tool_called")):
            add(f"tool called: {name}", name in used, f"tools used: {', '.join(used) or 'none'}")
        if expect.get("citation_present"):
            add("reply cites a retrieved source", bool(result.citations) and any(
                c.lower() in (result.reply or "").lower() for c in result.citations),
                f"retrieved {len(result.citations)} source(s)")
        if expect.get("no_output_warnings"):
            add("no output-check warnings", not result.output_warnings, "; ".join(result.output_warnings))
    return checks


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

KNOWN_CHECKS = {"blocked", "unchanged", "task_exists", "total_tasks_change", "no_conflicts",
                "pending_removal", "no_write_tools", "reply_mentions_any", "refuses_with_vet",
                "task_count_for", "tool_called", "citation_present", "no_output_warnings"}
KNOWN_COUNT_FIELDS = {"pet", "description_any", "count", "min", "max"}
KNOWN_TASK_FIELDS = {"pet", "description_any", "time", "time_not", "due_date", "frequency", "completed"}


def load_cases(path: Path = DEFAULT_CASES) -> dict:
    """Load and validate cases.yaml. A typo in a check name would silently skip that
    check (and make the case pass too easily), so unknown names are an error."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    ids = [c["id"] for c in data["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("case ids in cases.yaml must be unique")
    for case in data["cases"]:
        expect = case.get("expect") or {}
        if not expect:
            raise ValueError(f"case {case['id']!r} has no checks under 'expect'")
        unknown = set(expect) - KNOWN_CHECKS
        if unknown:
            raise ValueError(f"case {case['id']!r}: unknown check(s) {sorted(unknown)}")
        for item in expect.get("task_exists", []) or []:
            bad = set(item) - KNOWN_TASK_FIELDS
            if bad:
                raise ValueError(f"case {case['id']!r}: unknown task field(s) {sorted(bad)}")
        for item in expect.get("task_count_for", []) or []:
            bad = set(item) - KNOWN_COUNT_FIELDS
            if bad:
                raise ValueError(f"case {case['id']!r}: unknown task_count_for field(s) {sorted(bad)}")
        if case.get("start", "demo") not in STARTS:
            raise ValueError(f"case {case['id']!r}: unknown start {case['start']!r} (use one of {sorted(STARTS)})")
    return data


def run_case(case: dict, client: LLMClient, today: date, few_shot: bool, logger=None,
             rag: Optional[bool] = None) -> dict:
    rag = rag_enabled_by_default() if rag is None else rag
    owner = STARTS[case.get("start", "demo")](today)
    before = owner.to_dict()
    started = time.perf_counter()
    result = PawPalAgent(owner, client, logger=logger, today=today, few_shot=few_shot, rag=rag).run(case["request"])
    checks = evaluate(case.get("expect", {}), result, before, owner, rag=rag)
    return {
        "id": case["id"],
        "tags": case.get("tags", []),
        "request": case["request"],
        "few_shot": few_shot,
        "rag": rag,
        "passed": all(c["ok"] for c in checks),
        "failed_checks": [f"{c['check']}: {c['detail']}".rstrip(": ") for c in checks if not c["ok"]],
        "confidence": result.confidence,
        "model_confidence": result.model_confidence,
        "self_check": result.checks_passed,
        "blocked": result.block_reason if result.blocked else None,
        "error": result.error,
        "tools": [s.tool for s in result.steps],
        "llm_calls": result.llm_calls,
        "seconds": round(time.perf_counter() - started, 1),
        "run_id": result.run_id,
        "citations": list(result.citations),
        "reply": (result.reply or "")[:300],
    }


def shown(path: Path) -> str:
    """A path relative to the project when possible (no home-folder paths in output)."""
    try:
        return str(Path(path).resolve().relative_to(PROJECT_DIR))
    except ValueError:
        return str(path)


def git_commit() -> str:
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJECT_DIR,
                                capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=PROJECT_DIR,
                               capture_output=True, text=True).stdout.strip()
        return f"{commit} (with uncommitted changes)" if dirty else commit
    except Exception:
        return "unknown"


def resolve_modes(modes: list) -> list[tuple[bool, bool]]:
    """Turn bools / (few_shot, rag) pairs into concrete (few_shot, rag) pairs."""
    default_rag = rag_enabled_by_default()
    resolved = []
    for m in modes:
        few_shot, rag = (m, None) if isinstance(m, bool) else m
        resolved.append((few_shot, default_rag if rag is None else rag))
    return resolved


def mode_tag(mode: tuple[bool, bool]) -> str:
    few_shot, rag = mode
    return f"few-shot {'on ' if few_shot else 'off'} · RAG {'on ' if rag else 'off'}"


def mode_label(mode: tuple[bool, bool], modes: list[tuple[bool, bool]]) -> str:
    """Name a mode by the setting(s) that differ between the modes of this run."""
    few_shot_varies = len({m[0] for m in modes}) > 1
    rag_varies = len({m[1] for m in modes}) > 1
    parts = []
    if few_shot_varies or not rag_varies:
        parts.append(f"Few-shot {'on' if mode[0] else 'off'}")
    if rag_varies:
        parts.append(f"RAG {'on' if mode[1] else 'off'}")
    return ", ".join(parts)


def run_all(
    cases: list[dict],
    client: LLMClient,
    today: date,
    modes: list,
    run_file: Path,
    meta: dict,
    done: Optional[list[dict]] = None,
    delay_s: float = 0.0,
    logger=None,
    sleep: Callable[[float], None] = time.sleep,
    out=sys.stdout,
) -> tuple[list[dict], bool]:
    """Run every (mode, case) not already in `done`. Returns (records, completed).

    A mode is `few_shot` (a bool) or a `(few_shot, rag)` pair; rag None means the agent default.
    """
    records = list(done or [])
    finished = {(r["few_shot"], r.get("rag", True), r["id"]) for r in records}
    for mode in resolve_modes(modes):
        few_shot, rag = mode
        for case in cases:
            if (few_shot, rag, case["id"]) in finished:
                continue
            record = run_case(case, client, today, few_shot, logger, rag=rag)
            records.append(record)
            _save(run_file, meta, records)
            mark = "PASS" if record["passed"] else "FAIL"
            conf = f"{record['confidence']:.2f}" if record["confidence"] is not None else " n/a"
            print(f"  [{mode_tag(mode)}] {mark}  {case['id']:<26} "
                  f"conf {conf}  tools {len(record['tools'])}", file=out)
            for failure in record["failed_checks"]:
                print(f"        ✗ {failure}", file=out)
            if record["error"] and "daily Gemini quota" in record["error"]:
                print("\nStopped: the free daily Gemini quota is used up. Resume tomorrow with:\n"
                      f"  python evals/run_evals.py --resume {shown(run_file)}", file=out)
                return records, False
            if record["llm_calls"] and delay_s:
                sleep(delay_s)
    return records, True


def _save(run_file: Path, meta: dict, records: list[dict]) -> None:
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text(json.dumps({"meta": meta, "records": records}, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------

def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 2) if values else None


def record_mode(record: dict) -> tuple[bool, bool]:
    return record["few_shot"], record.get("rag", True)


def record_modes(records: list[dict]) -> list[tuple[bool, bool]]:
    """The distinct modes in a run, in the order they first appear."""
    return list(dict.fromkeys(record_mode(r) for r in records))


def summarize(records: list[dict]) -> dict:
    passed = [r for r in records if r["passed"]]
    tags: dict[str, list[int]] = {}
    for r in records:
        for tag in r["tags"]:
            tags.setdefault(tag, [0, 0])
            tags[tag][0] += r["passed"]
            tags[tag][1] += 1
    conf = lambda rs: _mean([r["confidence"] for r in rs if r["confidence"] is not None])  # noqa: E731
    reached = [r for r in records if r["llm_calls"]]
    return {
        "passed": len(passed),
        "total": len(records),
        "rate": round(100 * len(passed) / len(records)) if records else 0,
        "by_tag": {t: f"{p}/{n}" for t, (p, n) in sorted(tags.items())},
        "confidence_all": conf(records),
        "confidence_passed": conf(passed),
        "confidence_failed": conf([r for r in records if not r["passed"]]),
        "avg_tool_calls": _mean([len(r["tools"]) for r in reached]),
        "avg_llm_calls": _mean([r["llm_calls"] for r in reached]),
        "gemini_cases": len(reached),
    }


def summary_lines(label: str, s: dict) -> list[str]:
    fmt = lambda v: "n/a" if v is None else f"{v:.2f}"  # noqa: E731
    return [
        f"**{label}: {s['passed']}/{s['total']} passed ({s['rate']}%)**",
        f"- By tag: " + ", ".join(f"{t} {v}" for t, v in s["by_tag"].items()),
        f"- Confidence: average {fmt(s['confidence_all'])}; on passed cases {fmt(s['confidence_passed'])}, "
        f"on failed cases {fmt(s['confidence_failed'])}",
        f"- Per Gemini case ({s['gemini_cases']} cases): {fmt(s['avg_tool_calls'])} tool calls, "
        f"{fmt(s['avg_llm_calls'])} model calls on average",
    ]


def results_markdown(meta: dict, records: list[dict], completed: bool) -> str:
    lines = [
        f"## Run {meta['started']} · `{meta['model']}` · commit `{meta['commit']}`",
        "",
        f"Today fixed to `{meta['today']}` · demo data · modes: {', '.join(meta['modes'])}"
        + ("" if completed else " · **incomplete (stopped early; resumable)**"),
        "",
    ]
    modes = record_modes(records)
    for mode in modes:
        mode_records = [r for r in records if record_mode(r) == mode]
        label = mode_label(mode, modes)
        lines += summary_lines(label, summarize(mode_records)) + [""]
        lines += ["| Case | Tags | Result | Confidence | Tools called | Failed checks |", "|---|---|---|---|---|---|"]
        for r in mode_records:
            conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "n/a"
            tools = " → ".join(r["tools"]) or ("blocked: " + r["blocked"] if r["blocked"] else "none")
            failed = "; ".join(r["failed_checks"]).replace("|", "\\|") or ""
            lines.append(f"| `{r['id']}` | {', '.join(r['tags'])} | {'✅ pass' if r['passed'] else '❌ fail'} "
                         f"| {conf} | {tools} | {failed} |")
        lines.append("")
    varies = "rag" if len({m[1] for m in modes}) > 1 else "few_shot"
    name = "RAG" if varies == "rag" else "Few-shot"
    key = (lambda r: r.get("rag", True)) if varies == "rag" else (lambda r: r["few_shot"])  # noqa: E731
    on = {r["id"]: r for r in records if key(r)}
    off = {r["id"]: r for r in records if not key(r)}
    both = [i for i in on if i in off]
    if both:
        lines += [f"### {name} on vs. off (same cases)", "",
                  "| Case | On | Off | Tool calls on / off |", "|---|---|---|---|"]
        for i in both:
            lines.append(f"| `{i}` | {'✅' if on[i]['passed'] else '❌'} | {'✅' if off[i]['passed'] else '❌'} "
                         f"| {len(on[i]['tools'])} / {len(off[i]['tools'])} |")
        lines.append("")
    lines.append(f"Raw results: `{meta['run_file']}`")
    lines.append("")
    return "\n".join(lines)


def append_results(md: str, path: Path = RESULTS_MD) -> None:
    header = ("# PawPal+ evaluation results\n\n"
              "Generated by `python evals/run_evals.py`. Each run checks the final schedule in code. "
              "Newest run last. Every number comes from a real run.\n\n")
    existing = path.read_text(encoding="utf-8") if path.exists() else header
    path.write_text(existing.rstrip() + "\n\n" + md, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None, client: Optional[LLMClient] = None,
         sleep: Callable[[float], None] = time.sleep, out=sys.stdout,
         results_md: Path = RESULTS_MD, runs_dir: Path = RUNS_DIR, cases_file: Path = DEFAULT_CASES) -> int:
    p = argparse.ArgumentParser(prog="run_evals", description="Evaluate the PawPal+ agent on predefined cases.")
    p.add_argument("--compare-few-shot", action="store_true", help="run every case with few-shot on and off")
    p.add_argument("--no-few-shot", action="store_true", help="run with few-shot off only")
    p.add_argument("--compare-rag", action="store_true", help="run every case with retrieval (RAG) on and off")
    p.add_argument("--no-rag", action="store_true", help="run with retrieval (RAG) off only")
    p.add_argument("--tag", action="append", default=[], help="only cases with this tag (repeatable)")
    p.add_argument("--only", action="append", default=[], help="only this case id (repeatable)")
    p.add_argument("--model", help="Gemini model (default: PAWPAL_MODEL or the built-in default)")
    p.add_argument("--resume", type=Path, help="continue a run saved in evals/runs/")
    p.add_argument("--delay", type=float, default=float(os.environ.get("EVAL_DELAY_SECONDS", 8)),
                   help="seconds to wait after each case that called Gemini (default 8, or EVAL_DELAY_SECONDS)")
    args = p.parse_args(argv)

    load_dotenv(PROJECT_DIR / ".env")
    data = load_cases(cases_file)
    today = date.fromisoformat(str(data["today"]))
    cases = data["cases"]
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]
    if args.tag:
        cases = [c for c in cases if set(args.tag) & set(c.get("tags", []))]
    if not cases:
        print("No cases match the filters.", file=out)
        return 2
    if args.compare_few_shot and args.compare_rag:
        print("Choose one comparison: --compare-few-shot or --compare-rag.", file=out)
        return 2
    few_shot_modes = [True, False] if args.compare_few_shot else [not args.no_few_shot]
    rag_modes = [True, False] if args.compare_rag else [False if args.no_rag else None]
    modes = [(f, r) for r in rag_modes for f in few_shot_modes]
    logger = setup_agent_logger()

    if client is None:
        try:
            client = GeminiClient(model=args.model, logger=logger)
        except LLMError as exc:
            print(f"Setup problem: {exc}", file=out)
            return 2

    done: list[dict] = []
    if args.resume:
        saved = json.loads(args.resume.read_text(encoding="utf-8"))
        meta, done, run_file = saved["meta"], saved["records"], args.resume
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_file = runs_dir / f"run_{stamp}.json"
        meta = {
            "started": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
            "model": getattr(client, "model", "?"),
            "commit": git_commit(),
            "today": today.isoformat(),
            "modes": [mode_tag(m).replace("  ", " ").strip() for m in resolve_modes(modes)],
            "run_file": shown(run_file),
        }

    print(f"Evaluating {len(cases)} case(s) × {len(modes)} mode(s) with {meta['model']} "
          f"(today = {today.isoformat()})", file=out)
    records, completed = run_all(cases, client, today, modes, run_file, meta, done, args.delay, logger, sleep, out)

    print("", file=out)
    shown_modes = record_modes(records)
    for mode in shown_modes:
        for line in summary_lines(mode_label(mode, shown_modes), summarize([r for r in records if record_mode(r) == mode])):
            print(line.replace("**", ""), file=out)
    if completed:
        append_results(results_markdown(meta, records, completed), results_md)
        print(f"\nResults appended to {shown(results_md)}; "
              f"raw results in {meta['run_file']}", file=out)
        return 0 if all(r["passed"] for r in records) else 1
    return 3


if __name__ == "__main__":
    sys.exit(main())
