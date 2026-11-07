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
    """Write one run's records as a markdown trace and return the file path."""
    records = read_run(run_id, log_path)
    if not records:
        raise ValueError(f"no log records found for run {run_id!r}")
    out = Path(out_dir) if out_dir else DEFAULT_TRACE_DIR
    out.mkdir(parents=True, exist_ok=True)
    lines = [f"# Agent trace: {title or run_id}", "", f"- **run_id:** `{run_id}`", f"- **started:** {records[0]['ts']}", ""]
    lines += ["| # | Event | Details |", "|---|---|---|"]
    skip = {"ts", "level", "event", "run_id"}
    for i, record in enumerate(records, 1):
        details = {k: v for k, v in record.items() if k not in skip}
        text = json.dumps(details, ensure_ascii=False, default=str).replace("|", "\\|")
        lines.append(f"| {i} | `{record['event']}` | `{text}` |")
    path = out / f"trace_{run_id}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
