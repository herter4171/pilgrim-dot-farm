"""In-memory stand-in for media_publish.S3ObjectStore (AGENTS §1.2).

Records every PUT with its headers; `fail_next` injects failures."""
from __future__ import annotations

from pathlib import Path


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.headers: dict[str, dict[str, str]] = {}
        self.puts: list[str] = []
        self.fail_next = 0

    def head(self, key: str) -> int | None:
        b = self.objects.get(key)
        return None if b is None else len(b)

    def put_file(self, key: str, path: Path, *, content_type: str,
                 cache_control: str) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionError("fake S3: injected failure")
        self.objects[key] = Path(path).read_bytes()
        self.headers[key] = {"ContentType": content_type, "CacheControl": cache_control}
        self.puts.append(key)
