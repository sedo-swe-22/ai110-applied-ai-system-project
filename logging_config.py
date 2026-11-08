"""Structured JSON-lines logging for the PawPal+ agent.

Every record is one JSON object per line in `logs/agent.jsonl`, linked by a
`run_id` so one user request can be traced from start to finish:

    {"ts": "...", "level": "INFO", "event": "tool_call", "run_id": "a1b2c3d4", ...}

`export_trace()` turns one run into readable markdown under `docs/traces/`, which
is committed and linked from `ai_interactions.md` (the `logs/` folder itself is
git-ignored).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_PATH = PROJECT_DIR / "logs" / "agent.jsonl"
DEFAULT_TRACE_DIR = PROJECT_DIR / "docs" / "traces"
LOGGER_NAME = "pawpal.agent"


class JsonLineFormatter(logging.Formatter):
    """Format a record as a single JSON line, merging in its `payload` dict."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        data.update(getattr(record, "payload", {}) or {})
        return json.dumps(data, default=str, ensure_ascii=False)


def setup_agent_logger(log_path: Optional[Path | str] = None) -> logging.Logger:
    """Return the agent logger, writing JSON lines to `log_path`.

    Safe to call repeatedly (Streamlit reruns the script on every click): a file
    handler for the same path is only added once.
    """
    path = Path(log_path) if log_path else DEFAULT_LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # keep JSON lines out of the console
    target = str(path.resolve())
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and handler.baseFilename == target:
            return logger
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.setFormatter(JsonLineFormatter())
    logger.addHandler(handler)
    return logger


def null_logger() -> logging.Logger:
    """A logger that discards everything (used when no logger is given, e.g. tests)."""
    logger = logging.getLogger(f"{LOGGER_NAME}.null")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    logger.propagate = False
    return logger


def log_event(
    logger: logging.Logger, event: str, run_id: Optional[str] = None, level: int = logging.INFO, **fields: Any
) -> None:
    """Write one structured event, e.g. log_event(log, "tool_call", run_id, name="add_task")."""
    payload = {"run_id": run_id, **fields} if run_id else dict(fields)
    logger.log(level, event, extra={"payload": payload})


def read_run(run_id: str, log_path: Optional[Path | str] = None) -> list[dict]:
    """Return every log record for one run, in order."""
    path = Path(log_path) if log_path else DEFAULT_LOG_PATH
    records = []
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("run_id") == run_id:
                    records.append(record)
    return records


def export_trace(
    run_id: str,
    log_path: Optional[Path | str] = None,
    out_dir: Optional[Path | str] = None,
    title: Optional[str] = None,
) -> Path:
    """Write one run's records as a readable markdown trace and return the file path.

    The top half is the agent's decision chain in plain language; the raw JSON-lines
    records follow at the bottom, so nothing from the log is lost.
    """
    records = read_run(run_id, log_path)
    if not records:
        raise ValueError(f"no log records found for run {run_id!r}")
    out = Path(out_dir) if out_dir else DEFAULT_TRACE_DIR
    out.mkdir(parents=True, exist_ok=True)

    start = records[0]
    lines = [
        f"# Agent trace: {title or start.get('message') or run_id}",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| run_id | `{run_id}` |",
        f"| started (UTC) | {start['ts']} |",
        f"| model | {start.get('model', '?')} |",
        f"| today (as seen by the agent) | {start.get('today', '?')} |",
        f"| few-shot examples | {'on' if start.get('few_shot') else 'off'} |",
        "",
        f"**User request:** {start.get('message', '')}",
        "",
        "## Decision chain",
        "",
    ]
    step = 0
    for record in records:
        line = _describe(record)
        if line:
            step += 1
            lines.append(f"{step}. {line}")
    lines += [
        "",
        "## Raw log records",
        "",
        f"The exact lines from `logs/agent.jsonl` for this run ({len(records)} records).",
        "",
        "```json",
        *(json.dumps(r, ensure_ascii=False, default=str) for r in records),
        "```",
    ]
    path = out / f"trace_{run_id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _short(value: Any, limit: int = 300) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _describe(r: dict) -> Optional[str]:
    """One plain-language line per log event (None = skip)."""
    event = r.get("event")
    if event == "run_start":
        return None  # shown in the header
    if event == "input_blocked":
        return f"🛡️ **Input guardrail blocked the request** (reason: `{r.get('reason')}`). Gemini was not called."
    if event == "llm_call":
        tools = r.get("tool_calls") or []
        if tools:
            return f"🤖 **Model call {r.get('turn')}** ({r.get('ms')} ms): decided to call `{'`, `'.join(tools)}`."
        return f"🤖 **Model call {r.get('turn')}** ({r.get('ms')} ms): wrote the final reply: {_short(r.get('text', ''))}"
    if event in ("tool_call", "tool_blocked"):
        result = r.get("result") or {}
        status = "ok" if r.get("ok") else f"**error:** {result.get('error')}"
        what = result.get("summary") or _short({k: v for k, v in result.items() if k != "ok"}, 240)
        prefix = "🛡️ **Tool guardrail blocked**" if event == "tool_blocked" else "🔧 **Tool**"
        return f"{prefix} `{r.get('tool')}({_short(r.get('args', {}), 200)})` → {status}. {what}"
    if event == "llm_retry":
        return f"⏳ Gemini returned `{r.get('code')}`; waited {r.get('delay_s')}s and retried (attempt {r.get('attempt')})."
    if event == "llm_quota_exhausted":
        return f"⛔ Daily Gemini quota used up for {r.get('model')} (limit {r.get('limit')}/day)."
    if event == "self_check":
        verdict = {True: "passed", False: "FAILED", None: "n/a (nothing changed)"}.get(r.get("checks_passed"))
        details = "; ".join(d.strip() for d in r.get("details") or [])
        return (f"✅ **Self-check (code):** {verdict}. {details} "
                f"Model confidence {r.get('model_confidence')}, final confidence {r.get('confidence')}.")
    if event == "output_warning":
        return f"⚠️ **Output check:** {r.get('warning')}"
    if event == "saved":
        return "💾 Changes saved to the data file."
    if event == "run_failed":
        return f"❌ **Run failed:** {r.get('error')}"
    if event == "rollback":
        return "↩️ All changes from this run were rolled back."
    if event == "pending_confirmed":
        return f"🗑️ Owner **confirmed**: {r.get('action')} → {'done' if r.get('ok') else r.get('error')}."
    if event == "pending_cancelled":
        return f"🚫 Owner **cancelled**: {r.get('action')}. Nothing was deleted."
    if event == "run_end":
        pending = r.get("pending") or []
        extra = f" Waiting for confirmation: {'; '.join(pending)}." if pending else ""
        return f"🏁 Run ended with status **{r.get('status')}**.{extra}"
    return f"`{event}` {_short({k: v for k, v in r.items() if k not in {'ts', 'level', 'event', 'run_id'}})}"
