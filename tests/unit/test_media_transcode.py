"""Real ffmpeg run on a 4-second synthetic clip (skipped when ffmpeg is missing)."""

import asyncio
import shutil
from pathlib import Path

import pytest

from app.workers.transcode import KEY_URI, PLAYLIST, build_ffmpeg_cmd, sha256_file, transcode_to_hls

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_cmd_uses_aes_keyinfo_and_vod_playlist(tmp_path: Path):
    cmd = build_ffmpeg_cmd("ffmpeg", tmp_path / "in.mp4", tmp_path / "out", tmp_path / "k", segment_seconds=6)
    assert cmd[cmd.index("-hls_key_info_file") + 1] == str(tmp_path / "k")
    assert cmd[cmd.index("-hls_playlist_type") + 1] == "vod"
    assert "-nostdin" in cmd


async def test_transcode_produces_encrypted_hls(tmp_path: Path):
    src = tmp_path / "clip.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x240:rate=25",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440",
        "-t",
        "4",
        "-pix_fmt",
        "yuv420p",
        str(src),
    )
    assert await proc.wait() == 0

    result = await transcode_to_hls("ffmpeg", src, tmp_path / "work", segment_seconds=2)

    playlist = (result.out_dir / PLAYLIST).read_text()
    assert "#EXT-X-ENDLIST" in playlist
    assert f'#EXT-X-KEY:METHOD=AES-128,URI="{KEY_URI}"' in playlist
    segments = sorted(result.out_dir.glob("seg_*.ts"))
    assert len(segments) >= 2
    # Encrypted: a clear MPEG-TS segment starts with the 0x47 sync byte every 188 bytes.
    data = segments[0].read_bytes()
    assert not (data[0] == 0x47 and data[188] == 0x47)
    assert len(result.aes_key) == 16
    assert result.duration_seconds == 4
    # The key never sits next to the published files.
    assert not any(p.suffix == ".key" for p in result.out_dir.iterdir())
    assert len(sha256_file(src)) == 64
