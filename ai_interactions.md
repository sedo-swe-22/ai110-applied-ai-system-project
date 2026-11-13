# AI Interactions Log

## Final project: agent reasoning traces (committed)

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

## Agent Workflow (SF7)

> Document your experience using an AI agent (e.g., Cursor Agent, Claude, Copilot) to make multi-step changes autonomously.

**What task did you give the agent?**

From the list of stretch challenges, I asked the agent to pick the ones that would most improve the app. It recommended two and I approved them:

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
- **Design checks I reviewed rather than accepted blindly:** saving must happen before `st.rerun()` (which stops the script, so a save at the end of the file would be skipped), and the corrupt file is renamed instead of overwritten so the owner's data is not silently lost.
- **Left alone on purpose:** no serialization library (marshmallow) was added, because `to_dict`/`from_dict` on three small classes is simpler to read; durations are still not modelled, so the slot finder works on exact start times only.
- **Manual corrections:** none were needed to the code the agent wrote. *(Add any corrections you made yourself here.)*

---

## Prompt Comparison (SF11)

> Compare two different prompts (or two different models) on the same task.

| | Option A | Option B |
|-|----------|----------|
| **Model / tool used** | | |
| **Prompt** | | |
| **Response summary** | | |
| **What was useful** | | |
| **Problems noticed** | | |
| **Decision** | | |

**Which approach did you use in your final implementation and why?**

<!-- Your conclusion -->
