# Final Project Plan: PawPal+ AI Agent

## Context
The final project asks for an upgrade to an earlier project that adds RAG, an agentic workflow or reliability testing, along with guardrails, logging and documentation. We chose **PawPal+** (from `../ai110-module2show-pawpal-starter/`) for three reasons:
- `Scheduler` in `pawpal_system.py` already has clean, UI-free methods that can become agent tools as they are.
- `Task.__post_init__` already rejects bad input, which gives the agent guardrails for free.
- The project already has a Streamlit UI and a 63-test pytest suite.

**Goal:** a chat assistant in which the user types requests like *"Add a 9am walk for Max tomorrow and fix any clashes."* The agent works in a loop: plan → call tools → check for conflicts → repair → summarize. Guardrails, structured logs and an eval suite measure how reliable it is.

**Two different AIs are involved:**
- **App LLM: Google Gemini.** PawPal+ calls Gemini through the official `google-genai` Python SDK (`from google import genai`), using **manual function calling** so every step is visible and can be logged. Automatic function calling is turned off so the app controls the loop.
  - The key is read from `GEMINI_API_KEY`.
  - The model comes from the `PAWPAL_MODEL` env var. Pick the default (a current Flash-tier model, which is cheap and fast for evals) from Google's model list when building step 2, and record it in the README.
- **Development assistant: Claude (Claude Code).** It helps design, write, test and document the code. It is not part of the running app.

### Free-tier constraints (Gemini free tier is the chosen plan)
- **Rate limits:** the free tier caps requests per minute and per day. The limits differ by model and change over time, so check the current numbers in Google AI Studio instead of hard-coding them. Each agent turn makes several requests (one per loop step), so one chat message can use 3–6.
- **Retry with backoff:** the Gemini adapter catches rate-limit errors (HTTP 429), waits and retries a few times, then gives up with a friendly message. Every retry is logged.
- **Eval pacing:** `run_evals.py` waits a configurable delay between cases (`EVAL_DELAY_SECONDS`) and can run a subset (`--only`, `--limit`). Keep the suite at about 15 cases so a full run fits within the daily quota. Save every run's results, so a run cut off by the quota can be resumed instead of starting over.
- **Keep loops short:** the 10-tool-call cap and a compact system prompt also save quota.
- **Development without spending quota:** all pytest tests use the fake client and never call Gemini. Only `run_evals.py` and manual app use touch the real API.
- **Privacy:** Google may use free-tier prompts to improve its products. Use made-up owner and pet data only, and say so in `model_card.md` and the README.

## Step 0: Bring PawPal+ into this repo (done)
- [x] Export the committed PawPal+ files with `git archive` (commit `327cc83`, pushed)
- [x] Add `pytest.ini` (`pythonpath = .`, `testpaths = tests`) so plain `pytest` can find the project modules
- [x] Commit and push `pytest.ini` (commit `65c2ee9`)

## Timeline
**Due: Monday, November 16, 2026, 2:59 AM EST.** In practice that's **Sunday night, Nov 15**. Aim to submit by **Sun Nov 15, 9 PM EST**, so there are ~6 hours of buffer.

| Date | Build step | Done when |
|---|---|---|
| Wed Nov 4 | Planning ✅ | Plan reviewed and committed |
| Thu Nov 5 | 1 · Tool layer + tests | All tools tested and the 63 old tests still pass |
| Fri Nov 6 – Sat Nov 7 | 2 · Logging + agent loop + Gemini adapter | Fake-client tests pass; **first real Gemini call works** (get the API key by Fri) |
| Sun Nov 8 | 3 · Guardrails | Guardrail tests pass (medical advice vs scheduling, cap, pending action, output check) |
| Mon Nov 9 | 4 · Streamlit chat + CLI | End-to-end request works in both |
| Tue Nov 10 – Wed Nov 11 | 5 · Evals + few-shot comparison + human eval | `evals/results.md` has at least one before/after run; the human-eval table is filled |
| **Wed Nov 11 (evening)** | **RAG go/no-go** | Go only if steps 1–5 are done and stable |
| Thu Nov 12 – Fri Nov 13 | 7 · RAG phase 2 (**if go**); otherwise polish + extra eval runs | RAG before/after recorded, or skipped deliberately |
| Sat Nov 14 | 6 · Docs | README, `model_card.md`, final diagram, `docs/traces/`, reflection |
| Sun Nov 15 | 8 · Presentation and portfolio + final checks | Fresh-clone test passes; slides or video done; **submitted by 9 PM EST** |

**Timeline notes**
- **Free-tier quota is per day.** Spread real-Gemini eval runs over Nov 10–13 instead of doing them all on one day. Don't leave evals until the weekend.
- **Docs come after RAG on purpose:** the diagram and README must match the final code. If RAG is a no-go, start docs on Thu Nov 12 for extra buffer.
- **Keep reflection notes daily.** It's much easier than reconstructing them on Nov 14.
- **If you fall behind,** cut in this order: RAG → few-shot bonus → human-eval size (minimum ~5 cases). Never cut tests, guardrails, README or the diagram; those are required points.

## Key design decisions (from the plan review)
| Topic | Decision |
|---|---|
| Interface | Streamlit chat **and** a CLI demo (`python -m pawpal_cli "..."`) |
| Task identity | **Tool-layer refs:** `list_tasks` returns short refs like `Max:2`. The core `Task` class and `data.json` stay unchanged |
| Medical questions | **Refuse advice, allow scheduling.** Block dosage, symptom and diagnosis questions with "please contact a vet" (plus an emergency note). "Give Max his meds at 8am" still works |
| Specialization bonus | **Yes:** a few-shot example section in the system prompt, compared with and without it using the eval harness |
| Destructive actions | The agent ends its turn with a *pending action*; the **app** carries it out when the user confirms (no second LLM call) |
| Output check | An "Actions taken" list is built from the **successful tool results**, not from the model's text; the reply is flagged if it contradicts that list |
| Confidence | **Hybrid:** code checks (no conflicts, the requested change exists) + the model's own rating, stored as separate fields |
| Dates | The tool layer takes an injectable `today`, so tests and evals don't depend on the real date |
| Failed run | An API error, an unhandled exception or hitting the 10-call cap → **roll back**; nothing is saved |
| Trace evidence | `logs/` stays git-ignored; curated sample traces are copied to `docs/traces/` (committed) and linked from `ai_interactions.md` |
| SDK details | Check the `google-genai` function-calling API, including how to turn off automatic calling, against current docs when building it, not from memory |

## New / changed files

### 1. `agent_tools.py`: tool layer over the existing Scheduler
Thin wrappers that return JSON-able dicts and **never raise**. On failure they return `{"ok": false, "error": ...}` so the model can recover. Each wrapper reuses:
- `list_pets` → `Owner.pets`
- `get_todays_schedule` → `Scheduler.get_todays_schedule`
- `list_tasks(pet_name?, completed?)` → `Scheduler.filter_tasks`; each task comes with a **ref** like `Max:2` (pet name + index in that pet's list)
- `add_task(pet_name, description, time, frequency, priority, due_date)` → `Task(...)` (its validation is reused) + `Pet.add_task`; returns the new ref
- `complete_task(ref)` → `Scheduler.mark_task_complete`
- `find_free_slot(date, after)` → `Scheduler.next_available_slot`
- `check_conflicts()` → `Scheduler.detect_conflicts(suggest=True)`
- `reschedule_task(ref, time?, due_date?)`: builds the updated task with `dataclasses.replace()`, so `Task.__post_init__` re-validates it
- `remove_task(ref)` → `Pet.remove_task`. This one is flagged **destructive** (see §3).

**Refs:** a ref is only valid until the next change. Any tool call that changes tasks returns fresh refs, and the system prompt tells the agent to call `list_tasks` again before using an old ref. An unknown or stale ref returns `{"ok": false, "error": "unknown ref"}`.

**Date relative to today:** `ToolContext(owner, today=date.today())` is passed to every tool. Words like "tomorrow" are resolved against `today`, and tests and evals pass a fixed date.

Also add `TOOL_SCHEMAS`, the Gemini function declarations (`types.FunctionDeclaration` wrapped in a `types.Tool`), passed via `GenerateContentConfig(tools=...)`. Keep the tool logic independent of the SDK, so only the schema and loop code know about Gemini.

### 2. `agent.py`: the agentic loop
- `PawPalAgent(owner, client, logger).run(user_message) -> AgentResult(reply, steps, blocked)`
- Loop: send the conversation with the tools → if the response has `function_call` parts, run each one → append a `function_response` part for each → repeat until the response has only text (no function calls).
- Wrap the Gemini client behind a small `LLMClient` interface. Fake-client tests and any future provider swap then only touch one adapter.
- The system prompt is built from sections: role, rules, today's context, an optional **few-shot examples** section, and a RAG section added in phase 2. The rules tell the agent to always call `check_conflicts` after any write and repair clashes with `find_free_slot`. Today's date and pet names are injected into the prompt.
- **Few-shot section (specialization bonus):** 3–4 short worked examples (request → tool calls → reply style). A flag (`PAWPAL_FEW_SHOT=on/off`) turns it on or off, so the eval harness can compare baseline and specialized runs.
- **Self-check and confidence:** after the last change, code re-runs `check_conflicts` and confirms the requested change exists, giving `checks_passed`. The model also returns `model_confidence` (0–1) plus a reason. Both are stored in `AgentResult` and shown in the UI and CLI.
- **Logging lives here too:** `logging_config.py` is built in this step (moved up from step 3), so the loop logs from day one.
- Persistence: call `owner.save_to_json()` only after a run succeeds. A run **fails** on an API error (after retries), an unhandled exception or hitting the 10-call cap. A failed run rolls back to a deep copy of the owner taken at the start, and nothing is saved.

### 3. `guardrails.py`
- **Input:** a length cap, plus a scope check that refuses off-topic requests. This is a cheap keyword/regex pre-filter, backed by system-prompt instructions.
- **Medical (refuse advice, allow scheduling):** block *advice* requests (dosage, symptoms, diagnosis, "is it safe to give…") with a fixed "please contact a vet" message, plus an emergency note for urgent words ("poisoned", "bleeding", "not breathing"). *Scheduling* medication ("give Max his meds at 8am", "remind me about flea treatment weekly") is allowed. Test both sides, including tricky phrasings.
- **Tool level:** an allowlist of tool names (taken from the registry), argument validation (the pet must exist, the ref must be valid, dates may not be more than 1 year in the past or future), and a per-run cap of 10 tool calls.
- **Destructive actions:** `remove_task` does not delete anything. It records a *pending action* and the agent ends its turn ("I'll remove Max's 9:00 walk — please confirm"). The UI shows Confirm/Cancel buttons, and the CLI asks `y/N`. On confirm, the **app** calls the delete directly and logs it. There is no second LLM call.
- **Output check:** an "Actions taken" list is built from the **successful tool results** and shown with every reply. Simple checks flag a reply that claims something the list doesn't support (e.g. says "added" with no successful `add_task`). Flags are logged and shown as a warning.

### 4. `logging_config.py`
Uses the standard-library `logging` module to write JSON lines to `logs/agent.jsonl`. It logs one record per user turn, per tool call (name, args, result, ms, ok/error), per guardrail trigger and per API error, all linked by a `run_id`. Add `logs/` to `.gitignore`. A small helper exports chosen runs to readable markdown in **`docs/traces/`**, which is committed and linked from `ai_interactions.md` for the +2 agentic bonus.

### 5. `app.py`: add a "🤖 Assistant" chat section
Use `st.chat_input` and `st.chat_message` to show the reply. Add an expander that lists the agent's steps (tool calls) so graders can see the workflow, and show the confirm button for destructive actions. If `GEMINI_API_KEY` is missing, show a friendly message instead. Also show the "Actions taken" list, the confidence and any output-check warning.

### 5b. `pawpal_cli.py`: CLI demo
`python -m pawpal_cli "Add a 9am walk for Max tomorrow"` prints the reply, the actions taken, the step trace and the confidence. It uses the same `PawPalAgent` and `data.json` as the app, so it's the same system, not a separate demo. Options: `--data PATH` (use a demo data file, so real data isn't touched), `--today YYYY-MM-DD` (fixed date for repeatable README examples) and `--no-few-shot`. Destructive actions ask `y/N`. The README code-block examples are copied from real CLI runs.

### 6. Reliability testing
- `tests/test_agent_tools.py`: unit tests for every tool wrapper (no LLM). Cover bad times, unknown pets, an error never escaping as a raised exception, and so on.
- `tests/test_guardrails.py`: off-topic prompts; medical *advice* blocked while medication *scheduling* is allowed; the tool-call cap; the pending-action confirmation; output-check flags.
- `tests/test_agent_loop.py`: tests the loop with a **fake client** that returns scripted `function_call` responses. They check that conflict repair happens, that the step cap stops a runaway loop and rolls back, that an API error rolls back, and that `checks_passed` is computed correctly. These are deterministic and use no API key.
- `evals/cases.yaml` + `evals/run_evals.py`: about 15 tagged natural-language cases (`agent`, `guardrail`, later `rag`) run against the real model with a fixed `today`. Each case has a starting state and assertions on the final state, for example "Max has a walk at 09:00 tomorrow and there are no conflicts" or "the request was refused". The script prints the pass rate and average confidence, and adds the run to `evals/results.md` (date, model, few-shot on/off). `--compare-few-shot` runs every case both ways for the specialization comparison.

### 7. RAG: phase 2, designed in from the start
**Decision:** build the agent first. RAG is added later, but the design leaves room for it now.

**Room left in phase 1 (agent-only build)** so RAG can be added later without rewriting anything:
- **Tool registry:** `agent_tools.py` keeps tools in a registry (name → function + schema). Adding a tool means one new entry; the loop, guardrail allowlist and logging pick it up automatically.
- **Prompt in sections:** the system prompt is built from sections (role, rules, today's context). A "use retrieved guidelines and cite them" section can be added later.
- **Generic step log:** the step-trace format records any tool call, so retrieval calls show up in the UI, logs and `ai_interactions.md` with no extra work.
- **Eval tags:** `evals/cases.yaml` supports tags (e.g. `agent`, `guardrail`, `rag`), so RAG cases can be added and run separately for a before/after comparison.
- **Placeholder interface:** `retrieval.py` defines a `Retriever` interface. Phase 1 doesn't register it as a tool.

**Phase 2 (when the agent is solid):**
- Add `knowledge/`: about 6 short markdown docs on pet-care guidelines, such as how often to feed or walk by species and age.
- Implement `Retriever` with simple keyword or TF-IDF search, split into sections, and return each source's file and heading.
- Register a `lookup_care_guidelines(query)` tool.
- Make retrieval **change behavior**, as the requirements demand. For example, *"set up a routine for my new puppy"* should create tasks whose times and frequencies come from the retrieved docs, and the reply should cite them.
- Run the `rag` eval cases with retrieval off and then on, and record the before/after comparison in `model_card.md` for the +2 RAG bonus.

### 8. Documentation
- **Old Module 2 docs:** `README.md`, `reflection.md` and `ai_interactions.md` still hold the old PawPal content. Rewrite the README for the final project, moving the old description into an "Original project" section. In the other two files, keep the Module 2 content under a heading and add a new "Final project" section on top.
- Update `README.md` with:
  - **Original project:** PawPal+ (Module 2), its goal and its capabilities
  - **New features:** agent, guardrails, self-check/confidence, logging, evals, few-shot specialization
  - **Architecture:** `diagrams/architecture.mmd` embedded as a Mermaid block, updated to match the real code
  - **Setup / run / test steps:** venv, install, `.env` from `.env.example`, `pytest`, `streamlit run app.py`, `python -m pawpal_cli ...`, `python evals/run_evals.py`
  - **Sample input/output:** 2–3 real CLI runs in code blocks
  - **Guardrail examples table:** Input / Behavior / Result, from real runs
  - **Testing summary:** a few lines in the required style, with links to `evals/results.md`
  - **Known limitations**
- **`model_card.md` (required, needed for two bonuses):**
  - intended use, limitations, failure modes and safety choices (including the free-tier privacy note)
  - eval tables
  - the human-eval table
  - the few-shot baseline-vs-specialized comparison
  - the RAG before/after comparison (phase 2)
- `ai_interactions.md` links to `docs/traces/`. `reflection.md` collects the running reflection notes.
- Add `google-genai`, `python-dotenv` and `pyyaml` to `requirements.txt`.
- In the README, explain both AIs: Gemini powers the app, and Claude was the development assistant.

## Build order
0. ✅ Copy PawPal+ in and commit the baseline
1. Tool layer (`agent_tools.py`: registry, refs, `ToolContext.today`) + tests
2. Logging + agent loop (Gemini adapter, retry/backoff, prompt sections, self-check/confidence, rollback) + fake-client tests
3. Guardrails (input/medical, tool level, pending-action confirmation, output check) + tests
4. Interfaces: Streamlit chat + `pawpal_cli.py`
5. Evals: cases, harness, the few-shot on/off comparison, the human-eval table
6. Docs: README, `model_card.md`, the final diagram, `docs/traces/`, reflection
7. RAG phase 2 (knowledge docs, retriever, `lookup_care_guidelines` tool, before/after eval), if the go/no-go date allows
8. Presentation and portfolio entry (slides or demo video, screenshots, polished README)

## Verification
- `pytest`: the existing `tests/test_pawpal.py` and all new tests pass without an API key.
- `GEMINI_API_KEY=... python evals/run_evals.py`: prints the pass rate and average confidence, and adds the run to `evals/results.md`. `--compare-few-shot` prints both runs side by side.
- `python -m pawpal_cli --data demo.json --today 2026-11-10 "Add a 9am walk for Max tomorrow"`: prints the reply, the actions taken, the step trace and the confidence.
- `streamlit run app.py`:
  - Add a clashing task by chat and confirm the agent repairs it.
  - Try *"delete Max's walk"* and confirm nothing is deleted until you click Confirm.
  - Try an off-topic prompt and a medical *advice* prompt and confirm both are refused, while *"give Max his meds at 8am"* still works.
  - Check that `logs/agent.jsonl` has a matching record for each step.
- Fresh clone: new venv → `pip install -r requirements.txt` → `pytest` passes with no `.env`.

## Project Requirements (reference)
Your final project is about taking what you've already built and making it stronger, smarter, and more reliable. You'll extend one of your previous projects (from Modules 1-3) into a complete applied AI system.

**Example extensions**
| Extension idea | Added AI components |
|---|---|
| Add retrieval of external documentation and automated validation of answers | RAG + testing + guardrails |
| Integrate agentic planning and error-logging into the existing workflow | Agentic loop + logging |
| Extend explanation module with bias detection and evaluation metrics | RAG + validation |
| Add reliability scoring or self-critique loop | Testing + confidence scoring |

**Fit check**
- PawPal+ is from Module 2, so it meets the "Modules 1-3" requirement.
- This plan is mainly the second example, **"Agentic loop + logging,"** built into the existing Streamlit workflow.
- It also borrows from the first example: guardrails, testing and RAG over pet-care docs (phase 2).
- It also covers the fourth example: a **self-check step** (planned) re-verifies the final schedule and reports a hybrid confidence score (see §2).

## Functionality Requirements (reference)
Your project should do something useful with AI. For example:
- Summarize text or documents
- Retrieve information or data from a source
- Plan and complete a step-by-step task
- Help debug, classify, or explain something

To make your project more advanced, it must include at least one of the following AI features:

| Feature | What it means | Example |
|---|---|---|
| Retrieval-Augmented Generation (RAG) | Your AI looks up or retrieves information before answering. | A study bot that searches notes before generating a quiz question. |
| Agentic Workflow | Your AI can plan, act, and check its own work. | A coding assistant that writes, tests, and then fixes code automatically. |
| Fine-Tuned or Specialized Model | You use a model that's been trained or adjusted for a specific task. | A chatbot tuned to respond in a company's tone of voice. |
| Reliability or Testing System | You include ways to measure or test how well your AI performs. | A script that checks if your AI gives consistent answers. |

The feature should be fully integrated into the main application logic. It is not enough to have standalone script; the feature must meaningfully change how the system behaves or processes information. For example, if you add RAG, your AI should actively use the retrieved data to formulate its response rather than just printing the data alongside a standard answer.

Also, make sure your project:
- **Runs correctly and reproducibly:** If someone follows your instructions, it should work.
- **Includes logging or guardrails:** Your code should track what it does and handle errors safely.
- **Has clear setup steps:** Someone else should be able to run it without guessing what to install.

**Fit check**
- **Useful task:** "plan and complete a step-by-step task," which here means building and fixing a pet-care schedule.
- **AI feature:** an agentic workflow (plan → act with tools → check conflicts → fix), plus a reliability system (evals, guardrails).
- **Integrated:** the agent changes the real `Owner`/`Scheduler` data that the Streamlit UI shows and saves. It is not a side script.
- **If RAG is added:** retrieved guidelines must actually shape the tasks the agent creates (for example, feeding times for a puppy). Showing them next to the answer is not enough.
- **Reproducible:** use pinned `requirements.txt` and a `.env.example`. Tests must pass without an API key, and there must be a fresh-clone setup check.
- **Errors handled safely:** tool errors are returned to the model instead of crashing the app. API failures produce a friendly message and a log record, and a failed run rolls back changes.

## Design and Architecture (reference + draft)
**Requirement:** Show how your project is organized by creating a short system diagram. Your diagram should include:
- The main components (like retriever, agent, evaluator, or tester).
- How data flows through the system (input, then process, then output).
- Where humans or testing are involved in checking AI results.

**Draft diagram:** [`diagrams/architecture.mmd`](diagrams/architecture.mmd). This is the Mermaid source, which the rubric requires (a PNG alone is not enough). It renders correctly with `@mermaid-js/mermaid-cli`. Dashed boxes are RAG phase 2.

**Main components**
| Component | File | Role |
|---|---|---|
| UI | `app.py` + `pawpal_cli.py` | Takes the owner's request and shows the reply, schedule and step trace |
| Input guardrails | `guardrails.py` | Blocks off-topic, overly long and medical/emergency requests before any LLM call |
| Agent loop | `agent.py` | Plans → calls tools → checks conflicts → repairs; talks to Gemini |
| LLM | Gemini API (free tier) | Chooses which function to call and writes the final reply |
| Tools + tool guardrails | `agent_tools.py` | Tool registry; allowlist, argument checks and call cap; returns errors instead of raising |
| Core logic | `pawpal_system.py` | Existing `Scheduler` and `Task` validation (unchanged) |
| Output check | `guardrails.py` | Makes sure the reply doesn't claim actions that didn't succeed |
| Storage / logs | `data.json`, `logs/agent.jsonl` | Saved only on success; one log record per step |
| Retriever (phase 2) | `retrieval.py`, `knowledge/` | Pet-care guidelines the agent uses to create tasks |

**Data flow:** request → input guardrails → agent loop ⇄ Gemini → tools (with tool guardrails) → Scheduler → output check → reply, schedule and step trace → UI. Every step is logged, and data is saved only if the run succeeds.

**Where humans and testing check AI results**
1. **Owner confirmation:** destructive actions (`remove_task`) pause until the owner confirms in the UI.
2. **Owner review:** each reply shows the step trace, so the owner can see exactly what the agent did.
3. **pytest (no API key):** a fake LLM checks the tools, guardrails and loop logic every time the code changes.
4. **Eval harness:** `evals/run_evals.py` runs about 15 cases against real Gemini and reports a pass rate.
5. **Developer review:** eval results and logs are reviewed, then prompts, tools and guardrails are fixed and the evals run again.

**Keep it accurate:** the rubric checks that the diagram matches the real implementation. Update `architecture.mmd` at the end of the build (step 6, docs), remove anything not built, and embed it in the README.

## Reliability and Evaluation (reference + plan)
**Requirement:** Your AI should prove that it works, not just seem like it does. Include at least one way to test or measure its reliability, such as:
- Automated tests (e.g., unit tests or simple checks for key functions).
- Confidence scoring (the AI rates how sure it is).
- Logging and error handling (your code records what failed and why).
- Human evaluation (you or a peer review the AI's output).

Summarize your testing in a few lines, like:
> 5 out of 6 tests passed; the AI struggled when context was missing. Confidence scores averaged 0.8; accuracy improved after adding validation rules.

If you use human evaluation, document it in a parseable format such as a markdown table or structured text/JSON so your results can be read without watching a demo. For example:

| Test Input | Evaluation Criteria | Result |
|---|---|---|
| "Summarize this article" | Accurate, under 100 words | Pass |
| Empty input | Handles gracefully | Fail — crashed |

Put evaluation tables or structured test logs in README.md or model_card.md; keep them text-based and parseable, not screenshots.

**How this plan covers all four methods**
| Method | What we build | Evidence (text, not screenshots) |
|---|---|---|
| Automated tests | pytest for tools, guardrails and the agent loop (fake LLM), plus the original 63 PawPal tests | pytest summary line in the README |
| Eval harness | `evals/run_evals.py` runs about 15 tagged cases against real Gemini and checks the **final schedule state**, not just the reply text | Pass/fail table + summary in `evals/results.md` and `model_card.md` |
| Confidence scoring | **Upgraded from optional to planned:** after acting, the agent re-checks the schedule (`check_conflicts`, `list_tasks`) and reports `confidence` (0–1) with a reason. The harness records it per case | Average confidence, and how well confidence predicts pass/fail |
| Logging and error handling | `logs/agent.jsonl` records every tool call, guardrail hit, retry and error; tools return errors instead of crashing; failed runs roll back | A committed sample log, linked from `ai_interactions.md` |
| Human evaluation | The student reviews about 8 real replies against written criteria (correct schedule, clear reply, safe refusal) | Markdown table: Input / Criteria / Result / Notes |

**Show improvement, not just a score**
- Run the evals **before and after** a fix (e.g. adding a guardrail or a prompt rule) and record both runs. Example: "9/15 → 13/15 after adding the conflict-repair rule."
- Keep every run in `evals/results.md`, with the date, model and pass rate.
- Write the final summary in the required style. Example (to be filled from real runs only): *"X of Y eval cases passed; the agent struggled with … Confidence averaged … ; pass rate improved from … to … after …"*

**Rules**
- Never invent results. Every number and table row comes from a real run.
- Failures stay in the record. They feed the limitations section and the reflection.

## Division of Work: AI Assistant's Portion
The AI assistant (Claude Code) does the following. The student owns the design decisions, reviews and approves all changes, and does the reflection, presentation and final submission.

**Code** (§1–5b implemented Nov 6: 146 tests pass; verified live against Gemini and in Streamlit `AppTest`)
- [x] `agent_tools.py`: tool registry over `Scheduler`, task refs, `ToolContext.today`, plus `TOOL_SCHEMAS`
- [x] `agent.py`: the tool-use loop, prompt sections (including few-shot on/off), self-check/confidence, rollback and save-on-success
- [x] `llm_client.py` (new, split out of `agent.py`): `LLMClient` interface + `GeminiClient` with retry/backoff, server `retryDelay`, a 60s timeout and a low thinking level
- [x] `guardrails.py`: input/medical, tool-level, pending-action and output checks
- [x] `logging_config.py`: JSON-lines logging with a `run_id`, plus a trace export to `docs/traces/`
- [x] `app.py`: chat section, actions taken, step-trace expander, confirm/cancel, confidence, missing-key message
- [x] `pawpal_cli.py`: CLI demo with `--data`, `--reset-demo`, `--today`, `--no-few-shot`, `--model`, `--yes`/`--no` and `--export-trace`

**Implementation notes (differences from the plan)**
- `pawpal_system.py`: `get_todays_schedule()` and `mark_task_complete()` take an optional `today` parameter, so tests and evals can use a fixed date. Existing behavior is unchanged and all 63 original tests still pass.
- **Default model is `gemini-3.5-flash`.** Checked Nov 6 on the free-tier key: `gemini-2.5-flash` returned 404 ("no longer available to new users"); `gemini-3.8-flash` hit repeated 504/503 server errors; `gemini-3.5-flash` completed full runs.
- Live runs exposed two missing reliability features, both now fixed: a request **timeout** (a stuck call used to block forever) and a **lower thinking level** (long thinking caused 504 deadline errors). Good material for the reflection.
- The tests caught a bug: the confidence parser missed Gemini's markdown bold (`**CONFIDENCE:** 0.9`). Fixed.
- [ ] Phase 1: tool registry, prompt built in sections, eval tags and the `Retriever` interface (room for RAG)
- [ ] Phase 2: `knowledge/` docs, a retriever implementation and the `lookup_care_guidelines` tool
- [ ] Self-check / confidence step (now planned, not optional) + confidence recorded by the eval harness

**Testing and evaluation**
- [x] Unit tests for tools and guardrails, plus loop and CLI tests with a fake client (no API key): `tests/test_agent_tools.py`, `test_guardrails.py`, `test_agent_loop.py`, `test_cli.py`
- [ ] `evals/cases.yaml` (draft cases for the student to review) and `evals/run_evals.py` with a pass/fail summary
- [ ] Keep the existing 63 PawPal tests passing after every change

**Reproducibility**
- [ ] Pin `requirements.txt`, add `.env.example`, and update `.gitignore` (`logs/`, `.env`)
- [ ] Run a fresh-clone setup check (new venv → install → `pytest`) and fix anything that breaks

**Documentation drafts (the student reviews and edits them)**
- [ ] A Mermaid architecture diagram (`.mmd` + embedded in the README) that matches the final code
- [ ] README draft: base project, new features, setup, run and test steps, and sample input/output, using output from real runs
- [ ] A guardrail and evaluator examples table (input → behavior → result), taken from real runs
- [ ] `model_card.md` draft: intended use, limitations, safety choices, eval and human-eval tables, the few-shot baseline-vs-specialized comparison, and the RAG before/after (phase 2)
- [ ] Format a sample agent trace log so it can be committed and linked from `ai_interactions.md`

**Process rules for the AI assistant**
- Explain each step before building it, and wait for the student's go-ahead on design choices.
- Run tests after each step and report results honestly, including failures.
- Commit or push only when the student asks.
- Never invent eval numbers or sample outputs. Everything in the docs comes from real runs.

## Division of Work: Student's Portion

**Decisions (before and during building)**
- [x] Choose the LLM provider: Gemini for the app, Claude as the development assistant
- [x] Choose the Gemini tier: free (check the current rate limits in AI Studio)
- [x] Choose the scope: agent only first, with RAG as phase 2
- [x] Plan-review decisions: add a CLI, tool-layer task refs, refuse medical advice but allow scheduling, few-shot specialization
- [x] Set the timeline: due Mon Nov 16, 2:59 AM EST; RAG go/no-go Wed Nov 11
- [x] Get the Gemini API key (in local `.env` as `GEMINI_API_KEY`, git-ignored; done Nov 4)
- [ ] Make the RAG go/no-go call on **Wed Nov 11**
- [ ] Approve or change each step's design before it is built

**Accounts and running things**
- [x] Get a Gemini API key from Google AI Studio and put it in a local `.env` as `GEMINI_API_KEY`. Never share it in chat or commit it.
- [ ] Run the real-model evals and the Streamlit app, and check that the behavior makes sense
- [ ] Do the "someone else can run it" test: follow the README on a fresh clone exactly as written

**Review**
- [ ] Read and understand every file the AI writes, well enough to explain it in the presentation
- [ ] Review the draft eval cases and add a few of your own, especially tricky ones that might break it
- [ ] Human evaluation: review about 8 real agent replies against written criteria and fill in the markdown table (Input / Criteria / Result / Notes)
- [ ] Edit the AI's doc drafts into your own voice

**Writing that has to be yours**
- [ ] `reflection.md`: how you used AI, at least one suggestion you accepted and one you rejected or revised, and limitations and future improvements. Keep a running note as you go.
- [ ] `ai_interactions.md`: your record of the prompts you used and what happened, with a link to the committed trace log
- [ ] README "original project" section: confirm the draft is accurate

**Communicating the results**
- [ ] Screenshots of the app and a demo video (if one is required)
- [ ] Presentation slides and talk
- [ ] Portfolio entry and final submission

**Git**
- [ ] Decide when to commit and push

**Running reflection notes** (add to these as you go)
- Rejected or revised: the AI offered to commit the plan file together with `pytest.ini`; the student chose to commit `pytest.ini` alone.
- Accepted: chose PawPal+ over Music Recommender and Game Glitch, as the AI recommended.
- Accepted: used `git archive` to import the PawPal+ baseline, which left out caches and local data.

## Project Goals (reference)
From the project requirements page. By completing this project, you will be able to:
1. Extend and redesign a prior mini-project into a cohesive, end-to-end AI integrated system.
2. Implement modular components (retrieval, logic, or agentic planning) using Python.
3. Test and evaluate system reliability and guardrails through structured experiments.
4. Document and explain the AI's decision-making process clearly and responsibly.
5. Communicate results through a professional presentation and portfolio entry.

### How this plan maps to the goals
| Goal | Covered by | Gap to watch |
|---|---|---|
| 1. End-to-end AI system | Agent wired into the Streamlit app and CLI; PawPal's existing persistence is reused | Keep it one app, not a separate demo |
| 2. Modular components | Separate `agent_tools.py`, `agent.py`, `guardrails.py`, `logging_config.py` and an optional `retrieval.py` | Each module gets its own tests |
| 3. Structured experiments | `evals/` harness, fake-client tests, guardrail tests | Record results in `evals/results.md` (pass rate, failures and what changed) |
| 4. Explain AI decisions | Step trace in the UI, `logs/agent.jsonl`, `model_card.md` | Show *why* the agent chose each tool, not just what it did |
| 5. Presentation and portfolio | Build step 8: slides or a demo video, plus a portfolio write-up (clean README, screenshots) | Leave time for it after the docs |

## Grading Rubric (reference)
**Project 4: Applied AI System.** Total points: 21 + 8 bonus.

### Required features (21 pts)

**3 pts: Clear identification of the base project and its original scope**
- 1: Student identifies the original project they are extending.
- 1: Student provides a short description of the original system's goal and capabilities.
- 1: Description is accurate and sets clear context for extensions.

**3 pts: Substantial new AI feature added (RAG, Agent, Specialization, or Reliability Mechanism)**
- 1: Student adds at least one substantial AI feature such as RAG, a multi-step agent or planning workflow, specialized behavior (mini fine-tune or structured prompting), or a reliability harness (evaluation loop, guardrails, self-checking).
- 1: Feature is integrated into the working system (not an isolated demo).
- 1: Feature is functional and produces meaningful changes in system behavior.

**3 pts: System architecture diagram**
- 1: Architecture diagram is submitted as a Mermaid source file (.mmd or embedded in a .md file); a PNG image alone is not sufficient.
- 1: Diagram clearly illustrates data flow (input, then processing steps, then output).
- 1: Diagram matches actual project implementation (not theoretical).

**3 pts: Functional end-to-end system demonstration**
- 1: Student provides a working script or UI that demonstrates full system workflow.
- 1: README includes example commands and sample outputs in code blocks showing the system running end-to-end.
- 1: System responds consistently to at least 2-3 example inputs.

**3 pts: Reliability, evaluation, or guardrail component**
- 1: System includes a reliability mechanism such as input validation, output guardrails, self-critique or multi-model agreement, or an evaluation script that tests sample inputs.
- 1: Mechanism is functional and meaningfully improves reliability.
- 1: Student provides examples in markdown format (showing input, behavior, and result) demonstrating how the guardrail or evaluator behaves.

**3 pts: Documentation: README and setup instructions**
- 1: README clearly explains project goals and new features.
- 1: README contains step-by-step instructions to install, run, and test the system.
- 1: README includes sample input/output illustrating system behavior.

**3 pts: Reflection on AI collaboration and system design**
- 1: Student explains how they used AI during development (prompting, debugging, design).
- 1: Student identifies at least one AI suggestion they accepted and at least one they rejected or revised.
- 1: Student reflects on system limitations and future improvements.

### Stretch features (+8 pts)
- **+2: RAG enhancement (custom indexing or multi-source retrieval).** Retrieval is extended to include custom documents, sections, or multi-source retrieval. README.md or model_card.md documents the impact on output quality (a before/after example or a written description of how retrieval improved the results).
- **+2: Agentic workflow enhancement.** Multi-step reasoning with tool-calls, planning steps, or a decision-making chain is implemented. The agent's intermediate reasoning traces are saved in ai_interactions.md (embedded, or via a committed log file linked from it), not just printed to terminal.
- **+2: Fine-tuning or specialization behavior.** Specialized model behavior (few-shot patterns, synthetic datasets, or constrained tone/style) is demonstrated. model_card.md includes a comparison of baseline vs. specialized output showing measurable differences.
- **+2: Test harness or evaluation script.** A script that evaluates the system on multiple predefined inputs is built and runs successfully. The script prints a summary of results (pass/fail score, confidence, or similar).

### How this plan maps to the rubric
| Rubric item | Covered by | Gap to watch |
|---|---|---|
| Base project | README "Original project" section (PawPal+, module 2) | Write it before the new features |
| New AI feature | Agent loop (§1–2), wired into the Streamlit app (§5) | — |
| Architecture diagram | Mermaid `.mmd` + embedded in README (§8) | Update it at the end so it matches the real code |
| End-to-end demo | Streamlit chat + `pawpal_cli.py` (§5b) | Copy README code blocks from real CLI runs, using a fixed `--today` |
| Reliability / guardrails | `guardrails.py` (§3), tests (§6) | Add a markdown table of guardrail examples (input → behavior → result) |
| README / setup | §8 | Include install, run, test and sample I/O steps |
| Reflection | `reflection.md`, `ai_interactions.md` | Record AI suggestions accepted *and* rejected as you go |
| +2 RAG | §7 phase 2 | Needs a before/after example in `model_card.md`; depends on the go/no-go date |
| +2 Agentic | Agent loop + exported traces in `docs/traces/` | Link the traces from `ai_interactions.md` (`logs/` itself is git-ignored) |
| +2 Specialization | Few-shot prompt section (§2), `--compare-few-shot` (§6) | Baseline-vs-specialized table with measurable differences in `model_card.md` |
| +2 Eval harness | `evals/run_evals.py` (§6) | Must print a pass/fail summary |

## Open questions to decide before building
- ~~**LLM provider:**~~ **Decided:** Gemini for the app (`GEMINI_API_KEY`), Claude Code as the development assistant.
- ~~**Gemini usage limits:**~~ **Decided:** free tier. See "Free-tier constraints" in the Context section.
- ~~**Scope:**~~ **Decided:** start agent-only, with room for RAG as phase 2 (see §7).
- ~~**Interface:**~~ **Decided:** Streamlit chat + CLI (§5b).
- ~~**Medical guardrail:**~~ **Decided:** refuse advice, allow scheduling (§3).
- ~~**Specialization bonus:**~~ **Decided:** yes, a few-shot prompt section (§2, §6).
- ~~**Task identity:**~~ **Decided:** tool-layer refs (§1).
- ~~**RAG go/no-go:**~~ **Decided:** Wed Nov 11 (evening). Go only if steps 1–5 are done and stable.
- ~~**Timeline:**~~ **Decided:** due Mon Nov 16, 2:59 AM EST; target submission Sun Nov 15, 9 PM EST (see Timeline).

All open questions are resolved.
