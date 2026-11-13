"""Unit tests for the tool layer (no LLM involved)."""

from dataclasses import replace
from datetime import date

import pytest

from agent_tools import (
    TOOL_REGISTRY,
    TOOL_SCHEMAS,
    ToolContext,
    execute_pending,
    run_tool,
)
from pawpal_system import Owner, Pet, Task

TODAY = date(2026, 11, 10)
TOMORROW = "2026-11-11"


@pytest.fixture
def ctx():
    owner = Owner("Jordan")
    max_ = Pet("Max", "Beagle")
    mochi = Pet("Mochi", "Shiba Inu")
    owner.add_pet(max_)
    owner.add_pet(mochi)
    max_.add_task(Task("Morning walk", "08:00", frequency="daily", due_date=TODAY))
    mochi.add_task(Task("Grooming", "10:00", frequency="weekly", due_date=date(2026, 11, 11)))
    return ToolContext(owner=owner, today=TODAY)


def test_registry_and_schemas_match():
    assert {s["name"] for s in TOOL_SCHEMAS} == set(TOOL_REGISTRY)
    for schema in TOOL_SCHEMAS:
        assert schema["parameters"]["type"] == "object"
        assert schema["description"]


def test_list_pets(ctx):
    out = run_tool(ctx, "list_pets")
    assert out["ok"] and [p["name"] for p in out["pets"]] == ["Max", "Mochi"]


def test_list_tasks_returns_refs_and_filters(ctx):
    out = run_tool(ctx, "list_tasks", {"pet_name": "mochi"})  # case-insensitive
    assert out["ok"]
    assert [t["ref"] for t in out["tasks"]] == ["Mochi:0"]
    by_date = run_tool(ctx, "list_tasks", {"due_date": "today"})
    assert [t["description"] for t in by_date["tasks"]] == ["Morning walk"]


def test_get_todays_schedule_uses_injected_today(ctx):
    out = run_tool(ctx, "get_todays_schedule")
    assert out["date"] == "2026-11-10"
    assert [t["ref"] for t in out["tasks"]] == ["Max:0"]


def test_add_task_returns_new_ref_and_summary(ctx):
    out = run_tool(ctx, "add_task", {"pet_name": "Max", "description": "Vet visit", "time": "9:00", "due_date": "tomorrow"})
    assert out["ok"]
    assert out["task"]["ref"] == "Max:1"
    assert out["task"]["time"] == "09:00"  # normalized
    assert out["task"]["due_date"] == TOMORROW
    assert "Added Max's 'Vet visit'" in out["summary"]


@pytest.mark.parametrize(
    "args, error_part",
    [
        ({"pet_name": "Rex", "description": "Walk", "time": "09:00", "due_date": TOMORROW}, "unknown pet"),
        ({"pet_name": "Max", "description": "Walk", "time": "9am", "due_date": TOMORROW}, "invalid time"),
        ({"pet_name": "Max", "description": "Walk", "time": "09:00", "due_date": "next week"}, "invalid date"),
        ({"pet_name": "Max", "description": "Walk", "time": "09:00", "due_date": TOMORROW, "frequency": "hourly"}, "frequency"),
        ({"pet_name": "Max", "description": "  ", "time": "09:00", "due_date": TOMORROW}, "empty"),
        ({"pet_name": "Max", "description": "x" * 101, "time": "09:00", "due_date": TOMORROW}, "at most"),
        ({"pet_name": "Max", "description": "Walk", "time": "09:00"}, "missing required"),
        ({"pet_name": "Max", "description": "Walk", "time": "09:00", "due_date": TOMORROW, "color": "red"}, "unexpected"),
    ],
)
def test_add_task_errors_are_returned_not_raised(ctx, args, error_part):
    out = run_tool(ctx, "add_task", args)
    assert out["ok"] is False
    assert error_part in out["error"]
    assert len(ctx.owner.pets[0].tasks) == 1  # nothing was added


def test_unknown_tool_is_an_error(ctx):
    assert run_tool(ctx, "delete_everything")["ok"] is False


@pytest.mark.parametrize("ref", ["Max:5", "Rex:0", "Max", "Max:x", None])
def test_bad_refs_are_errors(ctx, ref):
    out = run_tool(ctx, "complete_task", {"ref": ref})
    assert out["ok"] is False


def test_complete_daily_task_creates_next_occurrence(ctx):
    out = run_tool(ctx, "complete_task", {"ref": "Max:0"})
    assert out["ok"] and out["task"]["completed"] is True
    assert out["next_occurrence"]["due_date"] == TOMORROW
    assert out["next_occurrence"]["ref"] == "Max:1"
    again = run_tool(ctx, "complete_task", {"ref": "Max:0"})
    assert again["ok"] is False and "already completed" in again["error"]


def test_reschedule_keeps_ref_and_revalidates(ctx):
    out = run_tool(ctx, "reschedule_task", {"ref": "Mochi:0", "time": "11:30"})
    assert out["ok"]
    assert out["task"]["ref"] == "Mochi:0" and out["task"]["time"] == "11:30"
    assert out["before"]["time"] == "10:00"
    assert run_tool(ctx, "reschedule_task", {"ref": "Mochi:0", "time": "25:00"})["ok"] is False
    assert run_tool(ctx, "reschedule_task", {"ref": "Mochi:0"})["ok"] is False  # nothing to change
    assert ctx.owner.pets[1].tasks[0].time == "11:30"


def test_find_free_slot_and_conflicts(ctx):
    run_tool(ctx, "add_task", {"pet_name": "Mochi", "description": "Feed", "time": "08:00", "due_date": "today"})
    conflicts = run_tool(ctx, "check_conflicts")
    assert conflicts["count"] == 1 and "08:00" in conflicts["conflicts"][0]
    slot = run_tool(ctx, "find_free_slot", {"date": "today", "after": "08:00"})
    assert slot["free_slot"] == "08:15"


def test_remove_task_only_queues_until_confirmed(ctx):
    out = run_tool(ctx, "remove_task", {"ref": "Mochi:0"})
    assert out["ok"] and out["status"] == "needs_confirmation"
    assert len(ctx.owner.pets[1].tasks) == 1  # still there
    assert len(ctx.pending_actions) == 1

    done = execute_pending(ctx.owner, ctx.pending_actions[0])
    assert done["ok"] and done["summary"].startswith("Removed")
    assert ctx.owner.pets[1].tasks == []
    assert execute_pending(ctx.owner, ctx.pending_actions[0])["ok"] is False  # already gone


def test_internal_errors_never_escape(ctx, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setitem(TOOL_REGISTRY, "list_pets", replace(TOOL_REGISTRY["list_pets"], func=boom))
    out = run_tool(ctx, "list_pets")
    assert out["ok"] is False and "internal error" in out["error"]


# ---------------------------------------------------------------- retrieval tools (RAG)

from agent_tools import tool_names, tool_schemas  # noqa: E402


def test_retrieval_tools_are_registered_and_read_only():
    for name in ("lookup_care_guidelines", "find_similar_tasks"):
        spec = TOOL_REGISTRY[name]
        assert spec.retrieval and not spec.writes and not spec.destructive


def test_rag_off_hides_the_retrieval_tools():
    on, off = {s["name"] for s in tool_schemas(True)}, {s["name"] for s in tool_schemas(False)}
    assert on - off == {"lookup_care_guidelines", "find_similar_tasks"}
    assert tool_names(False) == off and tool_names(True) == on


def test_lookup_care_guidelines_returns_cited_passages(ctx):
    out = run_tool(ctx, "lookup_care_guidelines",
                   {"query": "puppy daily routine feeding walks play", "species": "Dog", "life_stage": "puppy"})
    assert out["ok"] and not out["none_found"]
    cites = [h["cite"] for h in out["hits"]]
    assert "[dog-puppy.md: Feeding]" in cites
    assert all({"cite", "text"} == set(h) for h in out["hits"])
    feeding = next(h for h in out["hits"] if h["cite"] == "[dog-puppy.md: Feeding]")
    assert "3 meals" in feeding["text"]


def test_lookup_with_no_match_says_so_and_warns_against_inventing(ctx):
    out = run_tool(ctx, "lookup_care_guidelines", {"query": "quantum physics homework"})
    assert out["ok"] and out["none_found"] and out["hits"] == []
    assert "Do not invent" in out["note"]


def test_lookup_for_unknown_species_only_returns_generic_guidance(ctx):
    out = run_tool(ctx, "lookup_care_guidelines", {"query": "daily routine", "species": "axolotl"})
    assert out["ok"] and out["hits"]
    assert all(h["cite"].startswith("[general-routine.md") or h["cite"].startswith("[grooming.md")
               or h["cite"].startswith("[medication-scheduling.md") for h in out["hits"])


@pytest.mark.parametrize("args", [{"query": ""}, {"query": "   "}, {"query": "x" * 201}, {}])
def test_lookup_rejects_bad_queries(ctx, args):
    out = run_tool(ctx, "lookup_care_guidelines", args)
    assert out["ok"] is False and "query" in out["error"]


def test_find_similar_tasks_searches_the_live_history(ctx):
    out = run_tool(ctx, "find_similar_tasks", {"query": "walk", "pet_name": "max"})
    assert out["ok"] and [h["cite"] for h in out["hits"]] == ["[history: Max:0]"]
    assert "08:00" in out["hits"][0]["text"]
    run_tool(ctx, "add_task", {"pet_name": "Mochi", "description": "Walk", "time": "12:00", "due_date": TOMORROW})
    again = run_tool(ctx, "find_similar_tasks", {"query": "walk"})
    assert {h["cite"] for h in again["hits"]} == {"[history: Max:0]", "[history: Mochi:1]"}


def test_find_similar_tasks_unknown_pet_is_an_error_not_a_crash(ctx):
    out = run_tool(ctx, "find_similar_tasks", {"query": "walk", "pet_name": "Rex"})
    assert out["ok"] is False and "Rex" in out["error"]
