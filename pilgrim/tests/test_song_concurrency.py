"""Concurrent song generation (RADIO.md §6.1/§6.2, SONG_GEN_CONCURRENCY_PLAN):
the generator pool's slot accounting and transport, and the producer's song
decider. Fakes only: no backend is contacted (hard rule 2)."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import valid_brief
from pilgrim.config import Config, MusicBackend
from pilgrim.pipelines.songs import GeneratorUnavailable, MusicPool, SongPipeline
from test_producer import FakeVoice, make_song_producer


# --------------------------------------------------------------------------- #
# MusicPool
# --------------------------------------------------------------------------- #
class _T:
    """Fake monotonic clock + sleep for the pool (no wall time)."""

    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        # records the wait without moving the clock: concurrent sleepers all
        # see the same "now", as they would in real time
        self.slept.append(s)
        await asyncio.sleep(0)


def _pool(backends, stagger=0.0, cooldown=120.0) -> tuple[MusicPool, _T]:
    t = _T()
    return MusicPool([MusicBackend(**b) for b in backends], cooldown, stagger,
                     now=t.now, sleep=t.sleep), t


def test_pool_capacity_is_sum_of_slots_and_drops_while_down():
    pool, t = _pool([{"name": "a", "url": "http://a", "slots": 1},
                     {"name": "b", "url": "http://b", "slots": 2}])
    assert pool.capacity() == 3
    assert pool.mark_down("b") == 120.0
    assert pool.capacity() == 1
    t.t += 120.0
    assert pool.capacity() == 3          # cooldown over
    pool.mark_down("b")
    assert pool.mark_down("b") == 480.0  # third consecutive failure doubles twice
    pool.mark_ok("b")
    assert pool.capacity() == 3


def test_pool_never_leases_past_a_backends_slots():
    pool, _ = _pool([{"name": "a", "url": "http://a", "slots": 2}])

    async def main():
        held = []
        for _ in range(2):
            cm = pool.lease()
            held.append((cm, await cm.__aenter__()))
        assert pool.busy() == 2
        third = asyncio.ensure_future(pool.lease().__aenter__())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not third.done()            # waits instead of doubling up
        await held[0][0].__aexit__(None, None, None)
        b = await asyncio.wait_for(third, 1.0)
        assert b.name == "a" and pool.busy() == 2
    asyncio.run(main())


def test_pool_prefers_fast_for_requests_and_slow_for_stock():
    pool, _ = _pool([{"name": "mlx", "url": "http://m", "rtf_hint": 0.25},
                     {"name": "gpu", "url": "http://g", "rtf_hint": 0.45}])

    async def main():
        async with pool.lease("fast") as b:
            assert b.name == "gpu"
        async with pool.lease("slow") as b:
            assert b.name == "mlx"
        # fast box busy -> the other one
        async with pool.lease("fast") as fast, pool.lease("fast") as other:
            assert (fast.name, other.name) == ("gpu", "mlx")
    asyncio.run(main())
    pool.record("mlx", 100.0, 100.0)  # measured rtf 1.0 overtakes gpu (EWMA)
    assert pool.rtf()["mlx"] > pool.rtf()["gpu"]


def test_pool_staggers_submissions():
    """LiteLLM least-busy only fans out calls that arrive staggered."""
    pool, t = _pool([{"name": "l", "url": "http://l", "slots": 3}], stagger=3.0)
    starts: list[float] = []

    async def one():
        async with pool.lease():
            starts.append(t.now())
            await asyncio.sleep(0)

    async def main():
        await asyncio.gather(one(), one(), one())
    asyncio.run(main())
    # submit slots reserved 0 / 3 / 6 s after the first
    assert len(starts) == 3
    assert sorted(t.slept) == [3.0, 6.0]


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status=200, headers=None):
        self.status_code = status
        self.content = b"FAKEWAV"
        self.text = "busy"
        self.headers = headers or {}


class _Post:
    def __init__(self, resp):
        self.resp = resp
        self.calls: list[tuple[str, dict, dict]] = []

    async def post(self, url, json, headers=None):
        self.calls.append((url, json, headers or {}))
        return self.resp


def _sp(cfg, tmp_path, resp, backends) -> SongPipeline:
    cfg.songs.gen_backends = [MusicBackend(**b) for b in backends]
    sp = SongPipeline(cfg, llm=None, media_dir=tmp_path, api_key="tok")  # type: ignore[arg-type]
    sp._client = _Post(resp)  # type: ignore[assignment]
    return sp


def test_litellm_backend_gets_model_and_bearer_and_single_path(cfg, tmp_path):
    sp = _sp(cfg, tmp_path, _Resp(headers={"x-generated-by": "audiocpp/MiniMax-Music-3"}),
             [{"name": "litellm", "url": "http://localhost:4000/vllm/", "slots": 3,
               "auth": "litellm"}])
    gen = asyncio.run(sp.generate(valid_brief(seed=3)))
    url, payload, headers = sp._client.calls[0]  # type: ignore[attr-defined]
    assert url == "http://localhost:4000/vllm/v1/audio/music-generations"
    assert payload["model"] == cfg.models.music
    assert headers["Authorization"] == "Bearer tok"
    assert gen["backend"] == "litellm"
    assert gen["generator"] == "audiocpp/MiniMax-Music-3"


def test_direct_backend_sends_no_model_or_auth(cfg, tmp_path):
    sp = _sp(cfg, tmp_path, _Resp(), [{"name": "m5", "url": "http://127.0.0.1:11234"}])
    gen = asyncio.run(sp.generate(valid_brief(seed=3)))
    url, payload, headers = sp._client.calls[0]  # type: ignore[attr-defined]
    assert url == "http://127.0.0.1:11234/v1/audio/music-generations"
    assert "model" not in payload and headers == {}
    assert gen["generator"] == "m5"


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_overloaded_generator_is_a_transport_error_and_cools_down(cfg, tmp_path, status):
    """An outage must re-queue a request without burning an attempt."""
    sp = _sp(cfg, tmp_path, _Resp(status=status),
             [{"name": "a", "url": "http://a", "slots": 2}])
    with pytest.raises(GeneratorUnavailable) as ei:
        asyncio.run(sp._render(valid_brief(seed=1), "fast"))
    assert isinstance(ei.value, httpx.TransportError)
    assert sp.capacity() == 0
    assert list(tmp_path.iterdir()) == []


def test_sweep_temp_only_removes_render_temp_files(cfg, tmp_path):
    sp = _sp(cfg, tmp_path, _Resp(), [{"name": "a", "url": "http://a"}])
    (tmp_path / "_song_tmp_ab12.wav").write_bytes(b"x")
    (tmp_path / "song_1.flac").write_bytes(b"x")
    assert sp.sweep_temp() == 1
    assert [p.name for p in tmp_path.iterdir()] == ["song_1.flac"]


def test_config_requires_a_backend_and_music_model_for_litellm(base_config):
    data = base_config.model_dump()
    data["songs"]["gen_backends"] = []
    with pytest.raises(ValueError, match="gen_backends"):
        Config.model_validate(data)
    data = base_config.model_dump()
    data["models"]["music"] = None
    data["songs"]["gen_backends"] = [{"name": "litellm", "slots": 3, "auth": "litellm"}]
    with pytest.raises(ValueError, match="models.music"):
        Config.model_validate(data)
    data = base_config.model_dump()
    data["songs"]["gen_backends"][0]["slots"] = 0
    with pytest.raises(ValueError, match="slots"):
        Config.model_validate(data)


# --------------------------------------------------------------------------- #
# producer decider (song_loop)
# --------------------------------------------------------------------------- #
FAMILIES = ["jazz-swing-big-band", "soul-blues-gospel", "hip-hop-rap",
            "metal-heavy-rock", "country-americana", "general-pop-ballad"]


class GateSong:
    """Fake SongPipeline: a fixed capacity and renders that finish only when
    the test releases them, so concurrency can be observed exactly."""

    def __init__(self, cap: int = 3) -> None:
        self.cap = cap
        self.active = 0
        self.peak = 0
        self.briefs: list[tuple[str | None, list[str]]] = []
        self.renders: list[tuple[int | None, str, str]] = []  # (request_id, prefer, genre)
        self.pending: list[tuple[dict, asyncio.Event]] = []
        self.fail_with: BaseException | None = None
        self.fail_ids: set[int] = set()   # request renders that fail on release
        self.brief_gate: asyncio.Event | None = None  # hold briefs until set
        self.brief_gates: dict[str, asyncio.Event] = {}  # per request text
        self._n = 0

    def capacity(self) -> int:
        return self.cap

    async def brief(self, genres, request_text=None, short=False, paid=False):
        self.briefs.append((request_text, list(genres)))
        if self.brief_gate is not None:
            await self.brief_gate.wait()
        if request_text in self.brief_gates:
            await self.brief_gates[request_text].wait()
        fam = FAMILIES[self._n % len(FAMILIES)]
        self._n += 1
        await asyncio.sleep(0)
        return valid_brief(fam, title=f"Song {self._n}", artist="Fakes",
                           lyrics="[verse]\nla")

    async def produce_song(self, brief, prefer="slow"):
        self.renders.append((brief.get("request_id"), prefer, brief["genre"]))
        if self.fail_with is not None:
            raise self.fail_with
        self.active += 1
        self.peak = max(self.peak, self.active)
        ev = asyncio.Event()
        self.pending.append((brief, ev))
        try:
            await ev.wait()
        finally:
            self.active -= 1
        if brief.get("request_id") in self.fail_ids:
            self.fail_ids.discard(brief["request_id"])
            raise RuntimeError("song QC failed")
        return {"type": "song", "media_path": f"/tmp/{brief['title']}.flac",
                "duration_s": 45.0, "title": brief["title"], "artist": brief["artist"],
                "genre": brief["genre"], "meta": {"seed": 1}}

    def release(self, request_id=None, stock=False) -> None:
        for i, (b, ev) in enumerate(self.pending):
            if (stock and b.get("request_id") is None) or (
                    not stock and b.get("request_id") == request_id):
                ev.set()
                self.pending.pop(i)
                return
        raise AssertionError(f"no pending render for {request_id=} {stock=}")


async def _until(cond, timeout: float = 3.0) -> None:
    for _ in range(int(timeout / 0.002)):
        if cond():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("condition not reached")


SLEPT: list[float] = []


def _add_paid(store, text):
    r = store.add_request(text, cap=10)
    store._conn.execute("UPDATE requests SET priority=1 WHERE id=?", (r["id"],))
    store._conn.commit()
    return r


def _ready_order(store) -> list[int]:
    """Record the order requests become ready (= air order, §5.2)."""
    order: list[int] = []
    orig = store.mark_request_ready

    def rec(request_id, *a, **kw):
        order.append(request_id)
        return orig(request_id, *a, **kw)
    store.mark_request_ready = rec
    return order


def _status(store, r) -> str:
    return store.get_request(r["id"])["status"]


def _loop_producer(cfg, store, songs):
    """Producer whose song_loop waits are recorded and take ~no wall time."""
    prod = make_song_producer(cfg, store, FakeVoice(), songs)
    SLEPT.clear()

    async def fake_sleep(s):
        SLEPT.append(s)
        await asyncio.sleep(0.001)
    prod._sleep = fake_sleep  # type: ignore[method-assign]
    return prod


def _run(prod, scenario) -> None:
    async def main():
        loop_task = asyncio.create_task(prod.song_loop())
        try:
            await scenario()
        finally:
            loop_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await loop_task
    asyncio.run(main())


def test_stock_and_free_requests_leave_the_last_slot_for_paid(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 10
    cfg.songs.stock_gap_s = 0
    songs = GateSong(cap=3)
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: songs.active == 2)
        await asyncio.sleep(0.02)
        assert songs.active == 2                      # slot 3 held back
        free = store.add_request("a waltz for the goats", cap=10)
        await asyncio.sleep(0.02)
        assert songs.active == 2 and _status(store, free) == "queued"
        paid = _add_paid(store, "a paid polka")
        await _until(lambda: songs.active == 3)       # paid takes it at once
        assert (paid["id"], "fast") in [(r, p) for r, p, _ in songs.renders]
        songs.release(paid["id"])
        await _until(lambda: _status(store, paid) == "ready")
        await asyncio.sleep(0.02)
        assert songs.active == 2                      # nothing refills slot 3
    _run(prod, scenario)
    assert songs.peak == 3
    assert all(p == "slow" for r, p, _ in songs.renders if r is None)


def test_capacity_one_is_the_serial_worker(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 10
    cfg.songs.stock_gap_s = 0
    songs = GateSong(cap=1)
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: songs.active == 1)
        req = store.add_request("a song", cap=10)
        await asyncio.sleep(0.02)
        assert songs.active == 1                       # request waits for the slot
        songs.release(stock=True)
        await _until(lambda: any(r == req["id"] for r, _, _ in songs.renders))
    _run(prod, scenario)
    assert songs.peak == 1


def test_in_flight_stock_counts_toward_the_fresh_target(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 2
    cfg.songs.stock_gap_s = 0
    songs = GateSong(cap=5)
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: songs.active == 2)
        await asyncio.sleep(0.02)
        assert songs.active == 2 and len(songs.renders) == 2
        songs.release(stock=True)
        songs.release(stock=True)
        await _until(lambda: len(store.list_items("song")) == 2)
        await asyncio.sleep(0.02)
        assert len(songs.renders) == 2                 # target met: no overshoot
    _run(prod, scenario)


def test_two_free_requests_render_at_once_and_a_third_waits(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0               # stock out of the way
    a = store.add_request("first song", cap=10)
    b = store.add_request("second song", cap=10)
    c = store.add_request("third song", cap=10)
    songs = GateSong(cap=3)
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: songs.active == 2)
        await asyncio.sleep(0.02)
        assert [t for t, _ in songs.briefs] == ["first song", "second song"]
        assert _status(store, c) == "queued"          # last slot is for paid
        paid = _add_paid(store, "paid song")
        await _until(lambda: songs.active == 3)
        songs.release(paid["id"])                     # paid is ahead of a in queue order
        songs.release(a["id"])
        await _until(lambda: len(songs.briefs) == 4)  # c starts once a is done
        await _until(lambda: songs.active == 2)       # b and c
        for r in (b, c):
            songs.release(r["id"])
        await _until(lambda: _status(store, c) == "ready")
    _run(prod, scenario)
    assert songs.peak == 3


def test_free_request_finishing_first_waits_for_the_older_one(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    a = store.add_request("first song", cap=10)
    b = store.add_request("second song", cap=10)
    order = _ready_order(store)
    songs = GateSong(cap=3)
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: songs.active == 2)
        songs.release(b["id"])                        # b finishes first
        await asyncio.sleep(0.05)
        assert _status(store, b) == "producing"       # held, not ready
        assert store.list_items("song") == []         # and not in the library
        # the board must not say "being generated" for a finished song
        assert prod.request_stage[b["id"]] == "rendered"
        assert prod.request_stage[a["id"]] == "generating"
        songs.release(a["id"])
        await _until(lambda: _status(store, b) == "ready")
    _run(prod, scenario)
    assert order == [a["id"], b["id"]]


def test_held_request_goes_when_the_one_ahead_fails(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    a = store.add_request("first song", cap=10)
    b = store.add_request("second song", cap=10)
    songs = GateSong(cap=3)
    songs.fail_ids.add(a["id"])
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: songs.active == 2)
        songs.release(b["id"])
        await asyncio.sleep(0.05)
        assert _status(store, b) == "producing"
        songs.release(a["id"])                        # a's render fails
        await _until(lambda: _status(store, b) == "ready")
        assert int(store.get_request(a["id"])["attempts"] or 0) == 1
    _run(prod, scenario)


def test_free_request_waits_for_an_in_flight_paid_one(cfg, tmp_env):
    """Paid is ahead in queue order even when it started later."""
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    free = store.add_request("free song", cap=10)
    order = _ready_order(store)
    songs = GateSong(cap=3)
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: songs.active == 1)
        paid = _add_paid(store, "paid song")
        await _until(lambda: songs.active == 2)
        songs.release(free["id"])
        await asyncio.sleep(0.05)
        assert _status(store, free) == "producing"
        songs.release(paid["id"])
        await _until(lambda: _status(store, free) == "ready")
        assert order == [paid["id"], free["id"]]
    _run(prod, scenario)


def test_paid_requests_render_in_parallel_and_air_in_queue_order(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    order = _ready_order(store)
    songs = GateSong(cap=3)
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        p1 = _add_paid(store, "paid one")
        await _until(lambda: songs.active == 1)
        p2 = _add_paid(store, "paid two")
        await _until(lambda: songs.active == 2)       # second paid doesn't wait
        songs.release(p2["id"])                       # finishes first...
        await asyncio.sleep(0.05)
        assert _status(store, p2) == "producing"      # ...held behind p1
        songs.release(p1["id"])
        await _until(lambda: _status(store, p2) == "ready")
        assert order == [p1["id"], p2["id"]]
    _run(prod, scenario)


def test_two_paid_and_a_free_request_fill_all_three_slots(cfg, tmp_env):
    """Live incident 2026-11-01: paid 494 rendering, paid 497 and free 493
    queued. Only the queue head was considered, so two slots sat idle."""
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    free = store.add_request("free song", cap=10)
    p1 = _add_paid(store, "paid one")
    p2 = _add_paid(store, "paid two")
    songs = GateSong(cap=3)
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: songs.active == 3)
        assert {r for r, _, _ in songs.renders} == {free["id"], p1["id"], p2["id"]}
    _run(prod, scenario)


def test_parallel_stock_briefs_see_in_flight_genres(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 10
    cfg.songs.stock_gap_s = 0
    songs = GateSong(cap=3)
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: songs.active == 2)
    _run(prod, scenario)
    first_genre = songs.renders[0][2]
    assert first_genre in songs.briefs[1][1]


def test_outage_requeues_request_and_pauses_new_jobs(cfg, tmp_env):
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    req = store.add_request("a song", cap=10)
    songs = GateSong(cap=3)
    songs.fail_with = httpx.ConnectError("generator down")
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: 30.0 in SLEPT)             # 15 s, then doubled
    _run(prod, scenario)
    r = store.get_request(req["id"])
    # never failed, no attempt burned (it may be mid-retry when the loop stops;
    # a restart's reset_producing_to_queued covers that)
    assert r["status"] in ("queued", "producing")
    assert int(r["attempts"] or 0) == 0
    assert [s for s in SLEPT if s != 20.0][:2] == [15.0, 30.0]


def test_litellm_backend_url_follows_hosts_litellm(base_config):
    """The M5 points hosts.litellm at the proxy's LAN address (RUNBOOK); the
    song pool must follow it rather than hardcode localhost."""
    data = base_config.model_dump()
    data["hosts"]["litellm"] = "http://10.0.0.5:4000/v1/"
    data["songs"]["gen_backends"] = [{"name": "litellm", "slots": 3, "auth": "litellm"}]
    cfg = Config.model_validate(data)
    assert cfg.songs.gen_backends[0].url == "http://10.0.0.5:4000/vllm"
    data["songs"]["gen_backends"] = [{"name": "m5"}]
    with pytest.raises(ValueError, match="url is required"):
        Config.model_validate(data)


def test_board_stage_is_writing_then_generating(cfg, tmp_env):
    """The board says "being written" while the lyrics/brief are written and
    "being generated" once the song model runs (RADIO §11 `stage`); a held,
    finished song is `rendered` (test_free_request_finishing_first_...)."""
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    req = store.add_request("a hymn for the hens", cap=10)
    songs = GateSong(cap=3)
    songs.brief_gate = asyncio.Event()
    prod = _loop_producer(cfg, store, songs)

    def stage():
        rows = store.request_board(10, stages=prod.request_stage)["queue"]
        return rows[0]["stage"] if rows else None

    async def scenario():
        await _until(lambda: len(songs.briefs) == 1)
        assert stage() == "writing"   # "waiting" only while another brief runs
        songs.brief_gate.set()
        await _until(lambda: songs.active == 1)
        assert stage() == "generating"
        songs.release(req["id"])
        await _until(lambda: _status(store, req) == "ready")
        assert stage() == "" and prod.request_stage == {}
    _run(prod, scenario)


def test_board_stage_only_on_producing_rows(cfg, tmp_env):
    _, store, _ = tmp_env
    q = store.add_request("queued one", cap=10)
    p = store.add_request("producing one", cap=10)
    store.mark_request_producing(p["id"])
    rows = {r["text"]: r for r in store.request_board(10)["queue"]}
    assert rows["queued one"]["stage"] == ""
    assert rows["producing one"]["stage"] == "writing"   # default without a stage map
    rows = {r["text"]: r for r in
            store.request_board(10, stages={p["id"]: "generating", q["id"]: "x"})["queue"]}
    assert rows["producing one"]["stage"] == "generating"
    assert rows["queued one"]["stage"] == ""


# --------------------------------------------------------------------------- #
# cut-off briefs (2026-11-01: qwen38 streams ended empty at ~122 s)
# --------------------------------------------------------------------------- #
def test_chat_json_marks_cut_off_output_as_truncated(cfg):
    from pilgrim.pipelines.llm import LLM, LLMError, LLMTruncated
    llm = LLM(cfg, "tok")

    def answer(text):
        async def fake(*a, **kw):
            return text
        llm._complete = fake  # type: ignore[method-assign]

    for cut in ("", "   ", '{"title": "Mittens", "lyrics": "[verse]\\nme'):
        answer(cut)
        with pytest.raises(LLMTruncated):
            asyncio.run(llm.chat_json("m", "s", "u"))
    answer("I refuse to answer}")          # complete but not JSON: a real failure
    with pytest.raises(LLMError) as ei:
        asyncio.run(llm.chat_json("m", "s", "u"))
    assert not isinstance(ei.value, LLMTruncated)


class _CutSong:
    def __init__(self):
        self.calls = 0

    async def brief(self, genres, request_text=None, short=False, paid=False):
        from pilgrim.pipelines.llm import LLMTruncated
        self.calls += 1
        raise LLMTruncated("llm empty content")

    async def produce_song(self, brief, prefer="slow"):
        raise AssertionError("never reached")


def test_cut_off_briefs_requeue_free_then_count_attempts(cfg, tmp_env):
    from pilgrim.pipelines.llm import LLMTruncated
    from pilgrim.producer import LLM_FREE_CUTS
    cfg, store, _ = tmp_env
    req = store.add_request("a song", cap=10)
    prod = make_song_producer(cfg, store, FakeVoice(), _CutSong())
    for _ in range(LLM_FREE_CUTS):
        with pytest.raises(LLMTruncated):
            asyncio.run(prod.song_step())
        r = store.get_request(req["id"])
        assert r["status"] == "queued" and int(r["attempts"] or 0) == 0
    with pytest.raises(LLMTruncated):
        asyncio.run(prod.song_step())
    assert int(store.get_request(req["id"])["attempts"]) == 1
    assert prod.request_stage == {}


def test_request_waiting_for_the_brief_llm_shows_waiting(cfg, tmp_env):
    """Briefs run one at a time: the request whose brief hasn't started is
    `waiting` ("in line" on the board), not "being written"."""
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    a = store.add_request("first song", cap=10)
    b = store.add_request("second song", cap=10)
    songs = GateSong(cap=3)
    songs.brief_gate = asyncio.Event()
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: len(songs.briefs) == 1)
        await _until(lambda: prod.request_stage.get(b["id"]) == "waiting")
        assert prod.request_stage[a["id"]] == "writing"
        songs.brief_gate.set()
        await _until(lambda: songs.active == 2)
        assert prod.request_stage == {a["id"]: "generating", b["id"]: "generating"}
    _run(prod, scenario)



def test_priority_gate_grants_lowest_key_not_first_come():
    from pilgrim.producer import STOCK_BRIEF_KEY, PriorityGate
    gate = PriorityGate()
    order: list[str] = []

    async def take(name, key, started):
        async with gate.hold(key):
            order.append(name)
            started.set()
            await asyncio.sleep(0.01)

    async def main():
        first = asyncio.Event()
        holder = asyncio.create_task(take("holder", (0, "a", 1), first))
        await first.wait()
        evs = [asyncio.Event() for _ in range(3)]
        tasks = [asyncio.create_task(take("stock", STOCK_BRIEF_KEY, evs[0])),
                 asyncio.create_task(take("free", (0, "b", 2), evs[1])),
                 asyncio.create_task(take("paid", (-1, "c", 3), evs[2]))]
        await asyncio.gather(holder, *tasks)
    asyncio.run(main())
    assert order == ["holder", "paid", "free", "stock"]


def test_priority_gate_survives_a_cancelled_waiter():
    from pilgrim.producer import PriorityGate
    gate = PriorityGate()

    async def main():
        async with gate.hold((0,)):
            waiter = asyncio.create_task(gate.hold((0,)).__aenter__())
            await asyncio.sleep(0)
            waiter.cancel()
        async with gate.hold((0,)):   # not stuck busy
            pass
    asyncio.run(asyncio.wait_for(main(), 1.0))


def test_paid_request_briefs_before_an_older_free_one(cfg, tmp_env):
    """Live incident 2026-11-01: paid 494 re-queued after a cut-off waited
    behind free 493 for the brief LLM."""
    cfg, store, _ = tmp_env
    cfg.inventory.fresh_songs_ready = 0
    cfg.songs.free_request_slots = 2
    first = store.add_request("free first", cap=10)
    store.add_request("free second", cap=10)
    songs = GateSong(cap=4)
    songs.brief_gates["free first"] = asyncio.Event()
    prod = _loop_producer(cfg, store, songs)

    async def scenario():
        await _until(lambda: [t for t, _ in songs.briefs] == ["free first"])
        await _until(lambda: prod.request_stage.get(first["id"] + 1) == "waiting")
        _add_paid(store, "paid later")
        await _until(lambda: len(prod._jobs) == 3)
        songs.brief_gates["free first"].set()
        await _until(lambda: len(songs.briefs) == 3)
    _run(prod, scenario)
    assert [t for t, _ in songs.briefs] == ["free first", "paid later", "free second"]



def test_default_output_is_capped_at_what_the_server_admits(cfg):
    """Near-context max_tokens never got admitted while other calls ran
    ("expired while waiting for admission"); models.output_tokens caps it."""
    from pilgrim.pipelines.llm import LLM
    cfg.models.context_tokens = {"qwen38": 262144}
    cfg.models.output_tokens = {"qwen38": 32768}
    assert LLM(cfg, "tok").room_for("qwen38", "s", "u") == 32768


def test_in_stream_backend_error_is_a_retryable_cut(cfg):
    import httpx as _h
    from pilgrim.pipelines.llm import LLM, LLMTruncated
    sse = ('data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n'
           'data: {"error": {"message": "inference request expired while waiting '
           'for admission", "code": "500"}}\n\n')

    def handler(request):
        return _h.Response(200, text=sse, headers={"content-type": "text/event-stream"})
    llm = LLM(cfg, "tok", client=_h.AsyncClient(transport=_h.MockTransport(handler)))
    with pytest.raises(LLMTruncated, match="admission"):
        asyncio.run(llm.chat_json("qwen38", "s", "u"))


# --------------------------------------------------------------------------- #
# slots: auto — the pool counts the generators behind the proxy (RADIO §6.2,
# 2026-11-07). Fakes only: a scripted discover callable / a mock transport.
# --------------------------------------------------------------------------- #
class _Discover:
    """Scripted discovery: each call pops the next answer; the last repeats."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self, backend):
        self.calls += 1
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _auto_pool(disc, interval=300.0):
    t = _T()
    pool = MusicPool([MusicBackend(name="litellm", slots="auto", auth="litellm")],
                     120.0, 0.0, now=t.now, sleep=t.sleep,
                     discover=disc, discover_interval_s=interval)
    return pool, t


def test_config_auto_slots_need_litellm(base_config):
    data = base_config.model_dump()
    data["songs"]["gen_backends"] = [{"name": "litellm", "slots": "auto", "auth": "litellm"}]
    assert Config.model_validate(data).songs.gen_backends[0].slots == "auto"
    data["songs"]["gen_backends"] = [{"name": "m5", "slots": "auto",
                                      "url": "http://127.0.0.1:11234"}]
    with pytest.raises(ValueError, match="auto"):
        Config.model_validate(data)
    data = base_config.model_dump()
    data["songs"]["discover_interval_s"] = 0
    with pytest.raises(ValueError, match="discover_interval_s"):
        Config.model_validate(data)


def test_auto_slots_start_at_one_then_follow_discovery():
    disc = _Discover(3, None, 0, 2)
    pool, t = _auto_pool(disc)
    assert pool.capacity() == 1 and pool.slots() == {"litellm": 1}

    async def main():
        await pool.refresh()
        assert pool.capacity() == 3 and disc.calls == 1
        t.t = 100.0
        await pool.refresh()                 # inside the interval: no recount
        assert disc.calls == 1
        t.t = 301.0
        await pool.refresh()                 # None = proxy unreadable: keep 3
        assert disc.calls == 2 and pool.capacity() == 3
        t.t = 700.0
        await pool.refresh()                 # 0 healthy floors at 1
        assert pool.capacity() == 1
        pool.mark_down("litellm")            # a failure forces the next recount
        t.t = 700.0 + 120.0
        async with pool.lease("fast") as b:  # lease refreshes before picking
            assert b.name == "litellm"
        assert disc.calls == 4 and pool.capacity() == 2
    asyncio.run(main())


def test_fixed_slots_never_call_discovery():
    disc = _Discover(5)
    t = _T()
    pool = MusicPool([MusicBackend(name="a", url="http://a", slots=2)], 120.0, 0.0,
                     now=t.now, sleep=t.sleep, discover=disc)

    async def main():
        await pool.refresh(force=True)
        async with pool.lease():
            pass
        assert disc.calls == 0 and pool.capacity() == 2
    asyncio.run(main())


def test_discovery_failure_keeps_the_last_count():
    async def boom(backend):
        raise RuntimeError("proxy exploded")
    pool, t = _auto_pool(_Discover(2))

    async def main():
        await pool.refresh()
        assert pool.capacity() == 2
        pool._discover = boom
        await pool.refresh(force=True)
        assert pool.capacity() == 2
    asyncio.run(main())


def test_lease_never_exceeds_the_discovered_slots():
    pool, _ = _auto_pool(_Discover(1))

    async def main():
        cm = pool.lease()
        await cm.__aenter__()                # discovers 1 slot, takes it
        assert pool.capacity() == 1 and pool.busy() == 1
        second = asyncio.ensure_future(pool.lease().__aenter__())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not second.done()             # waits for the one slot
        await cm.__aexit__(None, None, None)
        b = await asyncio.wait_for(second, 1.0)
        assert b.name == "litellm" and pool.busy() == 1
    asyncio.run(main())


async def test_discover_counts_live_deployments_from_model_info(cfg):
    from pilgrim.pipelines.songs import discover_litellm_deployments
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/model/info":
            assert request.headers.get("authorization") == "Bearer tok"
            return httpx.Response(200, json={"data": [
                {"model_name": cfg.models.music,
                 "litellm_params": {"api_base": "http://box-a:8080"}},
                {"model_name": cfg.models.music,
                 "litellm_params": {"api_base": "http://box-b:8080"}},
                {"model_name": cfg.models.music,
                 "litellm_params": {"api_base": "http://m5:11234"}},
                {"model_name": cfg.models.music,
                 "litellm_params": {"api_base": "http://m5:11234"}},   # duplicate row
                {"model_name": "qwen38",
                 "litellm_params": {"api_base": "http://llm:8000"}},
            ]})
        host = request.url.host
        if host == "box-a":
            return httpx.Response(404)          # live box without a /health route
        if host == "box-b":
            raise httpx.ConnectError("refused")  # audiocpp down
        if host == "m5":
            return httpx.Response(200)
        raise AssertionError(f"unexpected probe {request.url}")

    backend = MusicBackend(name="litellm", slots="auto", auth="litellm")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await discover_litellm_deployments(cfg, client, "tok", backend) == 2
    root = cfg.hosts.litellm.rstrip("/").removesuffix("/v1")
    assert seen[0] == root + "/model/info"
    assert not any("llm:8000" in u for u in seen)      # other models are not probed
    assert sum("m5:11234" in u for u in seen) == 1     # duplicates probed once

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("proxy down")
    client = httpx.AsyncClient(transport=httpx.MockTransport(down))
    assert await discover_litellm_deployments(cfg, client, "tok", backend) is None

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})
    client = httpx.AsyncClient(transport=httpx.MockTransport(empty))
    assert await discover_litellm_deployments(cfg, client, "tok", backend) == 0
