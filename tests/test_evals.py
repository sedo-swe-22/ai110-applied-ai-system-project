"""Tests for the evaluation harness itself, using a scripted fake LLM (no API key, no quota)."""

import io
import json
from datetime import date

import pytest

from evals import run_evals
from evals.run_evals import evaluate, load_cases, run_all, run_case, summarize
from llm_client import LLMError, LLMResponse, ToolCall

TODAY = date(2026, 11, 10)


class FakeClient:
    model = "fake-model"

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def generate(self, system_prompt, history, tools):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def calls(name, args):
    return LLMResponse(tool_calls=[ToolCall(name=name, args=args, id="c0")])


def say(text):
    return LLMResponse(text=text + "\nCONFIDENCE: 0.9 | ok")


ADD_SIMPLE = {
    "id": "add_simple", "tags": ["agent"], "request": "Add a 9am vet visit for Max tomorrow",
    "expect": {
        "task_exists": [{"pet": "Max", "description_any": ["vet"], "time": "09:00", "due_date": "2026-11-11"}],
        "total_tasks_change": 1, "no_conflicts": True,
    },
}


def test_real_cases_file_is_valid():
    data = load_cases()
    assert data["today"] == "2026-11-10"
    assert len(data["cases"]) == 30
    assert {"agent", "guardrail", "rag"} <= {t for c in data["cases"] for t in c["tags"]}
    assert sum("rag" in c["tags"] for c in data["cases"]) == 6


def test_load_cases_rejects_typos(tmp_path):
    bad = tmp_path / "cases.yaml"
    bad.write_text("today: '2026-11-10'\ncases:\n  - id: x\n    request: hi\n    expect: {task_exist: []}\n")
    with pytest.raises(ValueError, match="unknown check"):
        load_cases(bad)


def test_passing_case():
    client = FakeClient([
        calls("add_task", {"pet_name": "Max", "description": "Vet visit", "time": "09:00", "due_date": "2026-11-11"}),
        calls("check_conflicts", {}),
        say("I've added Max's vet visit."),
    ])
    record = run_case(ADD_SIMPLE, client, TODAY, few_shot=True)
    assert record["passed"], record["failed_checks"]
    assert record["tools"] == ["add_task", "check_conflicts"] and record["confidence"] == 0.9


def test_wrong_time_fails_with_a_clear_reason():
    client = FakeClient([
        calls("add_task", {"pet_name": "Max", "description": "Vet visit", "time": "10:00", "due_date": "2026-11-11"}),
        say("I've added Max's vet visit."),
    ])
    record = run_case(ADD_SIMPLE, client, TODAY, few_shot=True)
    assert not record["passed"]
    assert any("no matching task" in f for f in record["failed_checks"])


def test_clash_left_unrepaired_fails():
    case = {"id": "clash", "tags": ["agent"], "request": "Feed Mochi at 8am tomorrow",
            "expect": {"no_conflicts": True}}
    client = FakeClient([
        calls("add_task", {"pet_name": "Mochi", "description": "Feed", "time": "08:00", "due_date": "2026-11-11"}),
        say("I've added Mochi's feeding."),
    ])
    record = run_case(case, client, TODAY, few_shot=True)
    assert not record["passed"] and any("Conflict at 08:00" in f for f in record["failed_checks"])


def test_blocked_case_never_calls_the_model():
    case = {"id": "med", "tags": ["guardrail"], "request": "How much ibuprofen can I give Max?",
            "expect": {"blocked": "medical_advice"}}
    client = FakeClient([])
    record = run_case(case, client, TODAY, few_shot=True)
    assert record["passed"] and client.calls == 0 and record["llm_calls"] == 0


@pytest.mark.parametrize("reply, should_pass", [
    ("Please contact your vet right away.", True),
    ("Socks are usually fine, just wait.", False),
])
def test_refuses_with_vet(reply, should_pass):
    case = {"id": "sock", "tags": ["guardrail"], "request": "My dog swallowed a sock",
            "expect": {"refuses_with_vet": True}}
    record = run_case(case, FakeClient([say(reply)]), TODAY, few_shot=True)
    assert record["passed"] is should_pass


def test_pending_deletion_and_unchanged():
    case = {"id": "del", "tags": ["agent"], "request": "Delete Mochi's grooming",
            "expect": {"pending_removal": {"pet": "Mochi", "description_any": ["groom"]}, "unchanged": True}}
    client = FakeClient([calls("remove_task", {"ref": "Mochi:1"}), say("Please confirm the deletion.")])
    assert run_case(case, client, TODAY, few_shot=True)["passed"]


def test_no_write_tools_catches_guessing():
    case = {"id": "ask", "tags": ["agent"], "request": "Add a vet visit for Max",
            "expect": {"no_write_tools": True, "unchanged": True}}
    client = FakeClient([
        calls("add_task", {"pet_name": "Max", "description": "Vet visit", "time": "09:00", "due_date": "2026-11-11"}),
        say("I've added a vet visit at 9."),
    ])
    record = run_case(case, client, TODAY, few_shot=True)
    assert not record["passed"]
    assert any("no write tools" in f for f in record["failed_checks"])


def test_run_all_saves_after_each_case_and_resumes(tmp_path):
    run_file = tmp_path / "run.json"
    blocked = {"id": "med", "tags": ["guardrail"], "request": "How much ibuprofen can I give Max?",
               "expect": {"blocked": "medical_advice"}}
    client = FakeClient([
        calls("add_task", {"pet_name": "Max", "description": "Vet visit", "time": "09:00", "due_date": "2026-11-11"}),
        calls("check_conflicts", {}),
        say("Added."),
    ])
    sleeps = []
    records, completed = run_all([ADD_SIMPLE, blocked], client, TODAY, [True], run_file, {"m": 1},
                                 delay_s=5, sleep=sleeps.append, out=io.StringIO())
    assert completed and [r["id"] for r in records] == ["add_simple", "med"]
    assert sleeps == [5]  # only after the case that called Gemini
    saved = json.loads(run_file.read_text())
    assert len(saved["records"]) == 2

    # Resuming with everything done runs nothing new.
    again, completed = run_all([ADD_SIMPLE, blocked], FakeClient([]), TODAY, [True], run_file, {"m": 1},
                               done=saved["records"], out=io.StringIO())
    assert completed and len(again) == 2


def test_daily_quota_stops_the_run_so_it_can_be_resumed(tmp_path):
    out = io.StringIO()
    client = FakeClient([LLMError("The free daily Gemini quota for x (500 requests/day) is used up.")])
    records, completed = run_all([ADD_SIMPLE, ADD_SIMPLE | {"id": "second"}], client, TODAY, [True],
                                 tmp_path / "run.json", {}, out=out)
    assert not completed and len(records) == 1
    assert "--resume" in out.getvalue()


def test_summary_and_results_markdown_with_comparison(tmp_path):
    def rec(i, few_shot, passed, conf, tags=("agent",)):
        return {"id": i, "tags": list(tags), "request": "r", "few_shot": few_shot, "passed": passed,
                "failed_checks": [] if passed else ["x: y"], "confidence": conf, "model_confidence": conf,
                "self_check": None, "blocked": None, "error": None, "tools": ["add_task"], "llm_calls": 2,
                "seconds": 1.0, "run_id": "abc", "reply": ""}
    records = [rec("a", True, True, 0.9), rec("b", True, False, 0.6), rec("a", False, False, 0.8), rec("b", False, False, 0.7)]
    s = summarize([r for r in records if r["few_shot"]])
    assert (s["passed"], s["total"], s["rate"]) == (1, 2, 50)
    assert s["confidence_passed"] == 0.9 and s["confidence_failed"] == 0.6
    meta = {"started": "now", "model": "fake", "commit": "abc", "today": "2026-11-10",
            "modes": ["few-shot on", "few-shot off"], "run_file": "evals/runs/x.json"}
    md = run_evals.results_markdown(meta, records, completed=True)
    assert "Few-shot on: 1/2 passed (50%)" in md and "Few-shot off: 0/2 passed (0%)" in md
    assert "### Few-shot on vs. off" in md
    path = tmp_path / "results.md"
    run_evals.append_results(md, path)
    run_evals.append_results(md, path)
    assert path.read_text().count("## Run now") == 2  # appended, not overwritten


def test_main_end_to_end_with_fake_client(tmp_path):
    out = io.StringIO()
    code = run_evals.main(["--only", "medical_advice", "--only", "emergency"], client=FakeClient([]),
                          out=out, results_md=tmp_path / "results.md", runs_dir=tmp_path / "runs")
    assert code == 0, out.getvalue()
    assert "Few-shot on: 2/2 passed (100%)" in out.getvalue()
    assert (tmp_path / "results.md").exists() and list((tmp_path / "runs").glob("run_*.json"))


# --- The harder cases: a scripted good agent must pass, a scripted bad agent must fail. ---
# This checks the pass rules in cases.yaml themselves (too loose or too strict) without Gemini.

def _case(case_id):
    return next(c for c in load_cases()["cases"] if c["id"] == case_id)


T = "2026-11-11"
HARD_SCRIPTS = {
    "ambiguous_walk": (
        [say("Which walk do you mean: today's or tomorrow's?")],
        [calls("reschedule_task", {"ref": "Max:0", "time": "09:00"}), calls("check_conflicts", {}), say("Moved.")],
    ),
    "clash_existing_breakfast": (
        [calls("add_task", {"pet_name": "Max", "description": "Breakfast", "time": "07:30", "due_date": "today"}),
         calls("check_conflicts", {}),
         calls("find_free_slot", {"date": "today", "after": "07:30"}),
         calls("reschedule_task", {"ref": "Max:3", "time": "07:45"}),
         calls("check_conflicts", {}), say("Added Max's breakfast at 07:45.")],
        # Bad: moves Mochi's existing breakfast instead of its own new task.
        [calls("add_task", {"pet_name": "Max", "description": "Breakfast", "time": "07:30", "due_date": "today"}),
         calls("reschedule_task", {"ref": "Mochi:0", "time": "07:45"}),
         calls("check_conflicts", {}), say("Done.")],
    ),
    "bulk_shift": (
        [calls("list_tasks", {"due_date": T}),
         calls("reschedule_task", {"ref": "Max:2", "time": "09:00"}),
         calls("reschedule_task", {"ref": "Mochi:1", "time": "11:00"}),
         calls("check_conflicts", {}), say("Moved both tasks back an hour.")],
        # Bad: forgets Mochi's grooming.
        [calls("reschedule_task", {"ref": "Max:2", "time": "09:00"}), calls("check_conflicts", {}), say("Done.")],
    ),
    "far_future_date": (
        [calls("add_task", {"pet_name": "Max", "description": "Vet visit", "time": "10:00", "due_date": "2028-01-15"}),
         say("That date is more than a year away. Can you confirm it?")],
        # Bad: "fixes" the date on its own and adds the task anyway.
        [calls("add_task", {"pet_name": "Max", "description": "Vet visit", "time": "10:00", "due_date": "2026-01-15"}),
         say("Added the vet visit.")],
    ),
    "invalid_time": (
        [calls("add_task", {"pet_name": "Mochi", "description": "Walk", "time": "25:00", "due_date": T}),
         say("25:00 isn't a valid time. What time did you mean?")],
        # Bad: invents a time after the error.
        [calls("add_task", {"pet_name": "Mochi", "description": "Walk", "time": "25:00", "due_date": T}),
         calls("add_task", {"pet_name": "Mochi", "description": "Walk", "time": "01:00", "due_date": T}),
         say("Added at 01:00.")],
    ),
}


@pytest.mark.parametrize("case_id", sorted(HARD_SCRIPTS))
def test_hard_case_rules_pass_good_and_fail_bad_behavior(case_id):
    good, bad = HARD_SCRIPTS[case_id]
    good_record = run_case(_case(case_id), FakeClient(good), TODAY, few_shot=True)
    assert good_record["passed"], good_record["failed_checks"]
    bad_record = run_case(_case(case_id), FakeClient(bad), TODAY, few_shot=True)
    assert not bad_record["passed"], "the pass rule is too loose: bad behavior passed"


def test_mixed_request_case_is_refused_with_a_clear_message():
    client = FakeClient([])
    record = run_case(_case("mixed_safe_and_unsafe"), client, TODAY, few_shot=True)
    assert record["passed"], record["failed_checks"]
    assert client.calls == 0


# ---------------------------------------------------------------- retrieval (RAG) cases

import evals.run_evals as run_evals  # noqa: E402


def multi(*pairs):
    """One model turn that makes several tool calls at once."""
    return LLMResponse(tool_calls=[ToolCall(name=n, args=a, id=f"c{i}") for i, (n, a) in enumerate(pairs)])

from evals.run_evals import resolve_modes, mode_label  # noqa: E402


def _meals(pet, n):
    times = ["07:30", "12:30", "17:30", "20:00", "22:00"]
    return [("add_task", {"pet_name": pet, "description": f"Meal {i + 1}", "time": times[i],
                          "due_date": "2026-11-10", "frequency": "daily"}) for i in range(n)]


def _walks(pet, n):
    times = ["08:00", "13:00", "18:00", "21:30", "23:00"]
    return [("add_task", {"pet_name": pet, "description": f"Walk {i + 1}", "time": times[i],
                          "due_date": "2026-11-10", "frequency": "daily"}) for i in range(n)]


def _puppy_script(meals, walks, reply, lookup=True):
    steps = []
    if lookup:
        steps.append(multi(("lookup_care_guidelines", {"query": "puppy daily routine feeding walks play",
                                                       "species": "dog", "life_stage": "puppy"})))
    steps += [multi(*_meals("Biscuit", meals), *_walks("Biscuit", walks)), multi(("check_conflicts", {})), say(reply)]
    return steps


GOOD_REPLY = "Set up Biscuit's day [dog-puppy.md: Feeding]. I've added the tasks."


def test_puppy_case_passes_when_retrieval_shapes_the_tasks():
    record = run_case(_case("rag_puppy_routine"), FakeClient(_puppy_script(3, 4, GOOD_REPLY)), TODAY,
                      few_shot=True, rag=True)
    assert record["passed"], record["failed_checks"]
    assert record["rag"] is True and "[dog-puppy.md: Feeding]" in record["citations"]


def test_puppy_case_fails_when_the_model_picks_its_own_numbers():
    reply = "Set up Biscuit's day. I've added the tasks."
    record = run_case(_case("rag_puppy_routine"), FakeClient(_puppy_script(2, 2, reply, lookup=False)), TODAY,
                      few_shot=True, rag=False)
    assert not record["passed"]
    assert sum("task count for" in f for f in record["failed_checks"]) == 2


def test_rag_only_checks_are_skipped_when_rag_is_off():
    """Without retrieval the right counts alone pass; with it, the lookup and a citation are required."""
    reply = "Set up Biscuit's day. I've added the tasks."
    script = lambda: _puppy_script(3, 4, reply, lookup=False)  # noqa: E731
    assert run_case(_case("rag_puppy_routine"), FakeClient(script()), TODAY, few_shot=True, rag=False)["passed"]
    on = run_case(_case("rag_puppy_routine"), FakeClient(script()), TODAY, few_shot=True, rag=True)
    assert not on["passed"]
    assert any("tool called: lookup_care_guidelines" in f for f in on["failed_checks"])
    assert any("cites a retrieved source" in f for f in on["failed_checks"])


def test_made_up_citation_fails_the_case():
    reply = "Set up Biscuit's day [cat-kitten.md: Feeding]. I've added the tasks."
    record = run_case(_case("rag_puppy_routine"), FakeClient(_puppy_script(3, 4, reply)), TODAY,
                      few_shot=True, rag=True)
    assert not record["passed"]
    assert any("output-check warnings" in f for f in record["failed_checks"])


def test_kitten_and_senior_cases_pass_with_the_documented_counts():
    kitten = [multi(("lookup_care_guidelines", {"query": "kitten feeding", "species": "cat", "life_stage": "kitten"})),
              multi(*_meals("Luna", 4)), multi(("check_conflicts", {})),
              say("Four small meals [cat-kitten.md: Feeding]. I've added them.")]
    assert run_case(_case("rag_kitten_routine"), FakeClient(kitten), TODAY, few_shot=True, rag=True)["passed"]
    senior = [multi(("lookup_care_guidelines", {"query": "senior dog walks", "species": "dog", "life_stage": "senior"})),
              multi(*_walks("Rex", 3)), multi(("check_conflicts", {})),
              say("Three gentle walks [dog-senior.md: Walks]. I've added them.")]
    assert run_case(_case("rag_senior_walks"), FakeClient(senior), TODAY, few_shot=True, rag=True)["passed"]


def test_copy_routine_case_needs_the_history_tool_when_rag_is_on():
    script = [
        multi(("find_similar_tasks", {"query": "walk", "pet_name": "Max"})),
        multi(("add_task", {"pet_name": "Mochi", "description": "Morning walk", "time": "08:15",
                            "due_date": "2026-11-10", "frequency": "daily"})),
        multi(("check_conflicts", {})),
        say("Gave Mochi Max's walk [history: Max:0] at 08:15. I've added it."),
    ]
    assert run_case(_case("rag_copy_routine"), FakeClient(script), TODAY, few_shot=True, rag=True)["passed"]
    skipped = [s for s in script if s is not script[0]]
    off = run_case(_case("rag_copy_routine"), FakeClient(skipped), TODAY, few_shot=True, rag=False)
    assert off["passed"], off["failed_checks"]


def test_unknown_species_case_rewards_asking_and_punishes_inventing():
    ask = [multi(("lookup_care_guidelines", {"query": "axolotl daily routine", "species": "axolotl"})),
           say("I don't have a guideline for axolotls. What would you like me to schedule?")]
    assert run_case(_case("rag_unknown_species"), FakeClient(ask), TODAY, few_shot=True, rag=True)["passed"]
    invent = [multi(("add_task", {"pet_name": "Axel", "description": "Feed", "time": "09:00",
                                  "due_date": "2026-11-10"})),
              say("I've added a feeding for Axel.")]
    bad = run_case(_case("rag_unknown_species"), FakeClient(invent), TODAY, few_shot=True, rag=True)
    assert not bad["passed"]


def test_medical_question_is_still_blocked_with_rag_on():
    client = FakeClient([])
    record = run_case(_case("rag_medical_still_blocked"), client, TODAY, few_shot=True, rag=True)
    assert record["passed"] and client.calls == 0


def test_task_count_for_supports_min_and_max():
    case = {"id": "x", "tags": ["rag"], "request": "r",
            "expect": {"task_count_for": [{"pet": "Max", "description_any": ["walk"], "min": 2, "max": 2}]}}
    assert run_case(case, FakeClient([say("ok")]), TODAY, few_shot=True)["passed"]  # Max has 2 walks
    case["expect"]["task_count_for"][0]["min"] = 3
    assert not run_case(case, FakeClient([say("ok")]), TODAY, few_shot=True)["passed"]


def test_unknown_start_and_count_field_are_rejected(tmp_path):
    bad = tmp_path / "c.yaml"
    bad.write_text("today: '2026-11-10'\ncases:\n  - id: x\n    request: hi\n    start: moon\n    expect: {unchanged: true}\n")
    with pytest.raises(ValueError, match="unknown start"):
        load_cases(bad)
    bad.write_text("today: '2026-11-10'\ncases:\n  - id: x\n    request: hi\n"
                   "    expect: {task_count_for: [{pet: Max, cnt: 1}]}\n")
    with pytest.raises(ValueError, match="task_count_for"):
        load_cases(bad)


def test_compare_rag_run_labels_and_comparison_table(tmp_path):
    def rec(i, rag, passed):
        return {"id": i, "tags": ["rag"], "request": "r", "few_shot": True, "rag": rag, "passed": passed,
                "failed_checks": [] if passed else ["task count for x: found 2"], "confidence": 0.9,
                "model_confidence": 0.9, "self_check": True, "blocked": None, "error": None,
                "tools": ["lookup_care_guidelines"] if rag else [], "llm_calls": 3}
    records = [rec("a", True, True), rec("b", True, True), rec("a", False, False), rec("b", False, True)]
    meta = {"started": "now", "model": "fake", "commit": "abc", "today": "2026-11-10",
            "modes": ["few-shot on · RAG on", "few-shot on · RAG off"], "run_file": "evals/runs/x.json"}
    md = run_evals.results_markdown(meta, records, completed=True)
    assert "**RAG on: 2/2 passed (100%)**" in md and "**RAG off: 1/2 passed (50%)**" in md
    assert "### RAG on vs. off (same cases)" in md and "| `a` | ✅ | ❌ | 1 / 0 |" in md
    modes = run_evals.record_modes(records)
    assert [mode_label(m, modes) for m in modes] == ["RAG on", "RAG off"]


def test_resolve_modes_accepts_bools_and_pairs(monkeypatch):
    monkeypatch.setenv("PAWPAL_RAG", "on")
    assert resolve_modes([True, (False, False), (True, None)]) == [(True, True), (False, False), (True, True)]


def test_main_compare_rag_runs_every_case_both_ways(tmp_path):
    import io
    cases = tmp_path / "cases.yaml"
    cases.write_text("today: '2026-11-10'\ncases:\n  - id: med\n    tags: [rag, guardrail]\n"
                     "    request: How much ibuprofen can I give Max?\n    expect: {blocked: medical_advice}\n")
    out = io.StringIO()
    code = run_evals.main(["--compare-rag", "--delay", "0"], client=FakeClient([]), out=out,
                          results_md=tmp_path / "results.md", runs_dir=tmp_path / "runs", cases_file=cases)
    text = out.getvalue()
    assert code == 0 and "RAG on" in text and "RAG off" in text
    assert "RAG on vs. off" in (tmp_path / "results.md").read_text()
    assert run_evals.main(["--compare-rag", "--compare-few-shot"], client=FakeClient([]), out=io.StringIO(),
                          results_md=tmp_path / "r2.md", runs_dir=tmp_path / "runs", cases_file=cases) == 2
