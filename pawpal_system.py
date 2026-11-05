from __future__ import annotations

import json
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Optional


FREQUENCIES = ("once", "daily", "weekly")
PRIORITIES = ("low", "medium", "high")


@dataclass
class Task:
    description: str
    time: str  # "HH:MM"
    frequency: str = "once"  # "once", "daily", or "weekly"
    due_date: date = field(default_factory=date.today)
    completed: bool = False
    priority: str = "medium"  # "low", "medium", or "high"

    def __post_init__(self) -> None:
        """Normalize time to zero-padded HH:MM and reject invalid field values."""
        self.time = datetime.strptime(self.time, "%H:%M").strftime("%H:%M")
        if self.frequency not in FREQUENCIES:
            raise ValueError(f"frequency must be one of {FREQUENCIES}, got {self.frequency!r}")
        if self.priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}, got {self.priority!r}")

    def mark_complete(self) -> None:
        """Mark this task as completed."""
        self.completed = True

    def to_dict(self) -> dict:
        """Return a JSON-friendly dict (the due date becomes an ISO string)."""
        return {
            "description": self.description,
            "time": self.time,
            "frequency": self.frequency,
            "due_date": self.due_date.isoformat(),
            "completed": self.completed,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Task:
        """Rebuild a task from to_dict() output; invalid values raise ValueError."""
        return cls(
            description=data["description"],
            time=data["time"],
            frequency=data.get("frequency", "once"),
            due_date=date.fromisoformat(data["due_date"]),
            completed=bool(data.get("completed", False)),
            priority=data.get("priority", "medium"),
        )


@dataclass
class Pet:
    name: str
    breed: str
    tasks: list[Task] = field(default_factory=list)

    def add_task(self, task: Task) -> None:
        """Add a task to this pet's task list."""
        self.tasks.append(task)

    def remove_task(self, task: Task) -> None:
        """Remove a task from this pet's task list."""
        self.tasks.remove(task)

    def to_dict(self) -> dict:
        """Return a JSON-friendly dict of this pet and its tasks."""
        return {
            "name": self.name,
            "breed": self.breed,
            "tasks": [t.to_dict() for t in self.tasks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Pet:
        """Rebuild a pet and its tasks from to_dict() output."""
        return cls(
            name=data["name"],
            breed=data["breed"],
            tasks=[Task.from_dict(t) for t in data.get("tasks", [])],
        )


@dataclass
class Owner:
    name: str
    pets: list[Pet] = field(default_factory=list)

    def add_pet(self, pet: Pet) -> None:
        """Add a pet to this owner."""
        self.pets.append(pet)

    def get_all_tasks(self) -> list[Task]:
        """Return every task across all of this owner's pets."""
        return [task for pet in self.pets for task in pet.tasks]

    def to_dict(self) -> dict:
        """Return a JSON-friendly dict of this owner, their pets and tasks."""
        return {"name": self.name, "pets": [p.to_dict() for p in self.pets]}

    @classmethod
    def from_dict(cls, data: dict) -> Owner:
        """Rebuild an owner (with pets and tasks) from to_dict() output."""
        return cls(
            name=data["name"],
            pets=[Pet.from_dict(p) for p in data.get("pets", [])],
        )

    def save_to_json(self, path: str = "data.json") -> None:
        """Write this owner, their pets and tasks to a JSON file.

        The data goes to a temporary file first and is then moved into place, so a
        crash mid-write cannot leave a half-written data file behind.
        """
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2)
            os.replace(tmp_path, path)
        except BaseException:
            os.unlink(tmp_path)
            raise

    @classmethod
    def load_from_json(cls, path: str = "data.json") -> Owner:
        """Load an owner from a JSON file written by save_to_json().

        Raises FileNotFoundError if the file is missing, and ValueError if it is not
        valid JSON or holds invalid task data.
        """
        with open(path, encoding="utf-8") as f:
            try:
                return cls.from_dict(json.load(f))
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ValueError(f"{path} is not valid PawPal+ data: {exc!r}") from exc


class Scheduler:
    def __init__(self, owner: Owner) -> None:
        """Create a scheduler for the given owner."""
        self.owner = owner

    def get_todays_schedule(self) -> list[Task]:
        """Return today's incomplete tasks, ordered by time then priority."""
        today = date.today()
        tasks = [
            t
            for t in self.owner.get_all_tasks()
            if t.due_date == today and not t.completed
        ]
        return self.sort_by_time(tasks)

    def sort_by_time(self, tasks: list[Task]) -> list[Task]:
        """Return the tasks sorted by time, with higher priority first on ties."""
        rank = {"high": 0, "medium": 1, "low": 2}
        return sorted(tasks, key=lambda t: (t.time, rank.get(t.priority, 1)))

    def filter_tasks(
        self, pet_name: Optional[str] = None, completed: Optional[bool] = None
    ) -> list[Task]:
        """Return tasks filtered by pet name and/or completion status.

        pet_name is matched case-insensitively; either filter may be None to skip
        it. Tasks of every due date are included.
        """
        if pet_name is None:
            tasks = self.owner.get_all_tasks()
        else:
            wanted = pet_name.casefold()
            tasks = [
                t
                for p in self.owner.pets
                if p.name.casefold() == wanted
                for t in p.tasks
            ]
        if completed is not None:
            tasks = [t for t in tasks if t.completed == completed]
        return tasks

    def next_available_slot(
        self,
        on: Optional[date] = None,
        after: str = "00:00",
        step_minutes: int = 15,
    ) -> Optional[str]:
        """Return the first free "HH:MM" at or after `after` on a date, or None.

        A slot is free when no open (not completed) task starts at that exact time
        on that date. Candidate slots are `step_minutes` apart, starting at `after`
        (default 00:00). Returns None when no free slot is left that day.
        """
        if step_minutes <= 0:
            raise ValueError("step_minutes must be positive")
        on = on or date.today()
        taken = {
            t.time
            for t in self.owner.get_all_tasks()
            if t.due_date == on and not t.completed
        }
        start = datetime.strptime(after, "%H:%M")
        minutes = start.hour * 60 + start.minute
        while minutes < 24 * 60:
            slot = f"{minutes // 60:02d}:{minutes % 60:02d}"
            if slot not in taken:
                return slot
            minutes += step_minutes
        return None

    def detect_conflicts(self, suggest: bool = False) -> list[str]:
        """Return a warning for each date/time slot holding more than one open task.

        Completed tasks are ignored, since they no longer need scheduling. With
        suggest=True each warning ends with the next free slot after the clash.

        Lightweight strategy: group tasks by (due_date, time) in one pass and warn
        about any group larger than one. It covers tasks for the same pet or for
        different pets, never raises, and only matches exact start times (task
        durations are not modelled, so overlapping ranges are not detected).
        """
        slots: defaultdict[tuple[date, str], list[str]] = defaultdict(list)
        for pet in self.owner.pets:
            for t in pet.tasks:
                if t.completed:
                    continue
                slots[(t.due_date, t.time)].append(f"{pet.name}: {t.description}")
        warnings = []
        for (due, time), entries in slots.items():
            if len(entries) < 2:
                continue
            message = f"Conflict at {time} on {due}: {', '.join(entries)}"
            if suggest:
                free = self.next_available_slot(due, after=time)
                message += (
                    f". Next free slot: {free}."
                    if free
                    else ". No free slot left that day."
                )
            warnings.append(message)
        return warnings

    def mark_task_complete(self, pet: Pet, task: Task) -> Optional[Task]:
        """Complete a task and, if recurring, add and return the next occurrence.

        The next due date is max(due_date, today) plus the interval, so a late task
        never spawns an occurrence in the past and an early one keeps its schedule.
        Completing an already-completed task is a no-op, so it never spawns duplicates.
        """
        if task.completed:
            return None
        task.mark_complete()
        days = {"daily": 1, "weekly": 7}.get(task.frequency)
        if days is None:
            return None
        base = max(task.due_date, date.today())
        next_task = replace(
            task, due_date=base + timedelta(days=days), completed=False
        )
        pet.add_task(next_task)
        return next_task
