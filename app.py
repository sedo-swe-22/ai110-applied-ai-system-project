import json
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from agent import AgentResult, PawPalAgent
from agent_tools import execute_pending
from llm_client import GeminiClient, LLMError
from logging_config import log_event, setup_agent_logger
from pawpal_system import Owner, Pet, Scheduler, Task

load_dotenv(Path(__file__).with_name(".env"))

st.set_page_config(page_title="PawPal+", page_icon="🐾", layout="centered")

st.title("🐾 PawPal+")

st.markdown(
    """
Welcome to PawPal+. Add pets and tasks below, then generate today's schedule.

The UI is wired to the classes in `pawpal_system.py`: `Owner`, `Pet`, `Task` and `Scheduler`.
"""
)

with st.expander("Scenario", expanded=True):
    st.markdown(
        """
**PawPal+** is a pet care planning assistant. It helps a pet owner plan care tasks
for their pet(s) based on constraints like time, priority, and preferences.

You will design and implement the scheduling logic and connect it to this Streamlit UI.
"""
    )

with st.expander("What you need to build", expanded=True):
    st.markdown(
        """
At minimum, your system should:
- Represent pet care tasks (what needs to happen, how long it takes, priority)
- Represent the pet and the owner (basic info and preferences)
- Build a plan/schedule for a day that chooses and orders tasks based on constraints
- Explain the plan (why each task was chosen and when it happens)
"""
    )

st.divider()

DATA_FILE = Path(__file__).with_name("data.json")


def load_owner() -> Owner:
    """Load saved data, or start fresh if there is none (or it is unreadable)."""
    try:
        return Owner.load_from_json(DATA_FILE)
    except FileNotFoundError:
        return Owner("Jordan")
    except ValueError:
        DATA_FILE.replace(DATA_FILE.with_suffix(".json.bak"))
        st.session_state.load_warning = (
            "Saved data could not be read, so a fresh start was made. "
            "The old file was kept as data.json.bak."
        )
        return Owner("Jordan")


def save() -> None:
    """Persist the current owner, pets and tasks to data.json."""
    owner.save_to_json(DATA_FILE)


# Streamlit reruns this script on every interaction, so keep the Owner in session_state.
if "owner" not in st.session_state:
    st.session_state.owner = load_owner()
owner = st.session_state.owner

if "load_warning" in st.session_state:
    st.warning(st.session_state.pop("load_warning"))

st.subheader("Owner & Pets")
owner.name = st.text_input("Owner name", value=owner.name)

with st.form("add_pet", clear_on_submit=True):
    pet_name = st.text_input("Pet name", value="Mochi")
    breed = st.text_input("Breed", value="Shiba Inu")
    if st.form_submit_button("Add pet") and pet_name.strip():
        owner.add_pet(Pet(pet_name.strip(), breed.strip()))
        save()

if owner.pets:
    st.write(f"{owner.name}'s pets: " + ", ".join(f"{p.name} ({p.breed})" for p in owner.pets))
else:
    st.info("No pets yet. Add one above.")

# ---------------------------------------------------------------------------
# 🤖 Assistant: the Gemini-powered agent (agent.py) working on the same data
# ---------------------------------------------------------------------------

st.divider()
st.subheader("🤖 Assistant")
st.caption(
    'Ask in plain English, e.g. "Add a 9am walk for Max tomorrow", "What\'s on today?" or '
    '"Move Mochi\'s grooming to 11:00". Every step is shown below the reply and logged to logs/agent.jsonl.'
)

agent_logger = setup_agent_logger()
st.session_state.setdefault("chat", [])
st.session_state.setdefault("pending", [])


def get_llm_client():
    """Create the Gemini client once per session; return None if the key is missing."""
    if "llm_client" not in st.session_state:
        try:
            st.session_state.llm_client = GeminiClient(logger=agent_logger)
        except LLMError as exc:
            st.session_state.llm_setup_error = str(exc)
            return None
    return st.session_state.llm_client


def render_result(result: AgentResult) -> None:
    """Show one agent reply with its actions, checks, confidence and step trace."""
    st.markdown(result.reply)
    if result.blocked:
        st.caption(f"🛡️ Blocked by an input guardrail ({result.block_reason}). Gemini was not called.")
        return
    if result.failed:
        st.error(f"Run failed and was rolled back: {result.error}")
    if result.actions_taken:
        st.markdown("**Actions taken**\n" + "\n".join(f"- {a}" for a in result.actions_taken))
    for warning in result.output_warnings:
        st.warning(warning, icon="🛡️")
    if not result.failed:
        check = {True: "✅ passed", False: "❌ failed", None: "n/a (nothing changed)"}[result.checks_passed]
        conf = f"{result.confidence:.2f}" if result.confidence is not None else "n/a"
        reason = f" ({result.confidence_reason})" if result.confidence_reason else ""
        st.caption(f"Self-check: {check} · Confidence: {conf}{reason}")
    with st.expander(f"Agent steps: {len(result.steps)} tool call(s), {result.llm_calls} model call(s) · run {result.run_id}"):
        if not result.steps:
            st.write("No tools were called.")
        for s in result.steps:
            status = "ok" if s.ok else f"error: {s.result.get('error')}"
            tag = " · blocked by guardrail" if s.blocked else ""
            st.markdown(f"**{s.n}. `{s.tool}`** → {status}{tag} ({s.ms} ms)")
            st.code(json.dumps({"args": s.args, "result": s.result}, indent=2, ensure_ascii=False), language="json")
        for line in result.check_details:
            st.text(line)


for message in st.session_state.chat:
    with st.chat_message(message["role"]):
        if message["role"] == "user":
            st.markdown(message["text"])
        else:
            render_result(message["result"])

if "assistant_flash" in st.session_state:
    st.success(st.session_state.pop("assistant_flash"))

# Destructive actions wait here until the owner confirms. The app (not the model) deletes.
for i, (run_id, action) in enumerate(list(st.session_state.pending)):
    st.warning(f"**Needs your confirmation:** {action.summary}", icon="🗑️")
    confirm_col, cancel_col = st.columns(2)
    if confirm_col.button("Confirm delete", key=f"confirm-{run_id}-{i}", type="primary"):
        outcome = execute_pending(owner, action)
        log_event(agent_logger, "pending_confirmed", run_id, action=action.summary,
                  ok=outcome["ok"], error=outcome.get("error"))
        st.session_state.pending.pop(i)
        if outcome["ok"]:
            save()
            st.session_state.assistant_flash = outcome["summary"]
        else:
            st.session_state.assistant_flash = f"Could not delete: {outcome['error']}"
        st.rerun()
    if cancel_col.button("Cancel", key=f"cancel-{run_id}-{i}"):
        log_event(agent_logger, "pending_cancelled", run_id, action=action.summary)
        st.session_state.pending.pop(i)
        st.session_state.assistant_flash = f"Cancelled: {action.summary}. Nothing was deleted."
        st.rerun()

llm_client = get_llm_client()
if llm_client is None:
    st.info(
        "The assistant needs a Gemini API key. Copy `.env.example` to `.env`, set "
        "`GEMINI_API_KEY`, then restart the app. The rest of PawPal+ works without it."
    )

prompt = st.chat_input("Ask PawPal+ to plan your pets' care…", disabled=llm_client is None)
if prompt and llm_client is not None:
    agent = PawPalAgent(owner, llm_client, logger=agent_logger, save_fn=save)
    with st.spinner("PawPal+ is planning…"):
        result = agent.run(prompt)
    st.session_state.chat.append({"role": "user", "text": prompt})
    st.session_state.chat.append({"role": "assistant", "result": result})
    st.session_state.pending = [(result.run_id, a) for a in result.pending_actions]
    st.rerun()  # redraw so the schedule below shows the agent's changes

st.markdown("### Tasks")

scheduler = Scheduler(owner)


def pet_name_of(task: Task) -> str:
    """Return the name of the pet that owns the task (tasks don't store their pet)."""
    return next(p.name for p in owner.pets if task in p.tasks)


def task_row(task: Task) -> dict:
    """Format a task as a table row."""
    return {
        "Time": task.time,
        "Pet": pet_name_of(task),
        "Task": task.description,
        "Priority": task.priority,
        "Repeats": task.frequency,
        "Due": task.due_date,
        "Status": "Done" if task.completed else "To do",
    }


if owner.pets:
    selected = st.selectbox("Pet", [p.name for p in owner.pets])
    pet = next(p for p in owner.pets if p.name == selected)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        description = st.text_input("Task", value="Morning walk")
    with col2:
        time = st.text_input("Time (HH:MM)", value="08:00")
    with col3:
        frequency = st.selectbox("Frequency", ["once", "daily", "weekly"])
    with col4:
        priority = st.selectbox("Priority", ["low", "medium", "high"], index=2)

    if st.button("Add task"):
        try:
            pet.add_task(Task(description, time, frequency=frequency, priority=priority))
        except ValueError:
            st.error("Time must be in HH:MM format, e.g. 08:00.")
        else:
            save()
            st.success(f"Added '{description}' for {pet.name} at {time}.")
else:
    st.info("Add a pet above to start adding tasks.")

st.divider()

st.subheader("Today's Schedule")

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))

conflicts = scheduler.detect_conflicts(suggest=True)
if conflicts:
    st.warning(
        f"**{len(conflicts)} scheduling conflict(s).** You can't do these at the same "
        "time, so move one task to the suggested free slot:"
    )
    for conflict in conflicts:
        st.warning(conflict, icon="⚠️")
elif owner.get_all_tasks():
    st.success("No scheduling conflicts.")

todays = scheduler.get_todays_schedule()
if todays:
    st.caption("Sorted by time; higher priority first when times match.")
    st.table([task_row(t) for t in todays])
    st.markdown("**Mark a task done**")
    for t in todays:
        label = f"{t.time}  {pet_name_of(t)}: {t.description}"
        if st.button(f"✔ {label}", key=f"done-{id(t)}"):
            owner_pet = next(p for p in owner.pets if t in p.tasks)
            nxt = scheduler.mark_task_complete(owner_pet, t)
            msg = f"Completed '{t.description}'."
            if nxt:
                msg += f" Next one is due {nxt.due_date}."
            st.session_state.flash = msg
            save()
            st.rerun()
else:
    st.info("Nothing scheduled for today.")

st.divider()

st.subheader("Browse All Tasks")

if owner.get_all_tasks():
    fcol1, fcol2 = st.columns(2)
    with fcol1:
        pet_filter = st.selectbox("Filter by pet", ["All pets"] + [p.name for p in owner.pets])
    with fcol2:
        status_filter = st.radio("Status", ["All", "To do", "Done"], horizontal=True)

    completed = {"All": None, "To do": False, "Done": True}[status_filter]
    matches = scheduler.filter_tasks(
        pet_name=None if pet_filter == "All pets" else pet_filter, completed=completed
    )
    if matches:
        st.table([task_row(t) for t in scheduler.sort_by_time(matches)])
    else:
        st.info("No tasks match these filters.")
else:
    st.info("No tasks yet.")

save()  # also persists edits such as a renamed owner
