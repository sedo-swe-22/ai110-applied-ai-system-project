# AI Interactions Log

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
