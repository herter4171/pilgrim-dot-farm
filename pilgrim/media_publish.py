"""Media publishing (RADIO §8.4): S3 + CloudFront are the source of truth for
listener audio.

Every rendered MP3 is uploaded once to `media.bucket` under an immutable,
content-addressed key; the item's `s3_key` is set only after the PUT succeeds.
The program/state API then hands browsers `media.public_base_url + "/" + key`
(CloudFront in front of the private-to-CloudFront bucket). Until an item is
uploaded the API falls back to `/api/media/<id>` so playout never waits on S3
(hard rule 1): publishing runs as its own background task, never inside the
scheduler, selector or producer.

Credentials come from the AWS shared config (`media.aws_profile`); nothing
secret lives here or in config.yaml.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import Any, Protocol

from pilgrim.config import ROOT, Config

log = logging.getLogger("radio.media")

CONTENT_TYPE = "audio/mpeg"
ART_CONTENT_TYPE = "image/jpeg"
# A key embeds the content hash, so its bytes never change: cache for a year.
CACHE_CONTROL = "public, max-age=31536000, immutable"
RETRY_AFTER_S = 300.0  # a failed item waits this long before another attempt


class ObjectStore(Protocol):
    """The two S3 calls the publisher needs (fakes/fake_s3.py in tests)."""

    def head(self, key: str) -> int | None:
        """Object size in bytes, or None when the key does not exist."""
        ...

    def put_file(self, key: str, path: Path, *, content_type: str,
                 cache_control: str) -> None:
        ...


class S3ObjectStore:
    """boto3-backed store. Only `make media-smoke`, the backfill tool and the
    running station construct this; tests never do (AGENTS §1.2)."""

    def __init__(self, cfg: Config) -> None:
        import boto3
        from botocore.config import Config as BotoConfig

        m = cfg.media
        session = boto3.Session(profile_name=m.aws_profile or None,
                                region_name=m.region)
        self.bucket = m.bucket
        self._client = session.client("s3", config=BotoConfig(
            connect_timeout=min(m.timeout_s, 15.0), read_timeout=m.timeout_s,
            retries={"max_attempts": m.max_attempts, "mode": "standard"}))

    def head(self, key: str) -> int | None:
        from botocore.exceptions import ClientError
        try:
            r = self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return int(r["ContentLength"])

    def put_file(self, key: str, path: Path, *, content_type: str,
                 cache_control: str) -> None:
        # upload_file switches to multipart above 8 MB and retries parts.
        self._client.upload_file(str(path), self.bucket, key, ExtraArgs={
            "ContentType": content_type, "CacheControl": cache_control})


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def media_file(item: dict[str, Any]) -> Path:
    return ROOT / str(item["media_path"])


def object_key(cfg: Config, item: dict[str, Any], digest: str) -> str:
    """`audio/<type>/<file stem>-<sha256[:16]>.mp3`. Content-addressed so a
    reused item id (a rebuilt station.db) can never overwrite other bytes
    behind a year-long immutable cache."""
    stem = Path(str(item["media_path"])).stem
    return f"{cfg.media.key_prefix}{item['type']}/{stem}-{digest[:16]}.mp3"


def art_object_key(cfg: Config, item: dict[str, Any], digest: str) -> str:
    """`art/<song media stem>-<sha256[:16]>.jpg` (ALBUM_ART.md Step 6). The art
    file may carry a `-2` collision suffix; the song stem keeps the key stable
    across re-renders, and the hash keeps the bytes immutable behind the
    year-long cache."""
    stem = Path(str(item["media_path"])).stem
    return f"{cfg.art.key_prefix}{stem}-{digest[:16]}.jpg"


def art_url(cfg: Config, item: dict[str, Any] | None) -> str:
    """Where a browser fetches a song's cover: CloudFront once uploaded, else
    "" (no origin fallback — art is cosmetic; ALBUM_ART.md Step 7)."""
    key = (item or {}).get("art_s3_key")
    base = cfg.media.public_base_url.rstrip("/")
    return f"{base}/{key}" if key and base else ""


def public_url(cfg: Config, item: dict[str, Any] | None, item_id: int) -> str:
    """Where a browser fetches item audio: CloudFront once uploaded, else the
    origin's transitional `/api/media/<id>` route."""
    key = (item or {}).get("s3_key")
    base = cfg.media.public_base_url.rstrip("/")
    if key and base:
        return f"{base}/{key}"
    return f"/api/media/{item_id}"


class MediaPublisher:
    def __init__(self, cfg: Config, db: Any, objects: ObjectStore) -> None:
        self.cfg = cfg
        self.db = db
        self.objects = objects
        self._failed: dict[int, float] = {}   # item id -> monotonic failure time
        self._failed_art: dict[int, float] = {}  # same, for cover JPEGs
        self.published = 0
        self.art_published = 0

    def publish_item(self, item: dict[str, Any]) -> str:
        """Blocking: hash, skip if already there with the same size, PUT, record.
        Raises on any failure; the caller logs and retries later."""
        path = media_file(item)
        if path.suffix != ".mp3":
            raise ValueError(f"not an mp3: {path.name}")
        size = path.stat().st_size
        key = object_key(self.cfg, item, sha256_file(path))
        if self.objects.head(key) != size:
            self.objects.put_file(key, path, content_type=CONTENT_TYPE,
                                  cache_control=CACHE_CONTROL)
        self.db.set_published(int(item["id"]), key)
        return key

    def publish_art_item(self, item: dict[str, Any]) -> str:
        """Blocking: hash the JPEG, skip if already there with the same size,
        PUT, record. Raises on any failure; the caller retries later."""
        path = Path(str(item["art_path"]))
        if not path.exists():
            raise FileNotFoundError(f"art file missing: {path}")
        size = path.stat().st_size
        key = art_object_key(self.cfg, item, sha256_file(path))
        if self.objects.head(key) != size:
            self.objects.put_file(key, path, content_type=ART_CONTENT_TYPE,
                                  cache_control=CACHE_CONTROL)
        self.db.set_art_published(int(item["id"]), key)
        return key

    async def publish_pending_art(self) -> int:
        """Upload one batch of rendered cover JPEGs (ALBUM_ART.md Step 6)."""
        now = time.monotonic()
        batch = [it for it in self.db.items_to_publish_art(
                     limit=max(1, self.cfg.media.concurrency) * 8 + len(self._failed_art))
                 if now - self._failed_art.get(int(it["id"]), -RETRY_AFTER_S) >= RETRY_AFTER_S]
        if not batch:
            return 0
        sem = asyncio.Semaphore(max(1, self.cfg.media.concurrency))

        async def one(it: dict[str, Any]) -> bool:
            async with sem:
                t0 = time.monotonic()
                try:
                    key = await asyncio.to_thread(self.publish_art_item, it)
                except Exception as e:  # noqa: BLE001 - S3/disk errors degrade
                    self._failed_art[int(it["id"])] = time.monotonic()
                    log.warning("media.art_publish_failed", extra={
                        "item_id": it["id"], "stage": "publish",
                        "error": f"{type(e).__name__}: {e}"[:300]})
                    return False
                self._failed_art.pop(int(it["id"]), None)
                log.info("media.art_published", extra={
                    "item_id": it["id"], "stage": "publish", "key": key,
                    "duration_ms": round((time.monotonic() - t0) * 1000)})
                return True

        ok = sum(await asyncio.gather(*(one(it) for it in batch)))
        self.art_published += ok
        return ok

    async def publish_pending(self) -> int:
        """Upload one batch of unpublished audio, then any pending covers;
        returns how many objects succeeded. Art runs even when there is no
        audio to upload, and an art failure never affects audio."""
        ok = await self._publish_audio()
        try:
            ok += await self.publish_pending_art()
        except Exception as e:  # noqa: BLE001 - art never breaks audio publishing
            log.warning("media.art_loop_error", extra={"error": str(e)[:300]})
        return ok

    async def _publish_audio(self) -> int:
        now = time.monotonic()
        # over-fetch by the backed-off count so failures can't starve the rest
        batch = [it for it in self.db.items_to_publish(
                     limit=max(1, self.cfg.media.concurrency) * 8 + len(self._failed))
                 if now - self._failed.get(int(it["id"]), -RETRY_AFTER_S) >= RETRY_AFTER_S]
        if not batch:
            return 0
        sem = asyncio.Semaphore(max(1, self.cfg.media.concurrency))

        async def one(it: dict[str, Any]) -> bool:
            async with sem:
                t0 = time.monotonic()
                try:
                    key = await asyncio.to_thread(self.publish_item, it)
                except Exception as e:  # noqa: BLE001 - S3/disk errors degrade, never crash
                    self._failed[int(it["id"])] = time.monotonic()
                    log.warning("media.publish_failed", extra={
                        "item_id": it["id"], "stage": "publish",
                        "error": f"{type(e).__name__}: {e}"[:300]})
                    return False
                self._failed.pop(int(it["id"]), None)
                log.info("media.published", extra={
                    "item_id": it["id"], "stage": "publish", "key": key,
                    "duration_ms": round((time.monotonic() - t0) * 1000)})
                return True

        ok = sum(await asyncio.gather(*(one(it) for it in batch)))
        self.published += ok
        return ok

    async def run(self) -> None:
        """Background loop in the station process (server.py startup)."""
        while True:
            try:
                n = await self.publish_pending()
            except Exception as e:  # noqa: BLE001 - e.g. a DB hiccup; keep running
                log.warning("media.publish_loop_error", extra={"error": str(e)[:300]})
                n = 0
            if n == 0:
                await asyncio.sleep(self.cfg.media.poll_s)
