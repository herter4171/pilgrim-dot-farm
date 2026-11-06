"""In-memory stand-in for the redis-py client slice `pilgrim.hotstate` uses
(AGENTS §1.2: tests never touch a real cache). TTLs expire against an
injected clock; `fail` injects a connection failure on the next call(s)."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeConnectionError(Exception):
    pass


class FakeRedis:
    def __init__(self, now: Callable[[], float]) -> None:
        self._now = now
        self._data: dict[str, tuple[bytes, float | None]] = {}
        self.fail_next = 0          # raise on this many upcoming calls
        self.calls: list[str] = []

    # -- plumbing -------------------------------------------------------
    def _check(self, op: str) -> None:
        self.calls.append(op)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise FakeConnectionError(f"injected failure on {op}")

    def _sweep(self) -> None:
        now = self._now()
        for k in [k for k, (_, exp) in self._data.items() if exp is not None and exp <= now]:
            del self._data[k]

    def _put(self, name: str, value: Any, ex: int | None) -> None:
        raw = value if isinstance(value, bytes) else str(value).encode()
        self._data[name] = (raw, self._now() + ex if ex is not None else None)

    def ttl(self, name: str) -> int:
        self._sweep()
        if name not in self._data:
            return -2
        exp = self._data[name][1]
        return -1 if exp is None else int(round(exp - self._now()))

    # -- redis-py API slice --------------------------------------------
    def ping(self) -> bool:
        self._check("ping")
        return True

    def set(self, name: str, value: Any, ex: int | None = None) -> bool:
        self._check("set")
        self._put(name, value, ex)
        return True

    def get(self, name: str) -> bytes | None:
        self._check("get")
        self._sweep()
        v = self._data.get(name)
        return v[0] if v else None

    def getdel(self, name: str) -> bytes | None:
        self._check("getdel")
        self._sweep()
        v = self._data.pop(name, None)
        return v[0] if v else None

    def delete(self, *names: str) -> int:
        self._check("delete")
        if len(names) > 1:   # Valkey Serverless (cluster mode) refuses cross-slot multi-key ops
            raise FakeConnectionError("CROSSSLOT Keys in request don't hash to the same slot")
        return sum(1 for n in names if self._data.pop(n, None) is not None)

    def scan_iter(self, match: str | None = None, count: int | None = None):
        self._check("scan")
        self._sweep()
        prefix = (match or "*").rstrip("*")
        for k in list(self._data):
            if k.startswith(prefix):
                yield k.encode()

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        if transaction:      # MULTI across slots is a CROSSSLOT error on Serverless
            raise FakeConnectionError("CROSSSLOT transactional pipeline")
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, r: FakeRedis) -> None:
        self._r = r
        self._ops: list[tuple[str, Any, int | None]] = []

    def set(self, name: str, value: Any, ex: int | None = None) -> _FakePipeline:
        self._ops.append((name, value, ex))
        return self

    def execute(self) -> list[bool]:
        self._r._check("pipeline")
        for name, value, ex in self._ops:
            self._r._put(name, value, ex)
        out = [True] * len(self._ops)
        self._ops = []
        return out
