"""OVERHAUL 1.1 — JSON-lines logging setup."""
from __future__ import annotations

import logging
import sys

from pilgrim.logging_setup import JsonFormatter, setup_logging


def test_json_formatter_includes_event_and_extra():
    fmt = JsonFormatter()
    r = logging.LogRecord("radio.test", logging.INFO, __file__, 1,
                          "program.commit", None, None, None)
    r.__dict__["item_id"] = 7
    r.__dict__["stage"] = "qc"
    line = fmt.format(r)
    assert '"event": "program.commit"' in line
    assert '"item_id": 7' in line
    assert '"stage": "qc"' in line


def test_json_formatter_emits_exc_key_for_exc_info():
    fmt = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        r = logging.LogRecord("radio.test", logging.ERROR, __file__, 1,
                              "pipe.failed", None, exc_info=sys.exc_info())
    line = fmt.format(r)
    assert '"level": "ERROR"' in line
    assert '"exc"' in line
    assert "ValueError" in line


def test_setup_logging_creates_file_in_tmp(tmp_path, base_config):
    cfg = base_config.model_copy(deep=True)
    cfg.logging.dir = str(tmp_path)
    cfg.logging.file = "test.log"
    setup_logging(cfg)
    try:
        assert (tmp_path / "test.log").exists()
        log = logging.getLogger("radio.test")
        log.info("hello", extra={"item_id": 1})
        text = (tmp_path / "test.log").read_text()
        assert '"event": "hello"' in text
    finally:
        # restore handlers so later tests are unaffected
        root = logging.getLogger()
        for h in list(root.handlers):
            root.removeHandler(h)


def test_importing_server_or_seed_does_not_configure_logging():
    # Caught a real regression: import-time basicConfig used to install handlers.
    # Importing must not touch root logging (or tests would write log files).
    import importlib
    root = logging.getLogger()
    importlib.import_module("pilgrim.server")
    importlib.import_module("pilgrim.seed")
    assert not any(isinstance(h, logging.handlers.RotatingFileHandler)
                   for h in root.handlers)
