"""The PawPal+ agent: plan -> act with tools -> check -> repair -> reply.

One call to `PawPalAgent.run(message)` handles one user request:

1. Input guardrails (no LLM call if the request is blocked).
2. Loop: Gemini proposes function calls; each passes the tool guardrails and runs
   against the real Owner/Scheduler; the results go back to Gemini. The loop ends
   when Gemini answers with text only, or fails at the 10-call cap.
3. Self-check in code: every change the agent reported must really exist, and
   the slots it touched must have no clashes. Combined with the model's own
   CONFIDENCE line, this gives the confidence score.
4. Output check: the reply may not claim changes that no tool call made.
5. Success -> save. Failure (API error, crash, call cap) -> roll back; nothing is saved.

Every step is logged to logs/agent.jsonl under one run_id.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

import guardrails
from agent_tools import (
    TOOL_REGISTRY,
    PendingAction,
    ToolContext,
    ToolError,
    resolve_ref,
    run_tool,
    tool_schemas,
    task_view,
)
from llm_client import LLMClient, LLMError, ToolCall
from logging_config import log_event, null_logger
from pawpal_system import Owner


# ---------------------------------------------------------------------------
# System prompt, built from sections (phase 2 adds a retrieval section)
# ---------------------------------------------------------------------------

ROLE_SECTION = (
    "You are PawPal+, a pet-care scheduling assistant for {owner}. You manage the pets' "
    "care tasks (walks, feeding, meds, grooming, vet visits, play) ONLY by calling the "
    "provided tools. You never invent tasks, refs or tool results."
)

RULES_SECTION = """Rules:
1. Dates are YYYY-MM-DD and times are 24-hour HH:MM. Resolve words like "tomorrow" or "Friday" against today's date below. "9am" is 09:00, "6pm" is 18:00.
2. Tasks are identified by refs like "Max:0". Get refs from list_tasks, get_todays_schedule or a previous tool result. Never guess a ref.
3. After ANY change (add_task, reschedule_task, complete_task), call check_conflicts. If a task you added or moved now clashes, call find_free_slot for that date with after= the clash time, then reschedule_task YOUR task (not the user's other tasks) to that slot, and tell the user.
4. remove_task does NOT delete anything; it queues the deletion for the user's confirmation. Say it needs confirmation. Never say it was deleted.
5. If a tool returns ok:false, read the error, fix the arguments and retry once; otherwise explain the problem.
6. If the request is missing something you need (which pet, the time, the day) or matches several tasks, ask ONE short clarifying question instead of guessing. Do not call write tools in that case.
7. Stay on topic: only pet-care scheduling. Never give medical advice (doses, symptoms, diagnoses); suggest contacting a vet. Scheduling medication tasks is fine.
8. Reply briefly in plain language. Say exactly what changed (pet, task, time, date).
9. End EVERY reply with one final line in exactly this format:
CONFIDENCE: <number from 0 to 1> | <short reason>
Use a high number only when the tool results confirm the request was fully done."""

CONTEXT_SECTION = """Today is {weekday}, {today}.
Owner: {owner}
Pets: {pets}"""

FEW_SHOT_SECTION = """Examples of good behavior (dates are illustrative):

Example 1 - simple add
User: Add a 9am walk for Max tomorrow
Calls: add_task(pet_name="Max", description="Walk", time="09:00", due_date="2026-11-07") -> ok, ref Max:3
       check_conflicts() -> count 0
Reply: Added Max's walk at 09:00 on 2026-11-07. No clashes.
CONFIDENCE: 0.95 | task added and conflict check is clean

Example 2 - clash repair
User: Feed Mochi at 8 tomorrow
Calls: add_task(pet_name="Mochi", description="Feed", time="08:00", due_date="2026-11-07") -> ok, ref Mochi:1
       check_conflicts() -> "Conflict at 08:00 on 2026-11-07: Max: Walk, Mochi: Feed. Next free slot: 08:15."
       find_free_slot(date="2026-11-07", after="08:00") -> "08:15"
       reschedule_task(ref="Mochi:1", time="08:15") -> ok
       check_conflicts() -> count 0
Reply: Added Mochi's feeding on 2026-11-07. 08:00 was taken by Max's walk, so I moved it to 08:15.
CONFIDENCE: 0.9 | added, clash found and repaired, re-check clean

Example 3 - deletion needs confirmation
User: Delete Mochi's grooming
Calls: list_tasks(pet_name="Mochi") -> one open "Grooming" task, ref Mochi:0
       remove_task(ref="Mochi:0") -> needs_confirmation
Reply: Mochi's grooming at 10:00 on 2026-11-08 is ready to be removed. Please confirm to delete it.
CONFIDENCE: 0.9 | correct task found; deletion waits for the user

Example 4 - missing information
User: Add a vet visit for Max
Calls: none
Reply: Sure - what day and time is Max's vet visit?
CONFIDENCE: 0.8 | need the day and time before adding anything"""


def few_shot_enabled_by_default() -> bool:
    return os.environ.get("PAWPAL_FEW_SHOT", "on").strip().lower() not in {"off", "0", "false", "no"}


def build_system_prompt(owner: Owner, today: date, few_shot: bool = True, extra_sections: tuple[str, ...] = ()) -> str:
    pets = ", ".join(f"{p.name} ({p.breed})" for p in owner.pets) or "none yet"
    sections = [
        ROLE_SECTION.format(owner=owner.name),
        RULES_SECTION,
        CONTEXT_SECTION.format(weekday=today.strftime("%A"), today=today.isoformat(), owner=owner.name, pets=pets),
    ]
    if few_shot:
        sections.append(FEW_SHOT_SECTION)
    sections.extend(extra_sections)
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class Step:
    """One tool call made during a run (shown in the UI/CLI and logged)."""

    n: int
    tool: str
    args: dict
    ok: bool
    result: dict
    ms: int
    blocked: bool = False  # stopped by a tool guardrail before running


@dataclass
class AgentResult:
    run_id: str
    reply: str
    steps: list[Step] = field(default_factory=list)
    actions_taken: list[str] = field(default_factory=list)
    pending_actions: list[PendingAction] = field(default_factory=list)
    blocked: bool = False
    block_reason: Optional[str] = None
    failed: bool = False
    error: Optional[str] = None
    checks_passed: Optional[bool] = None  # None = nothing changed, so nothing to verify
    check_details: list[str] = field(default_factory=list)
    model_confidence: Optional[float] = None
    confidence_reason: Optional[str] = None
    output_warnings: list[str] = field(default_factory=list)
    llm_calls: int = 0

    @property
    def confidence(self) -> Optional[float]:
        """Hybrid score: the model's rating, capped at 0.3 when the code checks fail."""
        if self.blocked or self.failed:
            return None
        base = self.model_confidence if self.model_confidence is not None else 0.5
        if self.checks_passed is False:
            return round(min(base, 0.3), 2)
        return round(base, 2)


_CONFIDENCE_LINE = re.compile(
    r"^\s*\**CONFIDENCE\**\s*:\s*\**\s*([0-9]*\.?[0-9]+)\s*\**\s*(?:\|\s*(.*))?$", re.IGNORECASE | re.MULTILINE
)


def split_confidence(text: str) -> tuple[str, Optional[float], Optional[str]]:
    """Remove the trailing 'CONFIDENCE: x | reason' line and return (reply, score, reason)."""
    matches = list(_CONFIDENCE_LINE.finditer(text or ""))
    if not matches:
        return (text or "").strip(), None, None
    last = matches[-1]
    try:
        score = max(0.0, min(1.0, float(last.group(1))))
    except ValueError:
        score = None
    reason = (last.group(2) or "").strip() or None
    reply = (text[: last.start()] + text[last.end():]).strip()
    return reply, score, reason


class ToolCallCapExceeded(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class PawPalAgent:
    def __init__(
        self,
        owner: Owner,
        client: LLMClient,
        logger: Optional[logging.Logger] = None,
        save_fn: Optional[Callable[[], None]] = None,
        today: Optional[date] = None,
        few_shot: Optional[bool] = None,
        max_tool_calls: int = guardrails.MAX_TOOL_CALLS,
    ) -> None:
        self.owner = owner
        self.client = client
        self.logger = logger or null_logger()
        self.save_fn = save_fn
        self._today = today
        self.few_shot = few_shot_enabled_by_default() if few_shot is None else few_shot
        self.max_tool_calls = max_tool_calls

    @property
    def today(self) -> date:
        return self._today or date.today()

    def _log(self, event: str, run_id: str, level: int = logging.INFO, **fields: Any) -> None:
        log_event(self.logger, event, run_id, level=level, **fields)

    def run(self, message: str) -> AgentResult:
        run_id = uuid.uuid4().hex[:8]
        today = self.today
        self._log("run_start", run_id, message=message, model=getattr(self.client, "model", "?"),
                  today=today.isoformat(), few_shot=self.few_shot)

        # 1. Input guardrails: blocked requests never reach the model.
        verdict = guardrails.check_input(message, [p.name for p in self.owner.pets])
        if not verdict.allowed:
            self._log("input_blocked", run_id, reason=verdict.reason)
            self._log("run_end", run_id, status="blocked")
            return AgentResult(run_id=run_id, reply=verdict.reply or "", blocked=True, block_reason=verdict.reason)

        snapshot = copy.deepcopy(self.owner.pets)
        ctx = ToolContext(owner=self.owner, today=today)
        result = AgentResult(run_id=run_id, reply="")
        if hasattr(self.client, "run_id"):
            self.client.run_id = run_id  # lets the client log retries under this run

        try:
            final_text = self._loop(message, ctx, result)
        except Exception as exc:  # LLMError, ToolCallCapExceeded or an unexpected bug
            self.owner.pets[:] = snapshot  # roll back every change made in this run
            friendly = str(exc) if isinstance(exc, (LLMError, ToolCallCapExceeded)) else "Something went wrong inside the assistant."
            self._log("run_failed", run_id, level=logging.ERROR, error_type=type(exc).__name__, error=str(exc))
            self._log("rollback", run_id, restored_tasks=sum(len(p.tasks) for p in snapshot))
            result.failed, result.error = True, f"{type(exc).__name__}: {exc}"
            result.reply = f"Sorry, I couldn't finish that. {friendly} No changes were saved."
            result.actions_taken, result.pending_actions = [], []
            self._log("run_end", run_id, status="failed")
            return result

        # 3. Self-check (code) + model confidence.
        reply, score, reason = split_confidence(final_text)
        result.reply = reply or "Done."
        result.model_confidence, result.confidence_reason = score, reason
        result.pending_actions = list(ctx.pending_actions)
        result.checks_passed, result.check_details = self_check(ctx, result.steps)
        self._log("self_check", run_id, checks_passed=result.checks_passed, details=result.check_details,
                  model_confidence=score, confidence=result.confidence)

        # 4. Output check against the successful tool results.
        succeeded = [s.tool for s in result.steps if s.ok and not s.blocked]
        result.output_warnings = guardrails.check_output(result.reply, succeeded, len(result.pending_actions))
        for warning in result.output_warnings:
            self._log("output_warning", run_id, level=logging.WARNING, warning=warning)

        # 5. Save only after a successful run that changed something.
        changed = any(s.ok and TOOL_REGISTRY[s.tool].writes and not TOOL_REGISTRY[s.tool].destructive
                      for s in result.steps if s.tool in TOOL_REGISTRY)
        if changed and self.save_fn is not None:
            self.save_fn()
            self._log("saved", run_id)
        self._log("run_end", run_id, status="ok", reply=result.reply, actions=result.actions_taken,
                  pending=[p.summary for p in result.pending_actions], llm_calls=result.llm_calls)
        return result

    def _loop(self, message: str, ctx: ToolContext, result: AgentResult) -> str:
        system = build_system_prompt(self.owner, ctx.today, self.few_shot)
        tools = tool_schemas()
        history: list[dict] = [{"role": "user", "text": message}]
        max_llm_calls = self.max_tool_calls + 3

        while result.llm_calls < max_llm_calls:
            started = time.perf_counter()
            response = self.client.generate(system, history, tools)
            result.llm_calls += 1
            self._log("llm_call", result.run_id, turn=result.llm_calls,
                      ms=int((time.perf_counter() - started) * 1000),
                      tool_calls=[c.name for c in response.tool_calls], text=response.text[:500])
            history.append({"role": "model", "text": response.text, "tool_calls": response.tool_calls, "raw": response.raw})

            if not response.tool_calls:
                return response.text

            outputs = []
            for call in response.tool_calls:
                outputs.append((call, self._run_call(call, ctx, result)))
            history.append({"role": "tool", "results": outputs})

        raise ToolCallCapExceeded(f"The assistant needed more than {max_llm_calls} model calls.")

    def _run_call(self, call: ToolCall, ctx: ToolContext, result: AgentResult) -> dict:
        if len(result.steps) >= self.max_tool_calls:
            raise ToolCallCapExceeded(f"The assistant hit the limit of {self.max_tool_calls} tool calls.")
        n = len(result.steps) + 1
        started = time.perf_counter()
        verdict = guardrails.check_tool_call(call.name, call.args, ctx.today)
        if verdict.allowed:
            output = run_tool(ctx, call.name, call.args)
            blocked = False
        else:
            output = {"ok": False, "error": verdict.error, "guardrail": True}
            blocked = True
        ms = int((time.perf_counter() - started) * 1000)
        ok = bool(output.get("ok"))
        result.steps.append(Step(n=n, tool=call.name, args=dict(call.args), ok=ok, result=output, ms=ms, blocked=blocked))
        if ok and output.get("summary"):
            result.actions_taken.append(output["summary"])
        self._log("tool_blocked" if blocked else "tool_call", result.run_id,
                  level=logging.WARNING if not ok else logging.INFO,
                  n=n, tool=call.name, args=call.args, ok=ok, ms=ms, result=output)
        return output


# ---------------------------------------------------------------------------
# Self-check
# ---------------------------------------------------------------------------

def self_check(ctx: ToolContext, steps: list[Step]) -> tuple[Optional[bool], list[str]]:
    """Verify in code that reported changes really exist and caused no clashes.

    Returns (None, [...]) when the run changed nothing, so there is nothing to verify.
    """
    write_steps = [s for s in steps if s.ok and s.tool in TOOL_REGISTRY and TOOL_REGISTRY[s.tool].writes
                   and not TOOL_REGISTRY[s.tool].destructive]
    if not write_steps:
        return None, ["No tasks were changed, so there was nothing to verify."]

    details: list[str] = []
    passed = True
    touched: set[tuple[str, str]] = set()
    latest: dict[str, dict] = {}  # ref -> last reported view (a task may be added, then moved)
    for step in write_steps:
        for key in ("task", "next_occurrence"):  # a completed daily task also creates a new one
            view = step.result.get(key) or {}
            if view.get("ref"):
                latest[view["ref"]] = view

    for ref, view in latest.items():
        try:
            pet, index, task = resolve_ref(ctx.owner, ref)
        except ToolError:
            passed = False
            details.append(f"FAIL {ref}: the task the agent reported no longer exists.")
            continue
        actual = task_view(pet, index, task)
        same = all(actual[k] == view.get(k) for k in ("description", "time", "due_date", "completed"))
        if same:
            details.append(f"OK   {ref}: '{actual['description']}' at {actual['time']} on {actual['due_date']} is saved.")
        else:
            passed = False
            details.append(f"FAIL {ref}: the saved task differs from what the agent reported.")
        if not actual["completed"]:
            touched.add((actual["due_date"], actual["time"]))

    open_slots = Counter(
        (t.due_date.isoformat(), t.time) for t in ctx.owner.get_all_tasks() if not t.completed
    )
    for due, at in sorted(touched):
        if open_slots[(due, at)] > 1:
            passed = False
            details.append(f"FAIL clash: {open_slots[(due, at)]} open tasks share {at} on {due}.")
    if touched and passed:
        details.append("OK   no clashes at the times the agent changed.")
    return passed, details
