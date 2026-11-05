from pawpal_system import Pet, Task


def test_mark_complete_changes_status():
    task = Task(description="Walk", time="08:00")
    assert task.completed is False
    task.mark_complete()
    assert task.completed is True


def test_add_task_increases_pet_task_count():
    pet = Pet(name="Mochi", breed="Corgi")
    before = len(pet.tasks)
    pet.add_task(Task(description="Feed", time="09:00"))
    assert len(pet.tasks) == before + 1


from datetime import date, timedelta

import pytest

from pawpal_system import Owner, Scheduler


def make_scheduler():
    owner = Owner(name="Sam")
    buddy = Pet(name="Buddy", breed="Lab")
    mia = Pet(name="Mia", breed="Tabby")
    owner.add_pet(buddy)
    owner.add_pet(mia)
    return Scheduler(owner), buddy, mia


def test_time_is_normalized_so_sorting_is_correct():
    sched, buddy, _ = make_scheduler()
    late = Task("Dinner", "18:00")
    early = Task("Walk", "9:00")
    assert early.time == "09:00"
    assert [t.description for t in sched.sort_by_time([late, early])] == ["Walk", "Dinner"]


@pytest.mark.parametrize("bad", ["25:00", "12:60", "noon", ""])
def test_invalid_time_is_rejected(bad):
    with pytest.raises(ValueError):
        Task("Walk", bad)


def test_sort_breaks_time_ties_by_priority():
    sched, _, _ = make_scheduler()
    low = Task("Brush", "08:00", priority="low")
    high = Task("Meds", "08:00", priority="high")
    med = Task("Feed", "08:00")
    assert sched.sort_by_time([low, med, high]) == [high, med, low]


def test_filter_by_pet_name_is_case_insensitive():
    sched, buddy, mia = make_scheduler()
    walk = Task("Walk", "08:00")
    buddy.add_task(walk)
    mia.add_task(Task("Feed", "09:00"))
    assert sched.filter_tasks(pet_name="bUDdy") == [walk]
    assert sched.filter_tasks(pet_name="nobody") == []


def test_filter_by_completed_status():
    sched, buddy, _ = make_scheduler()
    done, pending = Task("Walk", "08:00"), Task("Feed", "09:00")
    buddy.add_task(done)
    buddy.add_task(pending)
    done.mark_complete()
    assert sched.filter_tasks(completed=True) == [done]
    assert sched.filter_tasks(pet_name="Buddy", completed=False) == [pending]


def test_daily_task_spawns_next_day():
    sched, buddy, _ = make_scheduler()
    task = Task("Walk", "08:00", frequency="daily")
    buddy.add_task(task)
    nxt = sched.mark_task_complete(buddy, task)
    assert nxt.due_date == task.due_date + timedelta(days=1)
    assert nxt.completed is False


def test_completing_twice_does_not_duplicate_recurring_task():
    sched, buddy, _ = make_scheduler()
    task = Task("Walk", "08:00", frequency="weekly")
    buddy.add_task(task)
    assert sched.mark_task_complete(buddy, task) is not None
    assert sched.mark_task_complete(buddy, task) is None
    assert len(buddy.tasks) == 2


def test_once_task_does_not_recur():
    sched, buddy, _ = make_scheduler()
    task = Task("Vet", "14:00")
    buddy.add_task(task)
    assert sched.mark_task_complete(buddy, task) is None
    assert len(buddy.tasks) == 1


def test_detect_conflicts_across_pets():
    sched, buddy, mia = make_scheduler()
    buddy.add_task(Task("Walk", "08:00"))
    mia.add_task(Task("Feed", "08:00"))
    mia.add_task(Task("Brush", "09:00"))
    conflicts = sched.detect_conflicts()
    assert len(conflicts) == 1 and "Walk" in conflicts[0] and "Feed" in conflicts[0]


def test_no_conflict_on_different_dates():
    sched, buddy, _ = make_scheduler()
    buddy.add_task(Task("Walk", "08:00"))
    buddy.add_task(Task("Walk", "08:00", due_date=date.today() + timedelta(days=1)))
    assert sched.detect_conflicts() == []


def test_daily_due_today_recurs_tomorrow():
    sched, buddy, _ = make_scheduler()
    task = Task("Walk", "08:00", frequency="daily")
    buddy.add_task(task)
    assert sched.mark_task_complete(buddy, task).due_date == date.today() + timedelta(days=1)


def test_weekly_due_today_recurs_in_seven_days():
    sched, buddy, _ = make_scheduler()
    task = Task("Groom", "10:00", frequency="weekly")
    buddy.add_task(task)
    assert sched.mark_task_complete(buddy, task).due_date == date.today() + timedelta(days=7)


def test_late_daily_task_recurs_from_today_not_the_past():
    sched, buddy, _ = make_scheduler()
    task = Task("Walk", "08:00", frequency="daily", due_date=date.today() - timedelta(days=3))
    buddy.add_task(task)
    assert sched.mark_task_complete(buddy, task).due_date == date.today() + timedelta(days=1)


def test_early_completion_keeps_future_schedule():
    sched, buddy, _ = make_scheduler()
    tomorrow = date.today() + timedelta(days=1)
    task = Task("Walk", "08:00", frequency="daily", due_date=tomorrow)
    buddy.add_task(task)
    assert sched.mark_task_complete(buddy, task).due_date == tomorrow + timedelta(days=1)


def test_conflict_for_same_pet_names_the_pet():
    sched, buddy, _ = make_scheduler()
    buddy.add_task(Task("Walk", "08:00"))
    buddy.add_task(Task("Bath", "08:00"))
    (warning,) = sched.detect_conflicts()
    assert "Buddy: Walk" in warning and "Buddy: Bath" in warning


# ---- Empty cases ----

def test_empty_inputs_return_empty_results():
    empty_owner = Scheduler(Owner(name="Nobody"))
    assert empty_owner.sort_by_time([]) == []
    assert empty_owner.filter_tasks() == []
    assert empty_owner.filter_tasks(pet_name="Buddy", completed=False) == []
    assert empty_owner.get_todays_schedule() == []
    assert empty_owner.detect_conflicts() == []


def test_pet_with_no_tasks_is_handled():
    sched, buddy, mia = make_scheduler()
    mia.add_task(Task("Feed", "09:00"))
    assert sched.filter_tasks(pet_name="Buddy") == []
    assert [t.description for t in sched.get_todays_schedule()] == ["Feed"]
    assert sched.detect_conflicts() == []


# ---- Sorting ----

def test_sort_single_task_and_boundary_times():
    sched, _, _ = make_scheduler()
    only = Task("Walk", "12:00")
    assert sched.sort_by_time([only]) == [only]
    midnight, last = Task("Late", "23:59"), Task("Early", "00:00")
    assert sched.sort_by_time([midnight, last]) == [last, midnight]


def test_sort_is_stable_for_equal_time_and_priority():
    sched, _, _ = make_scheduler()
    a, b, c = Task("A", "08:00"), Task("B", "08:00"), Task("C", "08:00")
    assert sched.sort_by_time([b, c, a]) == [b, c, a]


# ---- Filtering ----

def test_filter_with_no_arguments_returns_everything():
    sched, buddy, mia = make_scheduler()
    buddy.add_task(Task("Walk", "08:00"))
    mia.add_task(Task("Feed", "09:00"))
    assert len(sched.filter_tasks()) == 2


def test_filter_matches_every_pet_sharing_a_name():
    owner = Owner(name="Sam")
    first, second = Pet("Max", "Lab"), Pet("max", "Pug")
    owner.add_pet(first)
    owner.add_pet(second)
    first.add_task(Task("Walk", "08:00"))
    second.add_task(Task("Feed", "09:00"))
    assert len(Scheduler(owner).filter_tasks(pet_name="MAX")) == 2


# ---- Recurring ----

def test_next_occurrence_keeps_fields_and_original_is_only_completed():
    sched, buddy, _ = make_scheduler()
    task = Task("Meds", "07:30", frequency="daily", priority="high")
    buddy.add_task(task)
    nxt = sched.mark_task_complete(buddy, task)
    assert nxt is not task and nxt in buddy.tasks
    assert (nxt.description, nxt.time, nxt.frequency, nxt.priority) == (
        "Meds", "07:30", "daily", "high")
    assert nxt.completed is False
    assert task.completed is True and task.due_date == date.today()


def test_recurrence_chains():
    sched, buddy, _ = make_scheduler()
    task = Task("Walk", "08:00", frequency="daily")
    buddy.add_task(task)
    second = sched.mark_task_complete(buddy, task)
    third = sched.mark_task_complete(buddy, second)
    assert third.due_date == date.today() + timedelta(days=2)
    assert len(buddy.tasks) == 3


@pytest.mark.parametrize(
    "due, frequency, expected",
    [
        (date(2025, 12, 31), "daily", date(2026, 1, 1)),
        (date(2026, 1, 31), "daily", date(2026, 2, 1)),
        (date(2028, 2, 28), "daily", date(2028, 2, 29)),
        (date(2028, 2, 29), "daily", date(2028, 3, 1)),
        (date(2025, 12, 28), "weekly", date(2026, 1, 4)),
    ],
)
def test_recurrence_across_month_year_and_leap_boundaries(monkeypatch, due, frequency, expected):
    import pawpal_system

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return due

    monkeypatch.setattr(pawpal_system, "date", FrozenDate)
    sched, buddy, _ = make_scheduler()
    task = Task("Walk", "08:00", frequency=frequency, due_date=due)
    buddy.add_task(task)
    assert sched.mark_task_complete(buddy, task).due_date == expected


# ---- Conflicts ----

def test_three_tasks_in_one_slot_give_one_warning():
    sched, buddy, mia = make_scheduler()
    buddy.add_task(Task("Walk", "08:00"))
    buddy.add_task(Task("Bath", "08:00"))
    mia.add_task(Task("Feed", "08:00"))
    (warning,) = sched.detect_conflicts()
    assert all(x in warning for x in ("Buddy: Walk", "Buddy: Bath", "Mia: Feed", "08:00"))


def test_separate_clashes_give_separate_warnings():
    sched, buddy, mia = make_scheduler()
    for pet in (buddy, mia):
        pet.add_task(Task("Walk", "08:00"))
        pet.add_task(Task("Feed", "12:00"))
    assert len(sched.detect_conflicts()) == 2


def test_completed_tasks_do_not_trigger_conflicts():
    sched, buddy, mia = make_scheduler()
    done = Task("Walk", "08:00")
    buddy.add_task(done)
    mia.add_task(Task("Feed", "08:00"))
    assert len(sched.detect_conflicts()) == 1
    done.mark_complete()
    assert sched.detect_conflicts() == []


# ---- Today's schedule ----

def test_todays_schedule_excludes_completed_and_other_dates():
    sched, buddy, _ = make_scheduler()
    today_task = Task("Walk", "09:00")
    first = Task("Feed", "07:00")
    done = Task("Meds", "08:00")
    done.mark_complete()
    future = Task("Vet", "10:00", due_date=date.today() + timedelta(days=1))
    past = Task("Old", "06:00", due_date=date.today() - timedelta(days=1))
    for t in (today_task, first, done, future, past):
        buddy.add_task(t)
    assert sched.get_todays_schedule() == [first, today_task]


# ---- Validation ----

@pytest.mark.parametrize("bad", ["monthly", "Daily", "", "yearly"])
def test_invalid_frequency_is_rejected(bad):
    with pytest.raises(ValueError):
        Task("Walk", "08:00", frequency=bad)


@pytest.mark.parametrize("bad", ["urgent", "High", "", "0"])
def test_invalid_priority_is_rejected(bad):
    with pytest.raises(ValueError):
        Task("Walk", "08:00", priority=bad)


def test_none_time_is_rejected():
    with pytest.raises(TypeError):
        Task("Walk", None)


def test_removing_missing_task_raises():
    pet = Pet("Buddy", "Lab")
    with pytest.raises(ValueError):
        pet.remove_task(Task("Walk", "08:00"))


# ---- Next available slot ----

def test_next_available_slot_on_an_empty_day_is_the_start():
    sched, _, _ = make_scheduler()
    assert sched.next_available_slot() == "00:00"
    assert sched.next_available_slot(after="08:00") == "08:00"


def test_next_available_slot_skips_taken_times():
    sched, buddy, mia = make_scheduler()
    buddy.add_task(Task("Walk", "08:00"))
    mia.add_task(Task("Feed", "08:15"))
    assert sched.next_available_slot(after="08:00") == "08:30"
    assert sched.next_available_slot(after="08:00", step_minutes=60) == "09:00"


def test_next_available_slot_ignores_completed_tasks_and_other_dates():
    sched, buddy, _ = make_scheduler()
    done = Task("Walk", "08:00")
    done.mark_complete()
    buddy.add_task(done)
    buddy.add_task(Task("Feed", "08:15", due_date=date.today() + timedelta(days=1)))
    assert sched.next_available_slot(after="08:00") == "08:00"
    assert sched.next_available_slot(after="08:15") == "08:15"


def test_next_available_slot_is_none_when_day_is_full():
    sched, buddy, _ = make_scheduler()
    buddy.add_task(Task("A", "00:00"))
    buddy.add_task(Task("B", "12:00"))
    assert sched.next_available_slot(step_minutes=720) is None
    assert sched.next_available_slot(after="23:45") == "23:45"


def test_next_available_slot_rejects_bad_arguments():
    sched, _, _ = make_scheduler()
    with pytest.raises(ValueError):
        sched.next_available_slot(step_minutes=0)
    with pytest.raises(ValueError):
        sched.next_available_slot(after="25:00")


def test_conflict_warning_can_suggest_a_free_slot():
    sched, buddy, mia = make_scheduler()
    buddy.add_task(Task("Walk", "08:00"))
    mia.add_task(Task("Feed", "08:00"))
    mia.add_task(Task("Brush", "08:15"))
    (plain,) = sched.detect_conflicts()
    (hint,) = sched.detect_conflicts(suggest=True)
    assert "Next free slot" not in plain
    assert hint.startswith(plain) and hint.endswith("Next free slot: 08:30.")


def test_conflict_suggestion_when_no_slot_is_left():
    sched, buddy, mia = make_scheduler()
    buddy.add_task(Task("Walk", "23:45"))
    mia.add_task(Task("Feed", "23:45"))
    (hint,) = sched.detect_conflicts(suggest=True)
    assert hint.endswith("No free slot left that day.")


# ---- Persistence ----

def sample_owner():
    owner = Owner(name="Sam")
    rex = Pet("Rex", "Lab")
    mia = Pet("Mia", "Tabby")
    owner.add_pet(rex)
    owner.add_pet(mia)
    rex.add_task(Task("Walk", "08:00", frequency="daily", priority="high"))
    done = Task("Feed", "09:00", due_date=date(2026, 1, 31))
    done.mark_complete()
    rex.add_task(done)
    return owner


def test_json_round_trip_preserves_everything(tmp_path):
    path = tmp_path / "data.json"
    original = sample_owner()
    original.save_to_json(path)
    loaded = Owner.load_from_json(path)
    assert loaded == original
    assert loaded.pets[0].tasks[1].due_date == date(2026, 1, 31)
    assert loaded.pets[0].tasks[1].completed is True
    assert loaded.pets[1].tasks == []


def test_save_overwrites_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "data.json"
    sample_owner().save_to_json(path)
    Owner(name="Empty").save_to_json(path)
    assert Owner.load_from_json(path) == Owner(name="Empty")
    assert [p.name for p in tmp_path.iterdir()] == ["data.json"]


def test_loaded_data_still_schedules(tmp_path):
    path = tmp_path / "data.json"
    owner = sample_owner()
    owner.save_to_json(path)
    sched = Scheduler(Owner.load_from_json(path))
    walk = sched.owner.pets[0].tasks[0]
    nxt = sched.mark_task_complete(sched.owner.pets[0], walk)
    assert nxt.due_date == date.today() + timedelta(days=1)


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        Owner.load_from_json(tmp_path / "nope.json")


@pytest.mark.parametrize(
    "content",
    [
        "not json at all",
        "[]",
        '{"pets": []}',
        '{"name": "Sam", "pets": [{"name": "Rex", "breed": "Lab", "tasks": '
        '[{"description": "Walk", "time": "99:99", "due_date": "2026-01-01"}]}]}',
        '{"name": "Sam", "pets": [{"name": "Rex", "breed": "Lab", "tasks": '
        '[{"description": "Walk", "time": "08:00", "due_date": "yesterday"}]}]}',
    ],
)
def test_load_rejects_invalid_files_with_value_error(tmp_path, content):
    path = tmp_path / "data.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        Owner.load_from_json(path)
