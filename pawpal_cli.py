"""Command-line demo of the PawPal+ agent (the same agent the Streamlit app uses).

Examples:
    python -m pawpal_cli --data demo.json --reset-demo --today 2026-11-10 "Add a 9am walk for Max tomorrow"
    python -m pawpal_cli --data demo.json "What's on today?"
    python -m pawpal_cli --data demo.json --yes "Delete Mochi's grooming"

It prints the reply, the actions taken, every tool call, the self-check and the
confidence. Destructive actions ask y/N (or use --yes / --no).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional, Sequence

from dotenv import load_dotenv

from agent import AgentResult, PawPalAgent
from agent_tools import execute_pending
from llm_client import GeminiClient, LLMClient, LLMError, UnavailableClient
from logging_config import export_trace, log_event, setup_agent_logger
from pawpal_system import Owner, Pet, Task

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA = PROJECT_DIR / "data.json"


def demo_owner(today: date) -> Owner:
    """Made-up demo data (never use real personal data with the free tier)."""
    owner = Owner("Jordan")
    max_ = Pet("Max", "Beagle")
    mochi = Pet("Mochi", "Shiba Inu")
    owner.add_pet(max_)
    owner.add_pet(mochi)
    tomorrow = today + timedelta(days=1)
    max_.add_task(Task("Morning walk", "08:00", frequency="daily", due_date=today, priority="high"))
    max_.add_task(Task("Evening meds", "20:00", frequency="daily", due_date=today, priority="high"))
    mochi.add_task(Task("Breakfast", "07:30", frequency="daily", due_date=today))
    mochi.add_task(Task("Grooming", "10:00", frequency="weekly", due_date=tomorrow, priority="low"))
    max_.add_task(Task("Morning walk", "08:00", frequency="daily", due_date=tomorrow, priority="high"))
    return owner


def load_owner(path: Path, today: date, reset_demo: bool) -> Owner:
    if reset_demo or not path.exists():
        owner = demo_owner(today)
        owner.save_to_json(str(path))
        return owner
    return Owner.load_from_json(str(path))


def print_result(result: AgentResult, out=sys.stdout) -> None:
    w = lambda text="": print(text, file=out)  # noqa: E731
    w(f"PawPal+: {result.reply}")
    if result.blocked:
        w(f"\n[guardrail] request blocked ({result.block_reason}); Gemini was not called.")
        w(f"\nrun_id: {result.run_id}  (details in logs/agent.jsonl)")
        return
    if result.failed:
        w(f"\n[error] {result.error}  (all changes rolled back)")
    if result.actions_taken:
        w("\nActions taken:")
        for action in result.actions_taken:
            w(f"  - {action}")
    if result.steps:
        w("\nSteps:")
        for s in result.steps:
            args = json.dumps(s.args, ensure_ascii=False)
            status = "ok" if s.ok else f"error: {s.result.get('error')}"
            tag = " [blocked by guardrail]" if s.blocked else ""
            w(f"  {s.n}. {s.tool}({args}) -> {status}{tag}")
    if not result.failed:
        check = {True: "passed", False: "FAILED", None: "n/a (nothing changed)"}[result.checks_passed]
        w(f"\nSelf-check: {check}")
        for line in result.check_details:
            w(f"  {line}")
        if result.confidence is not None:
            reason = f" ({result.confidence_reason})" if result.confidence_reason else ""
            w(f"Confidence: {result.confidence:.2f}{reason}")
    for warning in result.output_warnings:
        w(f"[output check] {warning}")
    w(f"\nrun_id: {result.run_id}  (details in logs/agent.jsonl)")


def confirm_pending(result: AgentResult, owner: Owner, decision: Optional[bool], save, logger, out=sys.stdout) -> None:
    for action in result.pending_actions:
        answer = decision
        if answer is None:
            if not sys.stdin.isatty():
                answer = False
            else:
                answer = input(f"\nConfirm: {action.summary}? [y/N] ").strip().lower() in {"y", "yes"}
        if answer:
            outcome = execute_pending(owner, action)
            log_event(logger, "pending_confirmed", result.run_id, action=action.summary, ok=outcome["ok"],
                      error=outcome.get("error"))
            if outcome["ok"]:
                save()
                print(f"Done: {outcome['summary']}", file=out)
            else:
                print(f"Could not do it: {outcome['error']}", file=out)
        else:
            log_event(logger, "pending_cancelled", result.run_id, action=action.summary)
            print(f"Cancelled: {action.summary} (nothing was deleted)", file=out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pawpal_cli", description="Ask the PawPal+ agent to manage pet-care tasks.")
    p.add_argument("request", help='What you want, e.g. "Add a 9am walk for Max tomorrow"')
    p.add_argument("--data", type=Path, default=DEFAULT_DATA, help="data file (default: data.json)")
    p.add_argument("--reset-demo", action="store_true", help="overwrite --data with the demo pets and tasks")
    p.add_argument("--today", type=date.fromisoformat, help="treat this date (YYYY-MM-DD) as today")
    p.add_argument("--no-few-shot", action="store_true", help="turn off the few-shot examples in the prompt")
    p.add_argument("--model", help="Gemini model (default: PAWPAL_MODEL or the built-in default)")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--yes", action="store_true", help="auto-confirm destructive actions")
    group.add_argument("--no", action="store_true", help="auto-cancel destructive actions")
    p.add_argument("--export-trace", action="store_true", help="also write this run to docs/traces/")
    return p


def main(argv: Optional[Sequence[str]] = None, client: Optional[LLMClient] = None, out=sys.stdout) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(PROJECT_DIR / ".env")
    today = args.today or date.today()
    logger = setup_agent_logger()

    try:
        owner = load_owner(args.data, today, args.reset_demo)
    except ValueError as exc:
        print(f"Could not read {args.data}: {exc}", file=out)
        return 1

    if client is None:
        try:
            client = GeminiClient(model=args.model, logger=logger)
        except LLMError as exc:
            # Guardrails never need Gemini, so blocked requests still get their normal
            # reply; anything that needs the model fails cleanly with this message.
            print(f"Setup problem: {exc}\n(Guardrail checks still work without a key.)\n", file=out)
            client = UnavailableClient(str(exc))

    save = lambda: owner.save_to_json(str(args.data))  # noqa: E731
    agent = PawPalAgent(owner, client, logger=logger, save_fn=save, today=today,
                        few_shot=False if args.no_few_shot else None)
    print(f"You: {args.request}\n", file=out)
    result = agent.run(args.request)
    print_result(result, out)
    if result.pending_actions:
        confirm_pending(result, owner, True if args.yes else False if args.no else None, save, logger, out)
    if args.export_trace:
        for handler in logger.handlers:
            handler.flush()
        path = export_trace(result.run_id, title=args.request)
        try:
            shown = path.resolve().relative_to(PROJECT_DIR)  # no home-folder paths in shared output
        except ValueError:
            shown = path
        print(f"Trace written to {shown}", file=out)
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
