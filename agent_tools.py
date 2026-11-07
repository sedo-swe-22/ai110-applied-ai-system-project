"""Tool layer for the PawPal+ agent.

Each tool is a thin wrapper over the existing PawPal+ core (`pawpal_system.py`).
Tools take a `ToolContext` plus keyword arguments and always return a JSON-able
dict. They never raise: `run_tool()` turns every error into
`{"ok": False, "error": "..."}` so the model can read the problem and recover.

Tasks are identified by short *refs* like "Max:2" (pet name + index in that pet's
task list). Refs stay valid during a run because tasks are only ever appended or
replaced in place; real deletion happens only after the user confirms it, outside
the agent loop.

Nothing here knows about Gemini. `TOOL_SCHEMAS` is a provider-neutral list of
JSON-schema tool definitions; `llm_client.py` converts it for the Gemini SDK.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from pawpal_system import FREQUENCIES, PRIORITIES, Owner, Pet, Scheduler, Task

MAX_DESCRIPTION_LENGTH = 100


class ToolError(ValueError):
    """A problem with tool arguments that the model can fix and retry."""


@dataclass
class PendingAction:
    """A destructive action waiting for the user's confirmation."""

    kind: str  # currently only "remove_task"
    pet_name: str
    task: dict  # Task.to_dict() snapshot, used to find the task again on confirm
    summary: str


@dataclass
class ToolContext:
    """Everything a tool needs: the owner's data and the date to treat as today."""

    owner: Owner
    today: date = field(default_factory=date.today)
    pending_actions: list[PendingAction] = field(default_factory=list)

    @property
    def scheduler(self) -> Scheduler:
        return Scheduler(self.owner)


@dataclass(frozen=True)
class ToolSpec:
    """One registry entry: the function plus what the model needs to call it."""

    name: str
    description: str
    parameters: dict  # JSON schema for the arguments
    func: Callable[..., dict]
    writes: bool = False  # changes tasks (triggers the self-check)
    destructive: bool = False  # needs user confirmation


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    """Add a tool to the registry. Phase 2 (RAG) adds its tool the same way."""
    TOOL_REGISTRY[spec.name] = spec
    return spec


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def find_pet(owner: Owner, pet_name: str) -> Pet:
    """Return the pet with this name (case-insensitive) or raise ToolError."""
    wanted = (pet_name or "").strip().casefold()
    for pet in owner.pets:
        if pet.name.casefold() == wanted:
            return pet
    known = ", ".join(p.name for p in owner.pets) or "none"
    raise ToolError(f"unknown pet {pet_name!r}; known pets: {known}")


def make_ref(pet: Pet, index: int) -> str:
    return f"{pet.name}:{index}"


def resolve_ref(owner: Owner, ref: str) -> tuple[Pet, int, Task]:
    """Turn a ref like "Max:2" into (pet, index, task) or raise ToolError."""
    if not isinstance(ref, str) or ":" not in ref:
        raise ToolError(f"invalid ref {ref!r}; use a ref like 'Max:0' from list_tasks")
    pet_name, _, index_text = ref.rpartition(":")
    pet = find_pet(owner, pet_name)
    try:
        index = int(index_text)
    except ValueError:
        raise ToolError(f"invalid ref {ref!r}; the part after ':' must be a number") from None
    if not 0 <= index < len(pet.tasks):
        raise ToolError(f"unknown ref {ref!r}; call list_tasks to get current refs")
    return pet, index, pet.tasks[index]


def parse_date(value: Any, today: date) -> date:
    """Accept YYYY-MM-DD, 'today' or 'tomorrow'; raise ToolError otherwise."""
    if isinstance(value, date):
        return value
    text = str(value or "").strip().lower()
    if text == "today":
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ToolError(f"invalid date {value!r}; use YYYY-MM-DD") from None


def check_time(value: Any) -> str:
    """Return a zero-padded HH:MM time or raise ToolError."""
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ToolError(f"invalid time {value!r}; use 24-hour HH:MM, e.g. 09:00") from None


def task_view(pet: Pet, index: int, task: Task) -> dict:
    """A JSON-friendly view of one task, including its ref."""
    return {
        "ref": make_ref(pet, index),
        "pet": pet.name,
        "description": task.description,
        "time": task.time,
        "due_date": task.due_date.isoformat(),
        "frequency": task.frequency,
        "priority": task.priority,
        "completed": task.completed,
    }


def _views(owner: Owner, keep: Callable[[Pet, Task], bool]) -> list[dict]:
    views = [
        task_view(pet, i, t)
        for pet in owner.pets
        for i, t in enumerate(pet.tasks)
        if keep(pet, t)
    ]
    return sorted(views, key=lambda v: (v["due_date"], v["time"], v["pet"]))


def _label(view: dict) -> str:
    return f"{view['pet']}'s '{view['description']}' at {view['time']} on {view['due_date']}"


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


def list_pets(ctx: ToolContext) -> dict:
    pets = [
        {"name": p.name, "breed": p.breed, "task_count": len(p.tasks)}
        for p in ctx.owner.pets
    ]
    return {"ok": True, "owner": ctx.owner.name, "pets": pets}


def get_todays_schedule(ctx: ToolContext) -> dict:
    today_tasks = ctx.scheduler.get_todays_schedule(today=ctx.today)
    views = []
    for task in today_tasks:  # keep the scheduler's order (time, then priority)
        for pet in ctx.owner.pets:
            for i, t in enumerate(pet.tasks):
                if t is task:
                    views.append(task_view(pet, i, t))
    return {"ok": True, "date": ctx.today.isoformat(), "tasks": views}


def list_tasks(
    ctx: ToolContext,
    pet_name: Optional[str] = None,
    completed: Optional[bool] = None,
    due_date: Optional[str] = None,
) -> dict:
    if pet_name:
        find_pet(ctx.owner, pet_name)  # unknown pet -> clear error
    wanted_pet = pet_name.casefold() if pet_name else None
    wanted_date = parse_date(due_date, ctx.today) if due_date else None

    def keep(pet: Pet, t: Task) -> bool:
        return (
            (wanted_pet is None or pet.name.casefold() == wanted_pet)
            and (completed is None or t.completed == completed)
            and (wanted_date is None or t.due_date == wanted_date)
        )

    return {"ok": True, "tasks": _views(ctx.owner, keep)}


def find_free_slot(ctx: ToolContext, date: str, after: str = "00:00") -> dict:
    on = parse_date(date, ctx.today)
    slot = ctx.scheduler.next_available_slot(on=on, after=check_time(after))
    return {"ok": True, "date": on.isoformat(), "after": after, "free_slot": slot}


def check_conflicts(ctx: ToolContext) -> dict:
    conflicts = ctx.scheduler.detect_conflicts(suggest=True)
    return {"ok": True, "count": len(conflicts), "conflicts": conflicts}


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------


def add_task(
    ctx: ToolContext,
    pet_name: str,
    description: str,
    time: str,
    due_date: str,
    frequency: str = "once",
    priority: str = "medium",
) -> dict:
    pet = find_pet(ctx.owner, pet_name)
    description = (description or "").strip()
    if not description:
        raise ToolError("description must not be empty")
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ToolError(f"description must be at most {MAX_DESCRIPTION_LENGTH} characters")
    task = Task(  # Task.__post_init__ validates frequency and priority
        description=description,
        time=check_time(time),
        frequency=frequency,
        due_date=parse_date(due_date, ctx.today),
        priority=priority,
    )
    pet.add_task(task)
    view = task_view(pet, len(pet.tasks) - 1, task)
    return {"ok": True, "task": view, "summary": f"Added {_label(view)}"}


def complete_task(ctx: ToolContext, ref: str) -> dict:
    pet, index, task = resolve_ref(ctx.owner, ref)
    if task.completed:
        raise ToolError(f"{ref} is already completed")
    next_task = ctx.scheduler.mark_task_complete(pet, task, today=ctx.today)
    view = task_view(pet, index, task)
    result = {"ok": True, "task": view, "summary": f"Completed {_label(view)}"}
    if next_task is not None:
        next_view = task_view(pet, len(pet.tasks) - 1, next_task)
        result["next_occurrence"] = next_view
        result["summary"] += f"; next one due {next_view['due_date']}"
    return result


def reschedule_task(
    ctx: ToolContext,
    ref: str,
    time: Optional[str] = None,
    due_date: Optional[str] = None,
) -> dict:
    if not time and not due_date:
        raise ToolError("give a new time, a new due_date, or both")
    pet, index, task = resolve_ref(ctx.owner, ref)
    if task.completed:
        raise ToolError(f"{ref} is already completed; it cannot be rescheduled")
    before = task_view(pet, index, task)
    changes: dict[str, Any] = {}
    if time:
        changes["time"] = check_time(time)
    if due_date:
        changes["due_date"] = parse_date(due_date, ctx.today)
    pet.tasks[index] = replace(task, **changes)  # replace() re-runs Task validation
    after = task_view(pet, index, pet.tasks[index])
    summary = (
        f"Moved {before['pet']}'s '{before['description']}' from "
        f"{before['time']} on {before['due_date']} to {after['time']} on {after['due_date']}"
    )
    return {"ok": True, "before": before, "task": after, "summary": summary}


def remove_task(ctx: ToolContext, ref: str) -> dict:
    """Queue a removal. Nothing is deleted until the user confirms it."""
    pet, index, task = resolve_ref(ctx.owner, ref)
    view = task_view(pet, index, task)
    summary = f"Remove {_label(view)}"
    ctx.pending_actions.append(
        PendingAction(kind="remove_task", pet_name=pet.name, task=task.to_dict(), summary=summary)
    )
    return {
        "ok": True,
        "status": "needs_confirmation",
        "task": view,
        "summary": f"Waiting for confirmation: {summary}",
        "message": "Not deleted yet. Tell the user it needs their confirmation.",
    }


def execute_pending(owner: Owner, action: PendingAction) -> dict:
    """Carry out a confirmed pending action. Called by the app/CLI, not the model."""
    try:
        if action.kind != "remove_task":
            raise ToolError(f"unknown pending action {action.kind!r}")
        pet = find_pet(owner, action.pet_name)
        target = Task.from_dict(action.task)
        for task in pet.tasks:
            if task == target:  # dataclass equality: same fields
                pet.remove_task(task)
                return {"ok": True, "summary": action.summary.replace("Remove", "Removed", 1)}
        raise ToolError("that task no longer exists")
    except (ToolError, ValueError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Registry + runner
# ---------------------------------------------------------------------------

_DATE = {"type": "string", "description": "Date as YYYY-MM-DD (or 'today' / 'tomorrow')."}
_TIME = {"type": "string", "description": "24-hour time as HH:MM, e.g. 09:00."}
_REF = {"type": "string", "description": "Task ref like 'Max:0', from list_tasks or another tool result."}
_PET = {"type": "string", "description": "The pet's name."}

register(ToolSpec(
    name="list_pets",
    description="List the owner's pets with breed and number of tasks.",
    parameters={"type": "object", "properties": {}},
    func=list_pets,
))
register(ToolSpec(
    name="get_todays_schedule",
    description="Get today's open (not completed) tasks for all pets, sorted by time.",
    parameters={"type": "object", "properties": {}},
    func=get_todays_schedule,
))
register(ToolSpec(
    name="list_tasks",
    description="List tasks with their refs. Optional filters: pet, completion status, due date.",
    parameters={
        "type": "object",
        "properties": {
            "pet_name": _PET,
            "completed": {"type": "boolean", "description": "true = only done, false = only open."},
            "due_date": _DATE,
        },
    },
    func=list_tasks,
))
register(ToolSpec(
    name="find_free_slot",
    description="Find the first free HH:MM start time on a date, at or after a given time (15-minute steps).",
    parameters={
        "type": "object",
        "properties": {"date": _DATE, "after": _TIME},
        "required": ["date"],
    },
    func=find_free_slot,
))
register(ToolSpec(
    name="check_conflicts",
    description="Find open tasks that share the same date and start time. Call this after every change.",
    parameters={"type": "object", "properties": {}},
    func=check_conflicts,
))
register(ToolSpec(
    name="add_task",
    description="Add a care task (walk, feeding, meds, grooming...) for one pet.",
    parameters={
        "type": "object",
        "properties": {
            "pet_name": _PET,
            "description": {"type": "string", "description": "Short task name, e.g. 'Morning walk'."},
            "time": _TIME,
            "due_date": _DATE,
            "frequency": {"type": "string", "enum": list(FREQUENCIES)},
            "priority": {"type": "string", "enum": list(PRIORITIES)},
        },
        "required": ["pet_name", "description", "time", "due_date"],
    },
    func=add_task,
    writes=True,
))
register(ToolSpec(
    name="complete_task",
    description="Mark a task done. Daily/weekly tasks automatically get their next occurrence.",
    parameters={"type": "object", "properties": {"ref": _REF}, "required": ["ref"]},
    func=complete_task,
    writes=True,
))
register(ToolSpec(
    name="reschedule_task",
    description="Move an open task to a new time and/or date. The ref stays the same.",
    parameters={
        "type": "object",
        "properties": {"ref": _REF, "time": _TIME, "due_date": _DATE},
        "required": ["ref"],
    },
    func=reschedule_task,
    writes=True,
))
register(ToolSpec(
    name="remove_task",
    description="Ask to delete a task. It is NOT deleted until the user confirms in the app.",
    parameters={"type": "object", "properties": {"ref": _REF}, "required": ["ref"]},
    func=remove_task,
    writes=True,
    destructive=True,
))


def tool_schemas() -> list[dict]:
    """Provider-neutral tool definitions (name, description, JSON-schema parameters)."""
    return [
        {"name": s.name, "description": s.description, "parameters": s.parameters}
        for s in TOOL_REGISTRY.values()
    ]


TOOL_SCHEMAS = tool_schemas()


def run_tool(ctx: ToolContext, name: str, args: Optional[dict] = None) -> dict:
    """Run one tool by name. Never raises; errors come back as {"ok": False, ...}."""
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        return {"ok": False, "error": f"unknown tool {name!r}"}
    args = dict(args or {})
    allowed = set(spec.parameters.get("properties", {}))
    unexpected = sorted(set(args) - allowed)
    if unexpected:
        return {"ok": False, "error": f"unexpected argument(s) for {name}: {', '.join(unexpected)}"}
    missing = [a for a in spec.parameters.get("required", []) if args.get(a) in (None, "")]
    if missing:
        return {"ok": False, "error": f"missing required argument(s) for {name}: {', '.join(missing)}"}
    try:
        return spec.func(ctx, **args)
    except (ToolError, ValueError) as exc:  # ValueError comes from Task validation
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # last-resort safety net: never crash the agent loop
        return {"ok": False, "error": f"internal error in {name}: {type(exc).__name__}"}
