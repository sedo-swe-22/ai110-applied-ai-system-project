"""Guardrails for the PawPal+ agent.

Three layers, all plain Python (no LLM calls, so they are fast, free and testable):

1. Input (`check_input`): runs *before* Gemini is called. Blocks empty or very
   long messages, prompt-injection attempts, obvious off-topic requests, pet
   emergencies, and requests for medical advice. Scheduling medication is still
   allowed: "Give Max his meds at 8am" passes, "How much ibuprofen can Max have?"
   does not.
2. Tool calls (`check_tool_call`): runs before every tool call. Only registry
   tools are allowed, dates must be within a year of today, and each run is
   capped at MAX_TOOL_CALLS calls.
3. Output (`check_output`): runs on the final reply. Flags claims that the
   successful tool results do not support (e.g. "I added..." with no successful
   add_task, or "deleted" when the removal is only waiting for confirmation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

from agent_tools import TOOL_REGISTRY, ToolError, parse_date

MAX_INPUT_CHARS = 1000
MAX_TOOL_CALLS = 10
MAX_DAYS_FROM_TODAY = 365

EMERGENCY_REPLY = (
    "This sounds like it could be an emergency. Please contact your vet or the nearest "
    "emergency animal hospital right away. In the US you can also call the ASPCA Animal "
    "Poison Control Center at (888) 426-4435. I can only help with scheduling care tasks."
)
MEDICAL_REPLY = (
    "I can't give medical advice, such as doses, symptoms or diagnoses. Please ask your vet. "
    "I can help you schedule things, for example: \"Give Max his meds at 8am daily\" or "
    "\"Add a vet visit for Max on Friday at 3pm\"."
)
OFF_TOPIC_REPLY = (
    "I'm PawPal+, a pet-care scheduling assistant. I can add, move, complete or remove care "
    "tasks for your pets, show today's schedule and fix time clashes."
)
INJECTION_REPLY = "I can't change my instructions. I can help you manage your pets' care schedule."
EMPTY_REPLY = "Please type a request, for example: \"Add a 9am walk for Max tomorrow\"."
TOO_LONG_REPLY = f"That message is too long (over {MAX_INPUT_CHARS} characters). Please shorten it."


@dataclass(frozen=True)
class InputVerdict:
    allowed: bool
    reason: Optional[str] = None  # e.g. "medical_advice"; None when allowed
    reply: Optional[str] = None  # fixed reply shown to the user when blocked


@dataclass(frozen=True)
class ToolVerdict:
    allowed: bool
    error: Optional[str] = None


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


EMERGENCY = _rx(
    r"\b(poison(ed|ing)?|toxic|overdos\w*|not breathing|can'?t breathe|stopped breathing|"
    r"choking|seizure\w*|convuls\w*|collapsed?|unconscious|unresponsive|bleeding|"
    r"hit by a car|heat ?stroke|emergency)\b"
    r"|\bate\b.{0,30}\b(chocolate|grapes?|raisins?|xylitol|onions?|antifreeze|rat poison|pills?)\b"
)
MED_WORD = (
    r"(med|meds|medicine|medication|pill|pills|tablet|tablets|drug|drugs|ibuprofen|advil|"
    r"tylenol|acetaminophen|aspirin|benadryl|antibiotic\w*|painkiller\w*|dewormer|insulin)"
)
DOSAGE = _rx(rf"\b(dose|doses|dosage|how much|how many|mg|milligrams?|ml)\b.*\b{MED_WORD}\b"
             rf"|\b{MED_WORD}\b.*\b(dose|doses|dosage|how much|how many|mg|milligrams?)\b")
SAFETY_WORD = _rx(r"\b(safe|unsafe|toxic|poisonous|dangerous|harmful)\b")
GIVE_MED = _rx(rf"\b(can|should|could) (i|we) give\b.*\b{MED_WORD}\b|\bwhat {MED_WORD}\b")
DIAGNOSIS = _rx(r"\b(diagnos\w*|what'?s wrong with|what is wrong with|is (he|she|it|my \w+) sick)\b")
SYMPTOM = _rx(r"\b(symptoms?|vomit\w*|throw\w* up|threw up|diarrh\w*|limp\w*|letharg\w*|cough\w*|"
              r"sneez\w*|itch\w*|scratch\w*|rash|lump|fever|not eating|won'?t eat|sick|pain|"
              r"bloat\w*|bleed\w*|swollen|infect\w*)\b")
QUESTION_CUE = _rx(r"\?|\b(why|what should|what do i|what can i|should i|is it normal|how do i treat|"
                   r"how to treat|cure|remedy)\b")
# "Max is limping, can you add a vet visit at 3pm?" is a scheduling request, not advice.
SCHEDULING_INTENT = _rx(r"\b(add|schedule|book|remind\w*|set up|move|reschedule|plan|put)\b")
INJECTION = _rx(r"\b(ignore|disregard|forget)\b.{0,40}\b(instructions?|rules|prompt|above)\b"
                r"|\b(system prompt|developer message|jailbreak|you are now)\b|\bact as (a|an|if)\b")
OFF_TOPIC = _rx(r"\b(write|generate|create)\b.{0,30}\b(code|program|script|essay|poem|story|song|email)\b"
                r"|\b(homework|weather|stock|stocks|bitcoin|crypto|lottery|recipe|translate|capital of|"
                r"president|election|football|movie|joke)\b")
CARE_WORD = _rx(r"\b(pet|pets|dog|dogs|cat|cats|puppy|kitten|walk|walks|feed|feeding|food|meal|"
                r"groom\w*|brush|bath|vet|meds?|medicine|medication|task|tasks|schedule|remind\w*|"
                r"appointment|play|litter|train\w*)\b")


def check_input(text: str, pet_names: Iterable[str] = ()) -> InputVerdict:
    """Decide whether a user message may go to the agent. Never raises."""
    message = (text or "").strip()
    if not message:
        return InputVerdict(False, "empty", EMPTY_REPLY)
    if len(message) > MAX_INPUT_CHARS:
        return InputVerdict(False, "too_long", TOO_LONG_REPLY)
    if INJECTION.search(message):
        return InputVerdict(False, "prompt_injection", INJECTION_REPLY)
    if EMERGENCY.search(message):
        return InputVerdict(False, "emergency", EMERGENCY_REPLY)
    is_question = bool(QUESTION_CUE.search(message))
    wants_scheduling = bool(SCHEDULING_INTENT.search(message))
    if (
        DOSAGE.search(message)
        or GIVE_MED.search(message)
        or DIAGNOSIS.search(message)
        or (SAFETY_WORD.search(message) and is_question)
        or (SYMPTOM.search(message) and is_question and not wants_scheduling)
    ):
        return InputVerdict(False, "medical_advice", MEDICAL_REPLY)
    mentions_pet = any(
        re.search(rf"\b{re.escape(name)}\b", message, re.IGNORECASE) for name in pet_names if name
    )
    if OFF_TOPIC.search(message) and not (mentions_pet or CARE_WORD.search(message)):
        return InputVerdict(False, "off_topic", OFF_TOPIC_REPLY)
    return InputVerdict(True)


DATE_ARGS = ("due_date", "date")


def check_tool_call(name: str, args: Optional[dict], today: date) -> ToolVerdict:
    """Check one tool call before it runs. Bad *formats* are left to the tool itself."""
    if name not in TOOL_REGISTRY:
        return ToolVerdict(False, f"tool {name!r} is not allowed")
    for key in DATE_ARGS:
        value = (args or {}).get(key)
        if not value:
            continue
        try:
            when = parse_date(value, today)
        except ToolError:
            continue  # the tool will report the format problem
        if abs((when - today).days) > MAX_DAYS_FROM_TODAY:
            return ToolVerdict(
                False,
                f"{key} {when.isoformat()} is more than {MAX_DAYS_FROM_TODAY} days from today "
                f"({today.isoformat()}); ask the user to confirm the date",
            )
    return ToolVerdict(True)


# Affirmative claims, e.g. "I added", "I've scheduled", "has been added", "Added Max's walk".
_CLAIMS = {
    "add_task": r"(\bI(?:'ve| have)?\s+(?:added|scheduled|created)\b|\b(?:has|have) been (?:added|scheduled|created)\b|^\s*[-*]?\s*(?:added|scheduled)\b)",
    "complete_task": r"(\bI(?:'ve| have)?\s+(?:completed|marked)\b|\b(?:has|have) been (?:completed|marked (?:as )?done)\b|^\s*[-*]?\s*(?:completed|marked)\b)",
    "reschedule_task": r"(\bI(?:'ve| have)?\s+(?:moved|rescheduled)\b|\b(?:has|have) been (?:moved|rescheduled)\b|^\s*[-*]?\s*(?:moved|rescheduled)\b)",
}
_DELETE_CLAIM = _rx(r"(\bI(?:'ve| have)?\s+(?:removed|deleted)\b|\b(?:has|have) been (?:removed|deleted)\b|^\s*[-*]?\s*(?:removed|deleted)\b)")


def check_output(reply: str, successful_tools: Iterable[str], pending_count: int = 0) -> list[str]:
    """Return warnings for claims in the reply that tool results don't support."""
    done = set(successful_tools)
    warnings = []
    for tool, pattern in _CLAIMS.items():
        if re.search(pattern, reply or "", re.IGNORECASE | re.MULTILINE) and tool not in done:
            warnings.append(f"The reply claims a '{tool}' change, but no {tool} call succeeded.")
    if _DELETE_CLAIM.search(reply or ""):
        if pending_count:
            warnings.append("The reply says a task was deleted, but it is only waiting for your confirmation.")
        else:
            warnings.append("The reply says a task was deleted, but nothing was deleted.")
    return warnings
