"""End-to-end CLI tests with a scripted fake LLM (no API key, no quota)."""

import io

import pytest

import pawpal_cli
from llm_client import LLMResponse, ToolCall
from logging_config import setup_agent_logger
from pawpal_system import Owner


class FakeClient:
    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)

    def generate(self, system_prompt, history, tools):
        return self.script.pop(0)


def calls(name, args):
    return LLMResponse(tool_calls=[ToolCall(name=name, args=args, id="c0")])


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """Keep tests away from the real .env and logs/agent.jsonl."""
    monkeypatch.setattr(pawpal_cli, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(pawpal_cli, "setup_agent_logger", lambda: setup_agent_logger(tmp_path / "agent.jsonl"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)


def run(argv, script):
    out = io.StringIO()
    code = pawpal_cli.main(argv, client=FakeClient(script), out=out)
    return code, out.getvalue()


def test_cli_add_prints_reply_actions_steps_and_confidence(tmp_path):
    data = tmp_path / "demo.json"
    code, text = run(
        ["--data", str(data), "--reset-demo", "--today", "2026-11-10", "Add a 9am vet visit for Max tomorrow"],
        [
            calls("add_task", {"pet_name": "Max", "description": "Vet visit", "time": "09:00", "due_date": "2026-11-11"}),
            calls("check_conflicts", {}),
            LLMResponse(text="I've added Max's vet visit at 09:00 on 2026-11-11.\nCONFIDENCE: 0.95 | clean"),
        ],
    )
    assert code == 0
    assert "PawPal+: I've added Max's vet visit" in text
    assert "Actions taken:" in text and "Added Max's 'Vet visit' at 09:00 on 2026-11-11" in text
    assert "1. add_task(" in text and "2. check_conflicts({}) -> ok" in text
    assert "Self-check: passed" in text and "Confidence: 0.95" in text
    saved = Owner.load_from_json(str(data))
    assert any(t.description == "Vet visit" for t in saved.pets[0].tasks)


def test_cli_blocked_request(tmp_path):
    code, text = run(["--data", str(tmp_path / "d.json"), "How much ibuprofen can I give Max?"], [])
    assert code == 0
    assert "[guardrail] request blocked (medical_advice)" in text


def test_cli_pending_delete_cancelled_with_no(tmp_path):
    data = tmp_path / "demo.json"
    code, text = run(
        ["--data", str(data), "--reset-demo", "--today", "2026-11-10", "--no", "Delete Mochi's grooming"],
        [
            calls("list_tasks", {"pet_name": "Mochi"}),
            calls("remove_task", {"ref": "Mochi:1"}),
            LLMResponse(text="Mochi's grooming is ready to be removed. Please confirm.\nCONFIDENCE: 0.9 | queued"),
        ],
    )
    assert code == 0 and "Cancelled:" in text
    assert any(t.description == "Grooming" for t in Owner.load_from_json(str(data)).pets[1].tasks)


def test_cli_pending_delete_confirmed_with_yes(tmp_path):
    data = tmp_path / "demo.json"
    code, text = run(
        ["--data", str(data), "--reset-demo", "--today", "2026-11-10", "--yes", "Delete Mochi's grooming"],
        [
            calls("remove_task", {"ref": "Mochi:1"}),
            LLMResponse(text="Ready to remove Mochi's grooming once you confirm.\nCONFIDENCE: 0.9 | queued"),
        ],
    )
    assert code == 0 and "Done: Removed Mochi's 'Grooming'" in text
    assert all(t.description != "Grooming" for t in Owner.load_from_json(str(data)).pets[1].tasks)


def test_cli_without_key_explains_setup(tmp_path):
    out = io.StringIO()
    code = pawpal_cli.main(["--data", str(tmp_path / "d.json"), "Hello"], out=out)
    assert code == 1 and "GEMINI_API_KEY is not set" in out.getvalue()
