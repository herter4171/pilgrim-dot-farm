"""OVERHAUL 1.1 — JSON-lines logging setup."""
from __future__ import annotations

import asyncio
import logging
import sys

from pilgrim.config import RNG, SimClock
from pilgrim.logging_setup import ConsoleFormatter, TextFormatter, setup_logging
from pilgrim.pipelines.moderation import Moderation
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.tests.fakes import FakeLLM


def test_text_formatter_is_one_greppable_line():
    fmt = TextFormatter()
    r = logging.LogRecord("radio.test", logging.INFO, __file__, 1,
                          "program.commit", None, None, None)
    r.__dict__["item_id"] = 7
    r.__dict__["stage"] = "qc"
    line = fmt.format(r)
    assert "program.commit" in line
    assert "INFO" in line
    assert "radio.test" in line
    # extra fields render as sorted key=value pairs
    assert "item_id=7" in line
    assert "stage=qc" in line
    assert line.count("\n") == 0


def test_console_formatter_colors_the_level_only():
    colored = ConsoleFormatter(use_color=True).format(
        logging.LogRecord("radio.test", logging.WARNING, __file__, 1,
                           "spot.check", None, None, None))
    assert "\033[33m" in colored        # ANSI yellow for WARNING
    assert "\033[0m" in colored        # reset after the level word
    # the level word itself is still present, unescaped
    assert "WARNING" in colored


def test_console_formatter_strips_color_when_disabled():
    plain = ConsoleFormatter(use_color=False).format(
        logging.LogRecord("radio.test", logging.ERROR, __file__, 1,
                           "pipe.failed", None, None, None))
    assert "\033[" not in plain
    assert "ERROR" in plain


def test_console_formatter_is_plain_for_non_tty():
    colored = ConsoleFormatter(use_color=False).format(
        logging.LogRecord("radio.test", logging.INFO, __file__, 1,
                           "ready", None, None, None))
    assert "\033[" not in colored


def test_text_formatter_emits_exc_key_for_exc_info():
    fmt = TextFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        r = logging.LogRecord("radio.test", logging.ERROR, __file__, 1,
                              "pipe.failed", None, exc_info=sys.exc_info())
    line = fmt.format(r)
    assert "ERROR" in line
    assert "exc=" in line
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
        # file is plain text, not JSONL, with key=value extras.
        # (setup_logging also logs its own logging.ready line, so the file
        # may hold more than one record — but every line is one record.)
        assert "hello" in text
        assert "item_id=1" in text
        assert all("\n" not in ln for ln in text.splitlines())
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


def test_scheduler_commit_logs_program_commit(cfg, tmp_env, caplog):
    from conftest import seed_pool
    _, store, _ = tmp_env
    seed_pool(store, cfg, n_song=10, n_liner=10, n_com=10, n_dj=5)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), clock, RNG(1))
    with caplog.at_level(logging.DEBUG, logger="radio.scheduler"):
        sched.commit_lookahead()
    commits = [r for r in caplog.records if r.getMessage() == "program.commit"]
    assert commits, "expected at least one program.commit record"
    # per-item commits are DEBUG so the default INFO log isn't flooded
    assert all(r.levelno == logging.DEBUG for r in commits)
    assert getattr(commits[0], "seq", None) is not None
    assert getattr(commits[0], "item_id", None) is not None


def test_moderation_logs_decision(cfg, caplog):
    llm = FakeLLM(responses={"play something nice": {"allowed": True, "reason": "ok"}})
    mod = Moderation(cfg, llm, prompts={"moderation": "moderate it"})
    with caplog.at_level(logging.INFO, logger="radio.moderation"):
        allowed, _ = asyncio.run(mod.moderate("play something nice"))
    assert allowed
    assert any(r.getMessage().startswith("moderation.")
               for r in caplog.records)
