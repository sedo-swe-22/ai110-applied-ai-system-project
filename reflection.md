# PawPal+ Project Reflection

## 1. System Design

**a. Initial design**
- owner
    - name, pet
    + add a pet
- pet
    - name, breed, 
    + add/edit task(s)
- task
    - time, priority, preference, frequencies
- scheduler
    + generate a daily schedule/plan
    + display a daily schedule/plan
    + add owner


+ Add Owner + Pet info
+ Add/Edit a task with duration and proirity
+ Generate a daily schedule/plan based on constraints and priorities cleanly

- Briefly describe your initial UML design.
- What classes did you include, and what responsibilities did you assign to each?

**Initial UML design**

The design has four classes in a simple chain. An `Owner` has many `Pet`s, and each `Pet` has many `Task`s. A `Scheduler` reads an owner's pets and tasks and produces a daily plan. The data classes (`Owner`, `Pet`, `Task`) only store information. All the scheduling logic sits in one place, `Scheduler`.

**Classes and responsibilities**

- **Owner** holds the owner's name, their `available_minutes` for the day, and their list of pets. The available time is the main constraint the scheduler works within. Its one method, `add_pet`, covers the requirement to enter owner and pet info.
- **Pet** holds the pet's name, breed, and its list of tasks. It manages those tasks through `add_task`, `edit_task` and `remove_task`, so users can add and edit tasks.
- **Task** is a single care activity. It stores the title, duration, priority, preferred time, frequency, and which pet it belongs to. It also stores the scheduler's results, `scheduled_time` and `reason`, so a plan can explain itself.
- **Scheduler** has no state of its own. `generate_schedule(owner)` picks and orders tasks by priority and the owner's available time, and fills in each task's `scheduled_time` and `reason`. `display_schedule(tasks)` shows the plan clearly.

**Design choice:** I kept the scheduling logic out of the data classes so it can change without touching them. I kept the number of classes at four to stay simple. The cost is that `Task` carries both input and output fields, and tasks that didn't fit have nowhere to record why.

**b. Design changes**

- Did your design change during implementation?
- If yes, describe at least one change and why you made it.

The design changed after I reviewed my skeleton against the project requirements. I simplified it to match what the brief asks for:

- **Added to match the requirements:** `Task.completed` and `Task.mark_complete()`, `Task.due_date` for recurring tasks, `Owner.get_all_tasks()`, and Scheduler methods for sorting, filtering, conflict detection and completing recurring tasks. The Scheduler now holds an `Owner` and reads every pet's tasks through `get_all_tasks()`.
- **Removed:** `Owner.available_minutes`, `Task.preferred_time`, `Task.scheduled_time`, `Task.reason`, `Task.duration_minutes` and `Scheduler.generate_schedule` and `display_schedule`. A time-budget scheduler with explanations was more complex than the project needs, which schedules by exact `"HH:MM"` time.
- **Removed `Task.pet`:** the two-way link between `Task` and `Pet` was hard to keep in sync. The Scheduler can find a task's pet while looping over `owner.pets`.
- **Kept to four classes:** every feature fits in `Task`, `Pet`, `Owner` or `Scheduler`, so extra classes such as a plan or priority class were unnecessary. `priority` stays a plain attribute on `Task`.

---

## 2. Scheduling Logic and Tradeoffs

**a. Constraints and priorities**

- What constraints does your scheduler consider (for example: time, priority, preferences)?
- How did you decide which constraints mattered most?

The scheduler considers three things: the due date and `"HH:MM"` time of each task, its priority, and whether it is completed. Time matters most because a daily plan is an ordered list and conflicts depend on it, so tasks sort by time first. Priority is only a tie-breaker, so the more important of two same-time tasks is listed first. Completion decides what appears in today's schedule and when a recurring task spawns its next occurrence. I dropped the original idea of an owner time budget and task preferences because the project schedules by exact time, and those would have added complexity without changing the core behavior.

**b. Tradeoffs**

- Describe one tradeoff your scheduler makes.
- Why is that tradeoff reasonable for this scenario?

**Tradeoff: exact-time conflicts only.** `detect_conflicts()` flags two tasks only when they share the same date and the same `"HH:MM"` start. Tasks have no duration, so a 30-minute walk at 08:00 and a feeding at 08:15 are not reported even though they overlap. I chose this because it is one pass over the tasks with a dictionary, it never raises, and the warning is easy to read. Modelling durations would need a `duration` field and an interval-overlap check (sort by start, compare each end to the next start), which is more than this app needs. A pet owner usually sets tasks at distinct times, and a missed overlap is a warning that wasn't shown, not a crash.

**Simplification review.** I asked how `detect_conflicts()` could be simpler. The suggestion was `defaultdict(list)` in place of `dict.setdefault(...)`, which I kept because it is shorter and still obvious to read. A fully compressed version (`itertools.groupby` over a sorted list, or a `Counter` plus comprehension) was more "Pythonic" but harder to follow, so I kept the explicit loop. The warning also now names the pet (`Rex: Walk, Mia: Feed`) so the owner knows who each task belongs to.

---

## 3. AI Collaboration

**a. How you used AI**

I used Claude Code across the whole workflow rather than for one step:

- **Design:** turning my UML into class skeletons, then reviewing those skeletons against the project requirements.
- **Implementation:** filling in the `Task`, `Pet`, `Owner` and `Scheduler` logic and a CLI demo (`main.py`).
- **Tests and docs:** drafting two simple unit tests, adding one-line docstrings to every method, and writing the README implementation summary.

The most helpful prompts were small and specific, such as "draft two simple tests: task completion and task addition", or "add 1-line docstrings to the methods". Each one gave a short result I could read in full. Asking the AI to review my skeleton against the requirements was also useful, because it surfaced where my first design had drifted from what the brief asked for.

**b. Judgment and verification**

- **What the AI suggested:** my first UML was a time-budget scheduler. It had `Owner.available_minutes`, `Task.duration_minutes`, `scheduled_time` and `reason` fields, and `generate_schedule` / `display_schedule` methods that would pick tasks by priority and explain the plan. The requirements review pointed out that this was more than the project needs, and that the project schedules by exact `"HH:MM"` time.
- **What I accepted:** removing those fields and methods, and adding `Task.completed`, `Task.due_date`, `Owner.get_all_tasks()` and the Scheduler's sorting, filtering, conflict-detection and recurrence methods.
- **What I rejected:** adding extra classes (a plan class, a priority class). Every feature fits in the four existing classes, so `priority` stays a plain attribute on `Task`. I also dropped the two-way `Task.pet` link because it was hard to keep in sync with `Pet.tasks`.
- **How I verified it:**
  - I ran the CLI demo in `main.py`, which exercises sorting, same-pet and cross-pet conflicts, filtering, and a daily task being completed and rescheduled.
  - I ran the test suite with `python -m pytest` (63 tests, all passing). It started as two simple tests and grew to cover sorting, filtering, recurrence, conflicts, next-free-slot and persistence.
  - After each change I re-ran the tests to confirm behavior did not regress.
  - I checked the generated docstrings and README summary against the code, since they should describe what the methods actually do.

**c. AI strategy**

- **Most effective features:**
  - Plan mode for the larger steps. It forced a short plan and one decision (the recurrence date rule) before any code changed.
  - Reading the existing code before suggesting anything. Many requested features already existed, and the AI said so instead of rewriting them.
  - Running `pytest` and `main.py` after each change, so every claim came with output.
  - Small, specific requests (a docstring pass, a list of edge cases, one table in the README).
- **A suggestion I modified or rejected:**
  - The brief said a daily task's next date should be "today + 1 day". Applied literally, completing a task early pulls the next one earlier, and completing it late is fine only by accident. I chose `max(due_date, today)` + interval instead and added tests for late and early completion.
  - For `detect_conflicts` I kept an explicit loop with `defaultdict` and rejected a `groupby` or `Counter` one-liner, because it was shorter but harder to read.
  - I also did not add duration-overlap conflict detection. It is out of scope for the project, and the exact-time limit is documented in section 2b.
- **Separate chat sessions for planning and implementing:** I used one session to plan (requirements, UML, a test plan, the open decisions) and a different session to implement. Planning in its own session kept the conversation focused on what to build, without code diffs and test output crowding it. Implementing in a fresh session meant I started from a short written plan instead of a long back-and-forth, so the AI stayed on the task and I could check each change against the plan. It also made mistakes easier to spot: when the implementation drifted from the plan, I could see exactly where.
- **Being the lead architect:** the AI is fast and often right, but it does not know what the project is for. My job was to set the boundaries (four classes, exact-time scheduling, no extra layers), decide the open questions (reject invalid input, ignore completed tasks in conflicts), and verify the results with tests rather than trust the summary.

---

## 4. Testing and Verification

**a. What you tested**

- What behaviors did you test?
- Why were these tests important?

I wrote 63 tests in `tests/test_pawpal.py`, covering the four target features plus the next-free-slot and persistence extras:
- **Sorting:** times like `"9:00"` are normalized so they sort correctly, invalid times are rejected, and same-time tasks order by priority.
- **Filtering:** by pet name (case-insensitive), by completion status, and by both.
- **Recurring tasks:** a daily task recurs the next day, a weekly task in seven days, a late task recurs from today instead of the past, an early one keeps its future schedule, a one-off task does not recur, and completing twice does not create a duplicate.
- **Conflicts:** same time for different pets, same time for one pet, and the same time on different dates (no conflict).

These matter because each one covers a rule where a bug would silently give the owner a wrong plan, such as a missed task or a duplicated one.

**b. Confidence**

- How confident are you that your scheduler works correctly?
- What edge cases would you test next if you had more time?

I am fairly confident in the scheduling logic, since all 63 tests pass and the `main.py` demo behaves as expected. I am less sure of the Streamlit layer, which I have not tested automatically. With more time I would test overlapping durations, and recurring tasks across more calendar edge cases. Invalid times, priorities and frequencies are rejected with `ValueError`, and completed tasks no longer trigger conflict warnings.

---

## 5. Reflection

**a. What went well**

- What part of this project are you most satisfied with?

Keeping all scheduling decisions in `Scheduler` while `Task`, `Pet` and `Owner` only store data. It made each feature small to add and easy to test, and the recurring-task and conflict rules are each a few lines.

**b. What you would improve**

- If you had another iteration, what would you improve or redesign?

I would add task durations so conflict detection can find overlapping ranges, show overdue tasks in the schedule. I would also add an automated test for the Streamlit flow.

**c. Key takeaway**

- What is one important thing you learned about designing systems or working with AI on this project?

AI is fastest when the request is small and checkable, and my review is what keeps the design right. Reviewing against the requirements cut my over-built first design, and reading the generated code found real edge cases (duplicate recurrence, unpadded times) that the first version missed.
