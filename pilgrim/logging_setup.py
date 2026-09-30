"""JSON-lines logging (AGENTS §5). One JSON object per line on stdout and in a
rotating file under config.logging.dir. Data goes in `extra={...}`; the message
itself is a short dotted event name (OVERHAUL 1.1, 1.2).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime

from pilgrim.config import ROOT, Config

# Standard LogRecord attributes — anything in extra with one of these names
# raises KeyError on emission, so we exclude them from the JSON object.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def err_text(e: BaseException) -> str:
    """Exception text for logs. httpx timeouts and friends stringify to ""
    (the blank `error` fields at startup), so always lead with the type."""
    msg = str(e)
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED:
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def setup_logging(cfg: Config) -> None:
    """Install JSON-lines handlers on the root logger. Call at app/seed start."""
    root = logging.getLogger()
    root.setLevel(cfg.logging.level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = JsonFormatter()
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    log_dir = ROOT / cfg.logging.dir
    log_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_dir / cfg.logging.file, maxBytes=cfg.logging.max_bytes,
        backupCount=cfg.logging.backups)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    logging.getLogger("radio.server").info(
        "logging.ready", extra={"file": str(log_dir / cfg.logging.file)})
