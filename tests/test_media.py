"""媒体处理：ffprobe 时长解析、ffmpeg 抽帧/音频提取、超时与清理。"""

import asyncio
import json
from pathlib import Path

import pytest

from conftest import make_video
from nekro_plugin_video import media
from nekro_plugin_video.models import VideoError
from nekro_plugin_video.plugin import VideoConfig


def run(coro):
    return asyncio.run(coro)


def cfg(**kw) -> VideoConfig:
    return VideoConfig(**kw)


class TestProbeDuration:
    def test_parse_format_duration(self, tmp_path, monkeypatch):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        payload = {"format": {"duration": "12.5"}, "streams": [{"codec_type": "video"}]}
        monkeypatch.setattr(media, "executable", lambda name, *, required: "/bin/true")
        monkeypatch.setattr(media, "run_process", lambda *a, **k: _fake(json.dumps(payload).encode()))
        assert run(media.probe_duration(video, cfg())) == 12.5

    def test_no_video_stream_rejected(self, tmp_path, monkeypatch):
        video = tmp_path / "a.mp4"
        video.write_bytes(b"x")
        payload = {"format": {"duration": "3"}, "streams": [{"codec_type": "audio", "duration": "3"}]}
        monkeypatch.setattr(media, "executable", lambda name, *, required: "/bin/true")
        monkeypatch.setattr(media, "run_process", lambda *a, **k: _fake(json.dumps(payload).encode()))
        with pytest.raises(VideoError, match="视频流"):
            run(media.probe_duration(video, cfg()))

    def test_missing_ffprobe_falls_back_to_ffmpeg(self, tmp_path, monkeypatch):
        """无 ffprobe 时改用 ffmpeg -i stderr 解析时长。"""
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        def fake_exec(name, *, required):
            return None if Path(name).name == "ffprobe" else "/bin/ffmpeg"

        monkeypatch.setattr(media, "executable", fake_exec)
        monkeypatch.setattr(media, "_duration_via_ffmpeg", _fake_sync(45.0))
        assert run(media.probe_duration(video, cfg())) == 45.0

    def test_duration_via_ffmpeg_stderr(self, tmp_path, monkeypatch):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")

        def fake_exec(name, *, required):
            return None if Path(name).name == "ffprobe" else "/bin/ffmpeg"

        monkeypatch.setattr(media, "executable", fake_exec)
        stderr = b"Input #0, mov, from 'v.mp4':\n  Duration: 00:01:30.50, start: 0.0\n  Stream #0:0: Video: h264\n"
        monkeypatch.setattr(media.asyncio, "create_subprocess_exec", _fake_proc(stderr))
        assert run(media.probe_duration(video, cfg())) == pytest.approx(90.5)


async def _fake(value):
    return value


def _fake_sync(value):
    async def _inner(*a, **k):
        return value
    return _inner


def _fake_proc(stderr: bytes):
    """模拟 asyncio.create_subprocess_exec，返回带 stderr 的假进程。"""

    class _Proc:
        returncode = 0

        async def communicate(self, input=None):
            return b"", stderr

        def kill(self):
            pass

        async def wait(self):
            return 0

    async def _spawn(*a, **k):
        return _Proc()

    return _spawn


class TestFrameTimes:
    def test_spacing(self):
        times = media.frame_times(60.0, cfg(FRAME_INTERVAL=6, MAX_FRAMES=20))
        assert len(times) == 10
        assert all(0 < t < 60 for t in times)

    def test_max_frames_cap(self):
        times = media.frame_times(3600.0, cfg(FRAME_INTERVAL=1, MAX_FRAMES=20))
        assert len(times) == 20


class TestRealFfmpeg:
    def test_sample_frames_with_duration(self, tmp_path):
        video = make_video(tmp_path / "v.mp4", seconds=2)
        out = tmp_path / "frames"
        out.mkdir()
        frames = run(media.sample_frames(video, out, 2.0, cfg(FRAME_INTERVAL=1, MAX_FRAMES=5)))
        assert frames, "应至少抽出一帧"
        for f in frames:
            assert f.path.is_file() and f.path.stat().st_size > 0

    def test_sample_frames_without_duration(self, tmp_path):
        video = make_video(tmp_path / "v.mp4", seconds=2)
        out = tmp_path / "frames"
        out.mkdir()
        frames = run(media.sample_frames(video, out, None, cfg(FRAME_INTERVAL=1, MAX_FRAMES=5)))
        assert frames

    def test_extract_audio(self, tmp_path):
        video = make_video(tmp_path / "v.mp4", seconds=2, with_audio=True)
        wav = tmp_path / "a.wav"
        run(media.extract_audio(video, wav, cfg()))
        assert wav.is_file() and wav.stat().st_size > 100

    def test_corrupt_file_raises(self, tmp_path):
        bad = tmp_path / "bad.mp4"
        bad.write_bytes(b"not a video")
        out = tmp_path / "frames"
        out.mkdir()
        with pytest.raises((VideoError, Exception)):
            run(media.sample_frames(bad, out, 2.0, cfg()))


class TestRunProcess:
    def test_timeout_kills(self):
        with pytest.raises(Exception):
            run(media.run_process(["sleep", "5"], 1))

    def test_nonzero_exit_raises(self):
        with pytest.raises(VideoError):
            run(media.run_process(["false"], 5))
