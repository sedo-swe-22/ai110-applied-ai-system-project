from pathlib import Path

import streamlit as st
from pawpal_system import Owner, Pet, Scheduler, Task

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
