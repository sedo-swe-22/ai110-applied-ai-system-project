"""Tests for the agent loop using a scripted fake LLM (no API key, no quota)."""

import json
from datetime import date

import pytest

from agent import PawPalAgent, build_system_prompt, split_confidence
from agent_tools import execute_pending
from llm_client import LLMError, LLMResponse, ToolCall
from logging_config import JsonLineFormatter, export_trace, read_run, setup_agent_logger
from pawpal_system import Owner, Pet, Task

TODAY = date(2026, 11, 10)
TOMORROW = "2026-11-11"


class FakeClient:
    """Returns scripted responses in order and records what it was sent."""

    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def generate(self, system_prompt, history, tools):
        self.calls.append({"system": system_prompt, "history": list(history), "tools": tools})
        if not self.script:
            raise AssertionError("FakeClient ran out of scripted responses")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def calls(*pairs):
    return LLMResponse(tool_calls=[ToolCall(name=n, args=a, id=f"c{i}") for i, (n, a) in enumerate(pairs)])


def say(text):
    return LLMResponse(text=text)


@pytest.fixture
def owner():
    o = Owner("Jordan")
    max_ = Pet("Max", "Beagle")
    mochi = Pet("Mochi", "Shiba Inu")
    o.add_pet(max_)
    o.add_pet(mochi)
    max_.add_task(Task("Morning walk", "08:00", frequency="daily", due_date=date(2026, 11, 11)))
    mochi.add_task(Task("Grooming", "10:00", due_date=date(2026, 11, 11)))
    return o


def make_agent(owner, script, **kwargs):
    client = FakeClient(script)
    saves = []
    agent = PawPalAgent(owner, client, save_fn=lambda: saves.append(1), today=TODAY, **kwargs)
    return agent, client, saves


def test_simple_add_runs_tools_and_saves(owner):
    agent, client, saves = make_agent(owner, [
        calls(("add_task", {"pet_name": "Max", "description": "Vet visit", "time": "09:00", "due_date": TOMORROW})),
        calls(("check_conflicts", {})),
        say("I've added Max's vet visit at 09:00 on 2026-11-11.\nCONFIDENCE: 0.95 | added, no clashes"),
    ])
    result = agent.run("Add a 9am vet visit for Max tomorrow")

    assert not result.failed and not result.blocked
    assert [s.tool for s in result.steps] == ["add_task", "check_conflicts"]
    assert result.actions_taken == ["Added Max's 'Vet visit' at 09:00 on 2026-11-11"]
    assert result.reply == "I've added Max's vet visit at 09:00 on 2026-11-11."  # confidence line stripped
    assert result.model_confidence == 0.95 and result.confidence == 0.95
    assert result.checks_passed is True
    assert result.output_warnings == []
    assert saves == [1]
    assert result.llm_calls == 3
    # Tool results were sent back to the model.
    last_history = client.calls[-1]["history"]
    assert last_history[-1]["role"] == "tool"
    assert last_history[-1]["results"][0][1]["ok"] is True


def test_conflict_repair_flow(owner):
    agent, _, _ = make_agent(owner, [
        calls(("add_task", {"pet_name": "Mochi", "description": "Feed", "time": "08:00", "due_date": TOMORROW})),
        calls(("check_conflicts", {})),
        calls(("find_free_slot", {"date": TOMORROW, "after": "08:00"})),
        calls(("reschedule_task", {"ref": "Mochi:1", "time": "08:15"})),
        calls(("check_conflicts", {})),
        say("Added Mochi's feeding; 08:00 was taken so I moved it to 08:15.\nCONFIDENCE: 0.9 | repaired"),
    ])
    result = agent.run("Feed Mochi at 8 tomorrow")

    conflict_step = result.steps[1]
    assert conflict_step.result["count"] == 1
    assert result.steps[2].result["free_slot"] == "08:15"
    assert result.steps[4].result["count"] == 0
    assert owner.pets[1].tasks[1].time == "08:15"
    assert result.checks_passed is True


def test_self_check_catches_unrepaired_clash(owner):
    agent, _, _ = make_agent(owner, [
        calls(("add_task", {"pet_name": "Mochi", "description": "Feed", "time": "08:00", "due_date": TOMORROW})),
        say("Added Mochi's feeding at 08:00.\nCONFIDENCE: 0.9 | done"),
    ])
    result = agent.run("Feed Mochi at 8 tomorrow")
    assert result.checks_passed is False
    assert any("clash" in d for d in result.check_details)
    assert result.confidence == 0.3  # capped because the code check failed


def test_read_only_question_has_no_self_check(owner):
    agent, _, saves = make_agent(owner, [
        calls(("get_todays_schedule", {})),
        say("Nothing is scheduled for today.\nCONFIDENCE: 0.9 | schedule read"),
    ])
    result = agent.run("What's on today?")
    assert result.checks_passed is None
    assert saves == []  # nothing changed, nothing saved


def test_blocked_input_never_calls_the_model(owner):
    agent, client, _ = make_agent(owner, [])
    result = agent.run("How much ibuprofen can I give Max?")
    assert result.blocked and result.block_reason == "medical_advice"
    assert client.calls == []


def test_tool_errors_go_back_to_the_model_which_can_retry(owner):
    agent, _, _ = make_agent(owner, [
        calls(("add_task", {"pet_name": "Max", "description": "Walk", "time": "9am", "due_date": TOMORROW})),
        calls(("add_task", {"pet_name": "Max", "description": "Walk", "time": "09:00", "due_date": TOMORROW})),
        calls(("check_conflicts", {})),
        say("I've added Max's walk at 09:00.\nCONFIDENCE: 0.9 | retried with a valid time"),
    ])
    result = agent.run("Walk Max at 9am tomorrow")
    assert [s.ok for s in result.steps] == [False, True, True]
    assert "invalid time" in result.steps[0].result["error"]
    assert not result.failed


def test_tool_guardrail_blocks_far_future_date(owner):
    agent, _, _ = make_agent(owner, [
        calls(("add_task", {"pet_name": "Max", "description": "Walk", "time": "09:00", "due_date": "2030-01-01"})),
        say("That date is too far ahead. Did you mean 2026?\nCONFIDENCE: 0.6 | needs confirmation"),
    ])
    result = agent.run("Walk Max at 9 on Jan 1 2030")
    assert result.steps[0].blocked and not result.steps[0].ok
    assert len(owner.pets[0].tasks) == 1  # nothing added


def test_call_cap_stops_runaway_loop_and_rolls_back(owner):
    script = [calls(("add_task", {"pet_name": "Max", "description": f"Walk {i}", "time": f"1{i}:00", "due_date": TOMORROW}))
              for i in range(10)]
    script.append(calls(("check_conflicts", {})))
    agent, _, saves = make_agent(owner, script, max_tool_calls=10)
    result = agent.run("Add lots of walks")
    assert result.failed and "limit of 10 tool calls" in result.error
    assert len(owner.pets[0].tasks) == 1  # all 10 adds rolled back
    assert saves == []
    assert result.actions_taken == []


def test_api_error_rolls_back_and_saves_nothing(owner):
    agent, _, saves = make_agent(owner, [
        calls(("add_task", {"pet_name": "Max", "description": "Walk", "time": "09:00", "due_date": TOMORROW})),
        LLMError("Gemini free-tier rate limit reached."),
    ])
    result = agent.run("Walk Max at 9 tomorrow")
    assert result.failed and "rate limit" in result.reply
    assert len(owner.pets[0].tasks) == 1
    assert saves == []


def test_remove_waits_for_confirmation(owner):
    agent, _, saves = make_agent(owner, [
        calls(("list_tasks", {"pet_name": "Mochi"})),
        calls(("remove_task", {"ref": "Mochi:0"})),
        say("Mochi's grooming is ready to be removed. Please confirm.\nCONFIDENCE: 0.9 | queued"),
    ])
    result = agent.run("Delete Mochi's grooming")
    assert len(result.pending_actions) == 1
    assert len(owner.pets[1].tasks) == 1  # not deleted yet
    assert saves == []  # queuing a deletion is not a saved change
    assert result.output_warnings == []
    assert execute_pending(owner, result.pending_actions[0])["ok"]
    assert owner.pets[1].tasks == []


def test_output_check_flags_a_false_claim(owner):
    agent, _, _ = make_agent(owner, [
        calls(("list_tasks", {})),
        say("I've added Max's walk at 09:00.\nCONFIDENCE: 0.9 | done"),
    ])
    result = agent.run("Add a walk for Max at 9 tomorrow")
    assert result.output_warnings and "add_task" in result.output_warnings[0]


def test_system_prompt_sections_and_few_shot_toggle(owner):
    with_examples = build_system_prompt(owner, TODAY, few_shot=True)
    without = build_system_prompt(owner, TODAY, few_shot=False)
    assert "Tuesday, 2026-11-10" in with_examples
    assert "Max (Beagle)" in with_examples
    assert "Example 2 - clash repair" in with_examples
    assert "Example" not in without
    assert build_system_prompt(owner, TODAY, few_shot=False, extra_sections=("RAG GOES HERE",)).endswith("RAG GOES HERE")


def test_few_shot_flag_reaches_the_model(owner):
    agent, client, _ = make_agent(owner, [say("Hi!\nCONFIDENCE: 0.5 | greeting")], few_shot=False)
    agent.run("Hello")
    assert "Example 1" not in client.calls[0]["system"]


@pytest.mark.parametrize(
    "text, reply, score, reason",
    [
        ("Done.\nCONFIDENCE: 0.8 | ok", "Done.", 0.8, "ok"),
        ("Done.\n**CONFIDENCE:** 1.0", "Done.", 1.0, None),
        ("Done.\nconfidence: 7 | too high", "Done.", 1.0, "too high"),  # clamped to 0-1
        ("Done, no score.", "Done, no score.", None, None),
    ],
)
def test_split_confidence(text, reply, score, reason):
    assert split_confidence(text) == (reply, score, reason)


def test_every_step_is_logged_with_run_id(owner, tmp_path):
    log_path = tmp_path / "agent.jsonl"
    logger = setup_agent_logger(log_path)
    client = FakeClient([
        calls(("add_task", {"pet_name": "Max", "description": "Walk", "time": "09:00", "due_date": TOMORROW})),
        calls(("check_conflicts", {})),
        say("I've added Max's walk.\nCONFIDENCE: 0.9 | ok"),
    ])
    result = PawPalAgent(owner, client, logger=logger, today=TODAY).run("Walk Max at 9 tomorrow")
    for handler in logger.handlers:
        handler.flush()
    events = [r["event"] for r in read_run(result.run_id, log_path)]
    assert events[0] == "run_start" and events[-1] == "run_end"
    assert events.count("tool_call") == 2 and events.count("llm_call") == 3
    assert "self_check" in events
    for line in log_path.read_text().splitlines():
        json.loads(line)  # every line is valid JSON

    trace = export_trace(result.run_id, log_path=log_path, out_dir=tmp_path / "traces", title="Walk Max").read_text()
    assert trace.startswith("# Agent trace: Walk Max")
    assert "| model | fake-model |" in trace and "| today (as seen by the agent) | 2026-11-10 |" in trace
    assert "## Decision chain" in trace
    assert "decided to call `add_task`" in trace
    assert "🔧 **Tool** `add_task(" in trace and "Added Max's 'Walk' at 09:00 on 2026-11-11" in trace
    assert "**Self-check (code):** passed" in trace
    assert "## Raw log records" in trace and f'"run_id": "{result.run_id}"' in trace
    for handler in list(logger.handlers):
        if getattr(handler, "baseFilename", "") == str(log_path.resolve()):
            logger.removeHandler(handler)
            handler.close()


def test_json_formatter_merges_payload():
    import logging

    record = logging.LogRecord("x", logging.INFO, __file__, 1, "tool_call", None, None)
    record.payload = {"run_id": "abc", "tool": "add_task"}
    data = json.loads(JsonLineFormatter().format(record))
    assert data["event"] == "tool_call" and data["run_id"] == "abc" and data["tool"] == "add_task"
