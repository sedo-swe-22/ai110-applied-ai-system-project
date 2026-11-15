# AI Interactions Log

## Final project: agent reasoning traces

These are real runs of the PawPal+ agent (Gemini), exported by `logging_config.export_trace` from `logs/agent.jsonl`. Each trace shows the decision chain in order (model decision, tool call, result, self-check, final reply), with the raw log lines at the bottom. They are committed so the reasoning is on record, not only printed in a terminal.

| Trace | Request | What it shows |
|---|---|---|
| [`trace_new-puppy-routine-retrieval-on.md`](docs/traces/trace_new-puppy-routine-retrieval-on.md) | Set up a daily routine for my new puppy Biscuit (**retrieval on**) | `lookup_care_guidelines` first, then 9 `add_task` calls whose times and counts come from the retrieved `dog-puppy.md` sections, `check_conflicts`, and a reply that cites its sources |
| [`trace_new-puppy-routine-retrieval-off.md`](docs/traces/trace_new-puppy-routine-retrieval-off.md) | The same request (**retrieval off**, `--no-rag`) | The agent only calls `list_tasks` and asks what to schedule; nothing changes. The contrast with the trace above is the RAG before/after |
| [`trace_add-a-task-that-clashes-agent-repairs-it.md`](docs/traces/trace_add-a-task-that-clashes-agent-repairs-it.md) | Feed Mochi at 8am tomorrow | Clash repair: add, `check_conflicts`, `find_free_slot`, `reschedule_task`, re-check |
| [`trace_reschedule-an-existing-task.md`](docs/traces/trace_reschedule-an-existing-task.md) | Move Mochi's grooming to 11am | Look up the task ref, reschedule, check |
| [`trace_delete-needs-confirmation-owner-cancels.md`](docs/traces/trace_delete-needs-confirmation-owner-cancels.md) | Delete Mochi's grooming | `remove_task` only queues a pending deletion; nothing is deleted until the owner confirms |
| [`guardrail_misses/`](docs/traces/guardrail_misses/) | Three phrasings the keyword guardrail missed (grapes, a Spanish dose question, a swallowed sock) | The second layer (the system-prompt rule) still refused and pointed to a vet |

More evidence: [`docs/evidence/cli_runs.md`](docs/evidence/cli_runs.md) (recorded CLI runs), [`evals/results.md`](evals/results.md) (every eval run).

*The sections below are from the Module 2 project (PawPal+ starter) and are kept as written.*

---

> **Stretch features only.** Only fill in the sections that apply to stretch features you attempted. If you did not attempt a stretch feature, leave its section blank or delete it. This file is not required for the core project.

---

## Agent Workflow

> Document your experience using an AI agent (e.g., Cursor Agent, Claude, Copilot) to make multi-step changes autonomously.

**What task did you give the agent?**

From the list of stretch challenges, I asked Claude to pick the ones that would most improve the app. It recommended two and I approved them:

1. **Advanced algorithmic capability:** a "next available slot" finder so conflict warnings tell the owner where to move a task.
2. **Data persistence:** save pets and tasks to `data.json` with `save_to_json` and `load_from_json`, so the app remembers them between runs.

**What did the agent do?**

Files modified and what changed:

| File | Change |
|------|--------|
| `pawpal_system.py` | Added `Scheduler.next_available_slot()`, a `suggest` option on `detect_conflicts()`, `to_dict()`/`from_dict()` on `Task`, `Pet` and `Owner`, and `Owner.save_to_json()` / `Owner.load_from_json()` (atomic write, validation on load). |
| `app.py` | Load `data.json` on startup, save after every change, back up a corrupt file to `data.json.bak` and warn, and show the suggested free slot in conflict warnings. |
| `tests/test_pawpal.py` | 16 new tests (63 total): free-slot cases, conflict suggestions, JSON round trip, missing and invalid files. |
| `main.py` | Prints conflicts with suggestions and the next free slot. |
| `README.md`, `reflection.md`, `diagrams/uml_final.mmd` | Documented persistence and the slot finder; updated test counts and the UML. |
| `.gitignore` | Ignores `data.json` and `data.json.bak` (personal data). |

Steps taken: read the current code, proposed the two challenges, implemented the logic, wrote tests and ran `pytest`, drove the Streamlit app headlessly (`streamlit.testing.v1.AppTest`) to check that data survives a new session and that a corrupt file is handled, then updated the docs.

**What did you have to verify or fix manually?**

- **Verified by running:** all 63 tests pass; the headless UI run showed the conflict warning with `Next free slot: 08:15.`, data reloaded in a second session, and a corrupt `data.json` produced a warning plus a `.bak` file instead of an error.
- **Design checks I reviewed rather than accepted blindly:** I always started in plan mode, so Claude could not begin implementing before I approved the plan. Once Claude generated a plan from my requirements, we discussed it. After I understood the plan and verified it would work for what I wanted, I approved Claude to build it.
- **Left alone on purpose:** no serialization library (marshmallow) was added, because `to_dict`/`from_dict` on three small classes is simpler to read; durations are still not modelled, so the slot finder works on exact start times only.
- **Manual corrections:** none were needed to the code Claude wrote. When I needed a change, I asked Claude to make it and told it what I wanted.

---

## Prompt Comparison

> Compare two different prompts (or two different models) on the same task.

| | Option A | Option B |
|-|----------|----------|
| **Model / tool used** | `gemini-3.5-flash-lite`, baseline system prompt (role, rules, today's context) | Same model, same prompt plus a few-shot section with 4 worked examples |
| **Prompt** | Request: "Move Max's walk to 9am" (Max has two walks, today and tomorrow) | Same request, same data and date |
| **Response summary** | Called `list_tasks`, then `reschedule_task` on today's walk. Reply: "I've moved Max's Morning walk to 09:00 today." Confidence 1.00. | Called `list_tasks`, then asked: "Which day would you like to move Max's walk on—today or tomorrow?" Confidence 0.9. |
| **What was useful** | Nothing extra: the base prompt's rule 6 says to ask when several tasks match, but the model did not follow it. | The example of asking for missing information made it ask. Over two runs it also used 10% and 16% fewer model calls. |
| **Problems noticed** | It guessed the task, and the code self-check still passed. Only the eval caught it. | It mostly copied the confidence numbers from the examples (0.95, 0.9, 0.8), so the number says little. |
| **Decision** | Not used | Used |

**Which approach did you use in your final implementation and why?**

I used the few-shot version (Option B). It passed 24 of 24 cases against 23 of 24 for the baseline in the second run, it used fewer model calls, and it asked the right question on the ambiguous request. The details are in `model_card.md` section 9. This is from one run per mode, so the ambiguous-walk difference could vary on a repeat.
