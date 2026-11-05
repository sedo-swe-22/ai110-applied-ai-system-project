from pawpal_system import Owner, Pet, Scheduler, Task

owner = Owner("Sam")
rex = Pet("Rex", "Labrador")
mia = Pet("Mia", "Siamese")
owner.add_pet(rex)
owner.add_pet(mia)

# Tasks are deliberately added out of chronological order.
walk = Task("Walk", "08:00", frequency="daily")
rex.add_task(Task("Vet visit", "14:30"))
rex.add_task(walk)
rex.add_task(Task("Evening play", "18:00", priority="low"))
mia.add_task(Task("Feed", "08:00", priority="high"))  # conflicts with Rex's walk
mia.add_task(Task("Brush", "07:15", frequency="weekly"))
mia.add_task(Task("Medication", "9:00"))  # normalized to 09:00
mia.add_task(Task("Play", "09:00"))  # conflicts with Mia's own Medication

scheduler = Scheduler(owner)


def show(title: str, tasks: list[Task]) -> None:
    print(f"\n{title}")
    for t in tasks:
        status = "done" if t.completed else "todo"
        print(f"  {t.time}  {t.description:<13} [{status}]")


show("As added (unsorted):", owner.get_all_tasks())
show("Sorted by time:", scheduler.sort_by_time(owner.get_all_tasks()))
show("Today's schedule:", scheduler.get_todays_schedule())

print("\nConflicts:")
for warning in scheduler.detect_conflicts():
    print(f"  {warning}")

print("\nConflicts with suggested free slots:")
for warning in scheduler.detect_conflicts(suggest=True):
    print(f"  {warning}")
print(f"\nNext free slot from 08:00: {scheduler.next_available_slot(after='08:00')}")

nxt = scheduler.mark_task_complete(rex, walk)
print(f"\nCompleted 'Walk'; next due {nxt.due_date}")

show("Filtered: pet 'rex' (case-insensitive):", scheduler.filter_tasks(pet_name="rex"))
show("Filtered: completed:", scheduler.filter_tasks(completed=True))
show("Filtered: Rex, pending:", scheduler.filter_tasks("Rex", completed=False))
