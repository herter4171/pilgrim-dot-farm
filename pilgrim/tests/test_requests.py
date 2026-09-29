"""Request-line tests (RADIO.md §7): FIFO top-N eviction + LLM moderation.

Fakes only — no real backends (AGENTS §1.2). The moderation LLM is a tiny
duck-typed fake that returns allowed/reject per the request text.
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest
from pilgrim.pipelines.moderation import Moderation
from pilgrim.store import Store


def _store():
    tmp = tempfile.mkdtemp()
    return Store(Path(tmp) / "station.db")


class _FakeModeratorLLM:
    """Returns a canned moderation verdict keyed on a substring of the prompt."""

    def __init__(self, rules=None):
        self.rules = rules or {}
        self.query = ""
        self.system = ""

    async def chat_json(self, model, system, user, max_tokens=800):
        self.query = user
        self.system = system
        for key, val in self.rules.items():
            if key and key in user:
                return val
        return {"allowed": True, "reason": "ok"}

    async def close(self):
        pass


def test_fifo_eviction_keeps_latest_ten(cfg):
    s = _store()
    for i in range(10):
        s.add_request(f"request {i}", cap=10)
    assert len(s.queued_requests()) == 10
    assert s.queued_requests()[0]["text"] == "request 0"

    # an 11th pushes the oldest out (the ass end falls out)
    s.add_request("request 10", cap=10)
    q = s.queued_requests()
    assert len(q) == 10
    assert q[0]["text"] == "request 1"
    assert q[-1]["text"] == "request 10"

    # the evicted one is recorded, not deleted
    evicted = [r for r in s.all_requests() if r["status"] == "evicted"]
    assert any(r["text"] == "request 0" for r in evicted)


def test_rejected_request_does_not_evict(cfg):
    s = _store()
    for i in range(10):
        s.add_request(f"r{i}", cap=10, status="queued")
    # rejected requests aren't part of the live queue, so they don't evict
    s.add_request("spam", cap=10, status="rejected", reason="no explicit content")
    q = s.queued_requests()
    assert len(q) == 10
    assert q[0]["text"] == "r0"
    assert all(r["status"] == "queued" for r in q)


def test_moderation_allows_clean(cfg):
    llm = _FakeModeratorLLM({"Play us a tune": {"allowed": True, "reason": "ok"}})
    m = Moderation(cfg, llm, prompts={"moderation": "mod template"})
    allowed, reason = asyncio.run(m.moderate("Play us a tune, Liam"))
    assert allowed
    assert reason == "ok"
    assert "mod template" in llm.system   # template is the system prompt only
    assert "mod template" not in llm.query  # request framing is purely the user msg


def test_moderation_rejects_explicit(cfg):
    llm = _FakeModeratorLLM({"underage": {"allowed": False, "reason": "explicit/illegal"}})
    m = Moderation(cfg, llm, prompts={"moderation": "mod template"})
    allowed, reason = asyncio.run(m.moderate("request about underage stuff"))
    assert not allowed
    assert "explicit" in reason


def test_moderation_gate_closed_on_model_error(cfg):
    class _Boom:
        async def chat_json(self, *a, **k):
            raise RuntimeError("model down")

    m = Moderation(cfg, _Boom(), prompts={"moderation": "mod template"})
    allowed, reason = asyncio.run(m.moderate("anything at all"))
    assert not allowed  # never slips through when the gate is down
    # fail-closed message must NOT leak internal error text to the user
    assert "moderation error" not in reason
    assert reason  # a friendly reason is shown


def test_moderation_missing_template_rejects(cfg):
    m = Moderation(cfg, _FakeModeratorLLM(), prompts={})
    allowed, _ = asyncio.run(m.moderate("anything"))
    assert not allowed


# --------------------------------------------------------------------------- #
# OVERHAUL 4.2 — hardened moderation: strict boolean, injection-safe framing,
# retry-once, configurable token cap.
# --------------------------------------------------------------------------- #

class _Recorder:
    """Records (system, user) per call; returns canned JSON from a script."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.max_tokens = []

    async def chat_json(self, model, system, user, max_tokens=800):
        self.calls.append((system, user))
        self.max_tokens.append(max_tokens)
        if not self.answers:
            raise RuntimeError("no more answers")
        nxt = self.answers.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def close(self):
        pass


@pytest.mark.parametrize("raw,expected", [
    ({"allowed": "false", "reason": "x"}, False),   # string -> rejected
    ({"allowed": "true", "reason": "x"}, False),    # string -> rejected
    ({"allowed": "True", "reason": "x"}, False),
    ({"allowed": 1, "reason": "x"}, False),         # truthy int -> rejected
    ({"allowed": True, "reason": "ok"}, True),      # real boolean -> allowed
])
def test_moderation_strict_boolean(cfg, raw, expected):
    llm = _Recorder([raw])
    m = Moderation(cfg, llm, prompts={"moderation": "be strict"})
    allowed, _ = asyncio.run(m.moderate("play the accordion"))
    assert allowed == expected
    # user message carries the JSON-escaped text inside <request>
    sys_msg, user_msg = llm.calls[0]
    assert '"play the accordion"' in user_msg
    assert "<request>" in user_msg
    assert "play the accordion" not in sys_msg  # request never reaches the system prompt


def test_moderation_missing_allowed_rejected(cfg):
    llm = _Recorder([{}])
    m = Moderation(cfg, llm, prompts={"moderation": "be strict"})
    allowed, reason = asyncio.run(m.moderate("anything"))
    assert allowed is False
    assert reason == "blocked by moderator"


def test_moderation_retries_once_then_allows(cfg):
    llm = _Recorder([TypeError("boom"), {"allowed": True, "reason": "ok"}])
    m = Moderation(cfg, llm, prompts={"moderation": "be strict"})
    allowed, _ = asyncio.run(m.moderate("play the accordion"))
    assert allowed is True
    assert len(llm.calls) == 2  # one retry happened
    assert llm.max_tokens == [cfg.requests.moderation_max_tokens] * 2


def test_moderation_fails_closed_after_retry(cfg):
    llm = _Recorder([RuntimeError("down"), RuntimeError("still down")])
    m = Moderation(cfg, llm, prompts={"moderation": "be strict"})
    allowed, reason = asyncio.run(m.moderate("anything"))
    assert allowed is False
    assert len(llm.calls) == 2
    assert "couldn't reach" in reason
