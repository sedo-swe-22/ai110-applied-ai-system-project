# Final Project Plan: PawPal+ AI Agent

## Context
The final project asks for an upgrade to an earlier project that adds RAG, an agentic workflow or reliability testing, along with guardrails, logging and documentation. We chose **PawPal+** (from `../ai110-module2show-pawpal-starter/`) for three reasons:
- `Scheduler` in `pawpal_system.py` already has clean, UI-free methods that can become agent tools as they are.
- `Task.__post_init__` already rejects bad input, which gives the agent guardrails for free.
- The project already has a Streamlit UI and a 63-test pytest suite.

**Goal:** a chat assistant in which the user types requests like *"Add a 9am walk for Max tomorrow and fix any clashes."* The agent works in a loop: plan → call tools → check for conflicts → repair → summarize. Guardrails, structured logs and an eval suite measure how reliable it is.

The LLM is Claude via the Anthropic Python SDK, using a manual tool-use loop so every step is visible and can be logged. The default model is `claude-sonnet-5-5`, which can be changed with the `PAWPAL_MODEL` env var.

## Step 0: Bring PawPal+ into this repo (mostly done)
- [x] Export the committed PawPal+ files with `git archive` (commit `327cc83`, pushed)
- [x] Add `pytest.ini` (`pythonpath = .`, `testpaths = tests`) so plain `pytest` can find the project modules
- [ ] Commit and push `pytest.ini`

## New / changed files

### 1. `agent_tools.py`: tool layer over the existing Scheduler
Thin wrappers that return JSON-able dicts and **never raise**. On failure they return `{"ok": false, "error": ...}` so the model can recover. Each wrapper reuses:
- `list_pets` → `Owner.pets`
- `get_todays_schedule` → `Scheduler.get_todays_schedule`
- `list_tasks(pet_name?, completed?)` → `Scheduler.filter_tasks`
- `add_task(pet_name, description, time, frequency, priority, due_date)` → `Task(...)` (its validation is reused) + `Pet.add_task`
- `complete_task(pet_name, description, due_date?)` → `Scheduler.mark_task_complete`
- `find_free_slot(date, after)` → `Scheduler.next_available_slot`
- `check_conflicts()` → `Scheduler.detect_conflicts(suggest=True)`
- `reschedule_task(...)`: new helper that changes a task's time or date, then re-validates it
- `remove_task(...)` → `Pet.remove_task`. This one is flagged **destructive**.

Also add `TOOL_SCHEMAS`, the JSON-schema definitions for the Anthropic `tools=` parameter.

### 2. `agent.py`: the agentic loop
- `PawPalAgent(owner, client, logger).run(user_message) -> AgentResult(reply, steps, blocked)`
- Loop: send the message with the tools → run each `tool_use` block → send back `tool_result` → repeat until `stop_reason == "end_turn"`.
- The system prompt tells the agent to always call `check_conflicts` after any write and repair clashes with `find_free_slot`. Today's date and pet names are injected into the prompt.
- Persistence: call `owner.save_to_json()` only after a run succeeds. If the run fails, roll back to a deep copy of the owner taken at the start.

### 3. `guardrails.py`
- **Input:** a length cap, plus a scope check that refuses off-topic requests and gives a fixed response for medical or emergency questions ("contact a vet"). This is a cheap keyword/regex pre-filter, backed by system-prompt instructions.
- **Tool level:** an allowlist of tool names, argument validation (the pet must exist, dates may not be more than 1 year in the past or future), and a per-run cap of 10 tool calls.
- **Destructive actions:** `remove_task` returns `needs_confirmation`. The UI shows a confirm button, and the action runs only after the user confirms.
- **Output:** the final reply must not claim a task was added unless an `add_task` call actually succeeded. Check this by comparing the reply against the log of steps.

### 4. `logging_config.py`
Uses the standard-library `logging` module to write JSON lines to `logs/agent.jsonl`. It logs one record per user turn, per tool call (name, args, result, ms, ok/error), per guardrail trigger and per API error, all linked by a `run_id`. Add `logs/` to `.gitignore`.

### 5. `app.py`: add a "🤖 Assistant" chat section
Use `st.chat_input` and `st.chat_message` to show the reply. Add an expander that lists the agent's steps (tool calls) so graders can see the workflow, and show the confirm button for destructive actions. If `ANTHROPIC_API_KEY` is missing, show a friendly message instead.

### 6. Reliability testing
- `tests/test_agent_tools.py`: unit tests for every tool wrapper (no LLM). Cover bad times, unknown pets, an error never escaping as a raised exception, and so on.
- `tests/test_guardrails.py`: off-topic and medical prompts, the tool-call cap, and the destructive-action confirmation.
- `tests/test_agent_loop.py`: tests the loop with a **fake client** that returns scripted `tool_use` responses. They check that conflict repair happens, that the step cap stops a runaway loop and that a failed run rolls back. These are deterministic and use no API key.
- `evals/cases.yaml` + `evals/run_evals.py`: about 15 natural-language cases run against the real model. Each case has a starting state and assertions on the final state, for example "Max has a walk at 09:00 tomorrow and there are no conflicts" or "the request was refused". The script prints the pass rate and saves a report to `evals/results.md`. This gives the documentation a real reliability number.

### 7. Optional RAG stretch (only if time allows)
Add `knowledge/` with about 6 short markdown docs on pet-care guidelines, such as how often to feed or walk by species and age. Add a `lookup_care_guidelines(query)` tool that does simple TF-IDF or keyword retrieval over these docs, so the agent can cite them when asked something like *"set up a routine for my new puppy"*.

### 8. Documentation
- Update `README.md` with:
  - the architecture: an updated Mermaid diagram showing UI → Agent → Guardrails → Tools → Scheduler, with logging feeding into the log file
  - setup steps, including the env var
  - the guardrails table
  - eval results
  - known limitations
- Add `model_card.md` or a responsible-AI section covering intended use, failure modes and safety choices.
- Keep `ai_interactions.md` and `reflection.md` up to date.
- Add `anthropic` and `pyyaml` to `requirements.txt`.

## Build order
0. Copy PawPal+ in and commit the baseline
1. tools + their tests
2. agent loop + fake-client tests
3. guardrails + logging
4. Streamlit chat
5. evals
6. docs
7. RAG stretch
8. Presentation and portfolio entry (slides or demo video, screenshots, polished README)

## Verification
- `pytest`: the existing `tests/test_pawpal.py` and all new tests pass without an API key.
- `ANTHROPIC_API_KEY=... python evals/run_evals.py`: prints the pass rate and writes `evals/results.md`.
- `streamlit run app.py`:
  - Add a clashing task by chat and confirm the agent repairs it.
  - Try *"delete Max's walk"* and confirm the button appears.
  - Try an off-topic prompt and a medical prompt and confirm both are refused.
  - Check that `logs/agent.jsonl` has a matching record for each step.

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
- It also borrows from the first example: guardrails, testing and the optional RAG over pet-care docs.
- Optional extra (fourth example): add a **self-check step** before the agent replies. The agent re-reads the final schedule, confirms there are no conflicts and that the requested task exists, and reports a confidence level. This is cheap to build, since `check_conflicts` and `list_tasks` already exist.

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

## Division of Work: AI Assistant's Portion
The AI assistant (Claude Code) does the following. The student owns the design decisions, reviews and approves all changes, and does the reflection, presentation and final submission.

**Code**
- [ ] `agent_tools.py`: tool wrappers over `Scheduler`, plus `TOOL_SCHEMAS`
- [ ] `agent.py`: the tool-use loop, system prompt, rollback and save-on-success
- [ ] `guardrails.py`: input, tool-level, destructive-action and output checks
- [ ] `logging_config.py`: JSON-lines logging with a `run_id`
- [ ] `app.py`: chat section, step-trace expander, confirm button, missing-key message
- [ ] CLI demo entry point (for README code-block examples)
- [ ] Optional: the self-check / confidence step and `retrieval.py` with a `knowledge/` docs scaffold

**Testing and evaluation**
- [ ] Unit tests for tools and guardrails, plus loop tests with a fake client (no API key)
- [ ] `evals/cases.yaml` (draft cases for the student to review) and `evals/run_evals.py` with a pass/fail summary
- [ ] Keep the existing 63 PawPal tests passing after every change

**Reproducibility**
- [ ] Pin `requirements.txt`, add `.env.example`, and update `.gitignore` (`logs/`, `.env`)
- [ ] Run a fresh-clone setup check (new venv → install → `pytest`) and fix anything that breaks

**Documentation drafts (the student reviews and edits them)**
- [ ] A Mermaid architecture diagram (`.mmd` + embedded in the README) that matches the final code
- [ ] README draft: base project, new features, setup, run and test steps, and sample input/output, using output from real runs
- [ ] A guardrail and evaluator examples table (input → behavior → result), taken from real runs
- [ ] `model_card.md` draft: intended use, limitations, safety choices, and the baseline-vs-specialized comparison if that bonus is attempted
- [ ] Format a sample agent trace log so it can be committed and linked from `ai_interactions.md`

**Process rules for the AI assistant**
- Explain each step before building it, and wait for the student's go-ahead on design choices.
- Run tests after each step and report results honestly, including failures.
- Commit or push only when the student asks.
- Never invent eval numbers or sample outputs. Everything in the docs comes from real runs.

## Division of Work: Student's Portion

**Decisions (before and during building)**
- [ ] Answer the open questions: LLM provider and budget, agent only or agent + RAG, CLI, how strict the medical guardrail should be, and deadlines
- [ ] Approve or change each step's design before it is built

**Accounts and running things**
- [ ] Get an API key and put it in a local `.env`. Never share it in chat or commit it.
- [ ] Run the real-model evals and the Streamlit app, and check that the behavior makes sense
- [ ] Do the "someone else can run it" test: follow the README on a fresh clone exactly as written

**Review**
- [ ] Read and understand every file the AI writes, well enough to explain it in the presentation
- [ ] Review the draft eval cases and add a few of your own, especially tricky ones that might break it
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
| 5. Presentation and portfolio | **Not planned yet** | Add a final step: slides or a demo video, plus a portfolio write-up (clean README, screenshots) |

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
| End-to-end demo | Streamlit chat + **a CLI demo script** | Add a CLI (e.g. `python -m demo "..."`) so README can show commands and output in code blocks |
| Reliability / guardrails | `guardrails.py` (§3), tests (§6) | Add a markdown table of guardrail examples (input → behavior → result) |
| README / setup | §8 | Include install, run, test and sample I/O steps |
| Reflection | `reflection.md`, `ai_interactions.md` | Record AI suggestions accepted *and* rejected as you go |
| +2 RAG | §7 stretch | Needs a before/after example in README or model card |
| +2 Agentic | Agent loop + `logs/agent.jsonl` | **Commit** a sample trace log and link it from `ai_interactions.md` |
| +2 Specialization | Not planned yet | Option: few-shot system prompt, with a baseline-vs-specialized comparison in `model_card.md` |
| +2 Eval harness | `evals/run_evals.py` (§6) | Must print a pass/fail summary |

## Open questions to decide before building
- **LLM provider:** Claude, or whatever API key or provider the course gives you? Is there a cost budget for running evals?
- **Scope:** agent only, or agent + RAG? Mixing both is stronger but takes more time.
- **Interface:** chat inside the existing Streamlit app, or a separate CLI as well (easier to script evals)?
- **Guardrail strictness:** how should medical or emergency questions be handled? A fixed refusal, or a refusal plus general guidance?
- **Timeline:** set a deadline for each build step.
