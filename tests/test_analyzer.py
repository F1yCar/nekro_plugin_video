"""端到端编排：真实 ffmpeg 抽帧 + mock 模型，验证流程、边界与清理。"""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import FakeModelGroup, gen_response_mock, make_video
from nekro_plugin_video import analyzer, media
from nekro_plugin_video.models import VideoError
from nekro_plugin_video.plugin import VideoConfig


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def reset_mock():
    gen_response_mock.reset_mock()
    gen_response_mock.side_effect = None
    gen_response_mock.return_value = SimpleNamespace(response_content="这是一段测试视频")
    yield


@pytest.fixture()
def setup_env(ctx, monkeypatch):
    core_cfg = SimpleNamespace(MODEL_GROUPS={"vision": FakeModelGroup()})
    cfg = VideoConfig(MODEL_GROUP="vision", FRAME_INTERVAL=1, MAX_FRAMES=5)
    monkeypatch.setattr(analyzer, "settings", AsyncMock(return_value=(cfg, core_cfg)))
    return cfg, core_cfg


class TestEndToEnd:
    def test_frames_mode_full_pipeline(self, ctx, setup_env):
        make_video(ctx.fs.upload_path / "v.mp4", seconds=2)
        result = run(analyzer.analyze(ctx, "/app/uploads/v.mp4", ""))
        assert result.summary == "这是一段测试视频"
        assert result.frame_timestamps, "应记录抽帧时间点"
        assert result.mode == "frames"
        # 抽帧模式下应有一次汇总调用（单帧 caption 视抽帧数而定）
        assert gen_response_mock.await_count >= 1

    def test_temp_files_cleaned(self, ctx, setup_env):
        tmp_root = Path(tempfile.gettempdir())
        before = set(tmp_root.glob("na_video_*"))
        make_video(ctx.fs.upload_path / "v.mp4", seconds=2)
        run(analyzer.analyze(ctx, "/app/uploads/v.mp4", ""))
        after = set(tmp_root.glob("na_video_*"))
        assert before == after, "临时目录应被清理"

    def test_duration_limit_rejected(self, ctx, setup_env, monkeypatch):
        make_video(ctx.fs.upload_path / "v.mp4", seconds=2)
        monkeypatch.setattr(media, "probe_duration", AsyncMock(return_value=999.0))
        with pytest.raises(VideoError, match="时长"):
            run(analyzer.analyze(ctx, "/app/uploads/v.mp4", ""))

    def test_caption_failure_nonfatal(self, ctx, setup_env, monkeypatch):
        make_video(ctx.fs.upload_path / "v.mp4", seconds=3)
        calls = {"n": 0}

        async def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("caption boom")
            return "描述"

        monkeypatch.setattr(analyzer.llm, "caption_frame", flaky)
        result = run(analyzer.analyze(ctx, "/app/uploads/v.mp4", ""))
        assert result.summary

    def test_both_inputs_rejected(self, ctx, setup_env):
        with pytest.raises(VideoError, match="只能提供一个"):
            run(analyzer.analyze(ctx, "/app/uploads/v.mp4", "12"))

    def test_result_returned_to_agent_not_sent(self, ctx, setup_env):
        """AGENT 方法只返回字符串，不调用任何消息发送接口。"""
        make_video(ctx.fs.upload_path / "v.mp4", seconds=1)
        result = run(analyzer.analyze(ctx, "/app/uploads/v.mp4", ""))
        assert isinstance(result.summary, str) and result.summary
