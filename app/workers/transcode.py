"""ffmpeg → HLS (AES-128) pipeline. Pure file-system work; S3/DB handling is in media_jobs.py."""

import asyncio
import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

PLAYLIST = "index.m3u8"
SEGMENT_PATTERN = "seg_%05d.ts"
# Relative URI written into the playlist; resolves to /media/hls/<token>/key in the player.
KEY_URI = "key"


class TranscodeError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class HlsResult:
    out_dir: Path
    aes_key: bytes
    duration_seconds: int | None


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def ffprobe_bin(ffmpeg_bin: str) -> str:
    head, tail = os.path.split(ffmpeg_bin)
    return os.path.join(head, tail.replace("ffmpeg", "ffprobe")) if "ffmpeg" in tail else "ffprobe"


def build_ffmpeg_cmd(
    ffmpeg_bin: str, src: Path, out_dir: Path, key_info: Path, *, segment_seconds: int, max_height: int = 720
) -> list[str]:
    return [
        ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-profile:v", "main", "-pix_fmt", "yuv420p",
        "-vf", f"scale=-2:'min({max_height},ih)'",
        "-force_key_frames", f"expr:gte(t,n_forced*{segment_seconds})",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-f", "hls", "-hls_time", str(segment_seconds), "-hls_playlist_type", "vod",
        "-hls_key_info_file", str(key_info),
        "-hls_segment_filename", str(out_dir / SEGMENT_PATTERN),
        str(out_dir / PLAYLIST),
    ]  # fmt: skip


async def _run(cmd: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise TranscodeError("ffmpeg timed out") from None
    return proc.returncode or 0, out, err


async def probe_duration(ffmpeg_bin: str, src: Path) -> int | None:
    cmd = [ffprobe_bin(ffmpeg_bin), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)]
    try:
        code, out, _ = await _run(cmd, 120)
    except (OSError, TranscodeError):
        return None
    try:
        return round(float(out.decode().strip())) if code == 0 else None
    except ValueError:
        return None


async def transcode_to_hls(
    ffmpeg_bin: str, src: Path, work_dir: Path, *, segment_seconds: int = 6, timeout: float = 4 * 3600
) -> HlsResult:
    """Transcode `src` into `work_dir/hls/` as AES-128 encrypted HLS. The key never goes into the output dir."""
    out_dir = work_dir / "hls"
    out_dir.mkdir(parents=True, exist_ok=True)
    aes_key = secrets.token_bytes(16)
    key_file = work_dir / "enc.key"
    key_file.write_bytes(aes_key)
    key_info = work_dir / "enc.keyinfo"
    key_info.write_text(f"{KEY_URI}\n{key_file}\n{secrets.token_hex(16)}\n")

    duration = await probe_duration(ffmpeg_bin, src)
    cmd = build_ffmpeg_cmd(ffmpeg_bin, src, out_dir, key_info, segment_seconds=segment_seconds)
    code, _, err = await _run(cmd, timeout)
    key_file.unlink(missing_ok=True)
    if code != 0 or not (out_dir / PLAYLIST).exists():
        # ffmpeg stderr may contain paths only; keep it short.
        raise TranscodeError((err.decode(errors="replace").strip().splitlines() or ["ffmpeg failed"])[-1][:300])
    return HlsResult(out_dir, aes_key, duration)
