"""Audio normalization and encoding (RADIO.md §8.2).

Two-pass `loudnorm` to the configured LUFS/TP, resample to the delivery rate
(ffmpeg's loudnorm secretly up-samples to 192 kHz), trim outer silence below
a threshold, and deliver a 48 kHz **stereo** CBR MP3 with a LAME/Info gapless
header. Stereo and 48 kHz are not taste: WebKit (Safari, every iOS browser)
ignores the gapless header on mono and on MPEG-2 (24 kHz) MP3s and leaves
~576 samples of encoder delay at the front of the decoded buffer, which breaks
sample-accurate joins (docs/backends.md §9).
"""
from __future__ import annotations

import json
import logging
import re
import struct
import subprocess
from pathlib import Path

import soundfile as sf

from pilgrim.config import Config

log = logging.getLogger("radio.normalize")


class NormalizeError(Exception):
    pass


def _ffmpeg() -> str:
    # prefer a bundled static build on PATH, else "ffmpeg"
    return "ffmpeg"


def probe(path: Path) -> tuple[int, int]:
    """Return (sample_rate, channels)."""
    info = sf.info(str(path))
    return int(info.samplerate), info.channels


def measure(path: Path, cfg: Config) -> dict[str, str]:
    """Pass 1: measure integrated loudness params."""
    cmd = [_ffmpeg(), "-y", "-i", str(path),
           "-af", (f"loudnorm=I={cfg.audio.lufs}:TP={cfg.audio.true_peak_db}:"
                   f"LRA={cfg.audio.lra}:print_format=json"),
           "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise NormalizeError(f"loudnorm measure failed: {p.stderr[-500:]}")
    # parse the JSON block printed to stderr
    m = re.search(r"\{.*?\"output_i\".*?\}", p.stderr, re.DOTALL)
    if not m:
        raise NormalizeError("could not parse loudnorm measurement output")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise NormalizeError(f"bad loudnorm JSON: {e}") from e


def normalize(src: Path, dst: Path, cfg: Config, fade_out_s: float = 0.0) -> dict[str, object]:
    """Two-pass loudnorm, resample, trim silence, encode MP3 at `dst`.

    When fade_out_s > 0 the tail chain becomes areverse,{trim},fade-in,areverse:
    a fade-IN on reversed audio is a fade-OUT on the original, so no EU duration
    is needed (OVERHAUL 3.2, soft landing for songs with hard endings).
    """
    _sr, channels = probe(src)
    measured = measure(src, cfg)

    def g(k: str, default: str = "0") -> str:
        return str(measured.get(k, default))

    silence_db = str(cfg.audio.silence_db)
    pad_ms = cfg.audio.edge_pad_ms
    # Trim ONLY outer edges. The old `stop_periods=1:stop_duration=0.25` truncated
    # the whole item at its FIRST >=250 ms quiet segment (any normal speech pause),
    # silently discarding the remainder of a commercial/liner/song. New approach:
    # trim leading silence, then reverse + trim leading (the original tail) + reverse.
    # This never cuts an internal pause or stops the output early.
    trim = f"silenceremove=start_periods=1:start_silence=0.05:start_threshold={silence_db}dB"
    tail = (f"areverse,{trim},areverse" if fade_out_s <= 0
            else f"areverse,{trim},afade=t=in:d={fade_out_s},areverse")
    af = (
        f"loudnorm=I={cfg.audio.lufs}:TP={cfg.audio.true_peak_db}:LRA={cfg.audio.lra}:"
        f"measured_I={g('input_i','-30')}:measured_TP={g('input_tp','-10')}:"
        f"measured_LRA={g('input_lra','0')}:measured_thresh={g('input_thresh','-40')}:linear=true,"
        f"aresample={cfg.audio.sample_rate},"
        f"{trim},"
        f"{tail},"
        f"adelay={pad_ms}:all=1"
    )
    pcm = dst.with_name(f"_norm_{dst.stem}.wav")
    cmd = [_ffmpeg(), "-y", "-i", str(src), "-af", af, "-ac", "2",
           "-c:a", "pcm_s24le", str(pcm)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if p.returncode != 0 or not pcm.exists():
            raise NormalizeError(f"normalize failed: {p.stderr[-500:]}")
        # mono sources are speech/SFX: the lower bitrate is plenty even though
        # the delivered file is stereo (identical channels cost ~nothing).
        kbps = cfg.audio.mp3_kbps if channels > 1 else cfg.audio.mp3_mono_source_kbps
        return encode_mp3(pcm, dst, cfg, kbps)
    finally:
        pcm.unlink(missing_ok=True)


def encode_mp3(src: Path, dst: Path, cfg: Config, kbps: int) -> dict[str, object]:
    """Encode `src` (any decodable audio, already loudness-normalized) to a
    48 kHz stereo CBR MP3 at `dst`, then prove the gapless header is present
    and consistent. No loudness processing: the backfill (tools/mp3_migrate)
    uses this directly on already-normalized FLAC masters."""
    sr = cfg.audio.sample_rate
    cmd = [_ffmpeg(), "-y", "-i", str(src), "-map", "0:a:0", "-map_metadata", "-1",
           "-af", f"aresample={sr}", "-ac", "2", "-ar", str(sr),
           "-c:a", "libmp3lame", "-b:a", f"{kbps}k", "-write_xing", "1", str(dst)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode != 0 or not dst.exists():
        raise NormalizeError(f"mp3 encode failed: {p.stderr[-500:]}")
    try:
        gap = gapless_info(dst)
        d = sf.info(str(dst))  # libsndfile honours the LAME delay/padding
    except (NormalizeError, RuntimeError) as e:
        dst.unlink(missing_ok=True)
        raise NormalizeError(f"mp3 verify failed: {e}") from e
    if gap["delay"] + int(d.frames) + gap["padding"] != gap["total_frames"]:
        dst.unlink(missing_ok=True)
        raise NormalizeError(f"mp3 gapless header inconsistent: {gap} vs {d.frames} frames")
    return {"sample_rate": int(d.samplerate), "channels": d.channels,
            "duration_s": round(float(d.frames) / d.samplerate, 3),
            "frames": int(d.frames), "enc_delay": gap["delay"],
            "enc_padding": gap["padding"], "bytes": dst.stat().st_size,
            "codec": "mp3", "kbps": kbps}


def _skip_id3(b: bytes) -> int:
    if b[:3] == b"ID3" and len(b) >= 10:
        size = (b[6] << 21) | (b[7] << 14) | (b[8] << 7) | b[9]
        return 10 + size + (10 if b[5] & 0x10 else 0)
    return 0


def gapless_info(path: Path) -> dict[str, int]:
    """Parse the Xing/Info + LAME extension of the first MP3 frame.

    Returns encoder `delay`, `padding` (LAME semantics) and `total_frames`
    = MPEG frames x samples-per-frame. Raises NormalizeError when absent: a
    file without the header cannot be joined gaplessly in any browser."""
    with open(path, "rb") as fh:
        b = fh.read(64 * 1024)
    i = _skip_id3(b)
    if i + 4 > len(b) or b[i] != 0xFF or (b[i + 1] & 0xE0) != 0xE0:
        raise NormalizeError("no MPEG frame sync after ID3")
    h = struct.unpack(">I", b[i:i + 4])[0]
    mpeg1 = ((h >> 19) & 3) == 3
    mono = ((h >> 6) & 3) == 3
    side = (17 if mono else 32) if mpeg1 else (9 if mono else 17)
    x = i + 4 + side
    if b[x:x + 4] not in (b"Xing", b"Info"):
        raise NormalizeError("no Xing/Info header")
    flags = struct.unpack(">I", b[x + 4:x + 8])[0]
    o = x + 8
    frames = None
    if flags & 1:
        frames = struct.unpack(">I", b[o:o + 4])[0]
        o += 4
    if flags & 2:
        o += 4
    if flags & 4:
        o += 100
    if flags & 8:
        o += 4
    if frames is None:
        raise NormalizeError("Xing header has no frame count")
    # LAME extension: 9-byte encoder string, then 12 bytes, then delay/padding
    d = b[o + 21:o + 24]
    if len(d) != 3:
        raise NormalizeError("truncated LAME extension")
    delay = (d[0] << 4) | (d[1] >> 4)
    padding = ((d[1] & 0x0F) << 8) | d[2]
    spf = 1152 if mpeg1 else 576
    return {"delay": delay, "padding": padding, "total_frames": frames * spf}


def verify_loudness(path: Path, cfg: Config, tol: float = 0.6) -> bool:
    """Post-check integrated loudness within tolerance of target LUFS."""
    try:
        dat = measure(path, cfg)
        out = float(dat.get("output_i", "-99"))
        return abs(out - cfg.audio.lufs) <= tol
    except Exception:
        return False
