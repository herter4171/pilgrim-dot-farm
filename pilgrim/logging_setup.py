"""Human-readable logging to stdout + a rotating file (AGENTS §5).

The default is a single greppable line per record, not JSONL:

    2026-09-30T23:33:21.616Z INFO  radio.scheduler   program.commit seq=533 item_id=60 ...

The message is a short dotted event name (OVERHAUL 1.1, 1.2); structured
fields passed via ``extra={...}`` are rendered as ``key=value`` pairs, sorted
for stable output. Exceptions are appended as ``exc=<traceback>``.

Color is only added on the console, and only when it is safe to do so (a TTY
or no ``NO_COLOR`` override). The rotating file is always plain text so it
stays clean under ``grep`` and log rotation.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from datetime import UTC, datetime

from pilgrim.config import ROOT, Config

# Standard LogRecord attributes — anything in extra with one of these names
# raises KeyError on emission, so we exclude them from the log line.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

# ANSI colors for the level word (console only).
_LEVEL_COLORS = {
    "DEBUG": "\033[37m",       # gray
    "INFO": "\033[32m",        # green
    "WARNING": "\033[33m",     # yellow
    "ERROR": "\033[31m",       # red
    "CRITICAL": "\033[1;31m",  # bold red
}
_RESET = "\033[0m"


def err_text(e: BaseException) -> str:
    """Exception text for logs. httpx timeouts and friends stringify to ""
    (the blank `error` fields at startup), so always lead with the type."""
    msg = str(e)
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


class TextFormatter(logging.Formatter):
    """One greppable line per record, no ANSI.

    Format::

        <ts> <LEVEL> <logger> <message> [key=value ...] [exc=<traceback>]

    Extra fields are sorted so identical records produce identical lines
    (stable diffs, easy `grep`/`awk`). The timestamp is UTC with a ``Z``
    suffix for a consistent, sortable prefix.
    """

    def _timestamp(self, record: logging.LogRecord) -> str:
        ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")
        return ts[:-6] + "Z" if ts.endswith("+00:00") else ts

    def format(self, record: logging.LogRecord) -> str:
        prefix = (
            f"{self._timestamp(record)} {record.levelname:<7} {record.name:<22}"
        )
        line = f"{prefix} {record.getMessage()}"
        if record.__dict__:
            parts = [
                f"{k}={record.__dict__[k]}"
                for k in sorted(k for k in record.__dict__ if k not in _RESERVED)
            ]
            if parts:
                line += " " + " ".join(parts)
        if record.exc_info:
            line += f" exc={self.formatException(record.exc_info)}"
        return line


class ConsoleFormatter(TextFormatter):
    """TextFormatter with ANSI color on the level word, for the console.

    The level is the first token after the timestamp, so we colorize only that
    occurrence; the message and ``key=value`` pairs stay plain. When
    ``use_color`` is false (piped output, or ``NO_COLOR`` set) the line is
    identical to :class:`TextFormatter`.
    """

    def __init__(self, use_color: bool = False) -> None:
        super().__init__()
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        if not self.use_color:
            return line
        color = _LEVEL_COLORS.get(record.levelname)
        if not color:
            return line
        idx = line.find(record.levelname)
        end = idx + len(record.levelname)
        return line[:idx] + color + record.levelname + _RESET + line[end:]


def _should_colorize(stream: object) -> bool:
    """Honor the standard NO_COLOR override and only color a real terminal."""
    if os.environ.get("NO_COLOR"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def setup_logging(cfg: Config) -> None:
    """Install a colored console handler + a plain rotating file handler.
    Call at app/seed start."""
    root = logging.getLogger()
    root.setLevel(cfg.logging.level)
    for h in list(root.handlers):
        root.removeHandler(h)

    log_dir = ROOT / cfg.logging.dir
    log_dir.mkdir(parents=True, exist_ok=True)

    # File: plain text, no ANSI, safe under grep and rotation.
    fh = logging.handlers.RotatingFileHandler(
        log_dir / cfg.logging.file, maxBytes=cfg.logging.max_bytes,
        backupCount=cfg.logging.backups)
    fh.setFormatter(TextFormatter())
    root.addHandler(fh)

    # Console: colored when writing to a terminal.
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(ConsoleFormatter(use_color=_should_colorize(sys.stdout)))
    root.addHandler(sh)

    logging.getLogger("radio.server").info(
        "logging.ready", extra={"file": str(log_dir / cfg.logging.file)})
