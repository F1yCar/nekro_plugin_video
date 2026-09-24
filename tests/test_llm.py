"""模型层：模型组校验、回退顺序、消息构造、ASR 配置。"""

import asyncio
from types import SimpleNamespace

import pytest

from conftest import FakeModelGroup, gen_response_mock
from nekro_plugin_video import llm
from nekro_plugin_video.models import VideoError
from nekro_plugin_video.plugin import VideoConfig


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def reset_mock():
    gen_response_mock.reset_mock()
    gen_response_mock.side_effect = None
    gen_response_mock.return_value = SimpleNamespace(response_content="模型回复")
    yield


def cfg(**kw) -> VideoConfig:
    return VideoConfig(**kw)


class TestGroupValidation:
    def test_missing_group_config(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={"draw": FakeModelGroup(MODEL_TYPE="draw")})
        with pytest.raises(VideoError, match="MODEL_GROUP|视觉"):
            run(llm.chat(ctx, cfg(), core, [{"role": "user", "content": "x"}]))

    def test_group_not_found(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={"draw": FakeModelGroup(MODEL_TYPE="draw")})
        with pytest.raises(VideoError, match="不存在"):
            run(llm.chat(ctx, cfg(MODEL_GROUP="nope"), core, [{"role": "user", "content": "x"}]))

    def test_empty_group_falls_back_to_use_model_group(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={"main": FakeModelGroup()}, USE_MODEL_GROUP="main")
        result = run(llm.chat(ctx, cfg(MODEL_OVERRIDE=""), core, [{"role": "user", "content": "x"}]))
        assert result == "模型回复"
        assert gen_response_mock.await_args.kwargs["model"] == "test-vision-model"

    def test_default_override_is_applied(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={"vision": FakeModelGroup()})
        run(llm.chat(ctx, cfg(MODEL_GROUP="vision"), core, [{"role": "user", "content": "x"}]))
        assert gen_response_mock.await_args.kwargs["model"] == "gemini-3-flash-preview"

    def test_auto_scan_picks_vision_group(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={
            "text": FakeModelGroup(ENABLE_VISION=False),
            "vision-auto": FakeModelGroup(),
            "draw": FakeModelGroup(MODEL_TYPE="draw"),
        })
        result = run(llm.chat(ctx, cfg(), core, [{"role": "user", "content": "x"}]))
        assert result == "模型回复"

    def test_model_override_replaces_chat_model(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={"vision": FakeModelGroup()})
        run(llm.chat(ctx, cfg(MODEL_GROUP="vision", MODEL_OVERRIDE="other-model"), core,
                     [{"role": "user", "content": "x"}]))
        assert gen_response_mock.await_args.kwargs["model"] == "other-model"

    def test_non_chat_group_rejected(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={"draw": FakeModelGroup(MODEL_TYPE="draw")})
        with pytest.raises(VideoError, match="chat"):
            run(llm.chat(ctx, cfg(MODEL_GROUP="draw"), core, [{"role": "user", "content": "x"}]))

    def test_vision_required_for_frames(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={"text": FakeModelGroup(ENABLE_VISION=False)})
        with pytest.raises(VideoError, match="视觉"):
            run(llm.chat(ctx, cfg(MODEL_GROUP="text"), core, [{"role": "user", "content": "x"}]))


class TestFallback:
    def test_fallback_on_failure(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={
            "bad": FakeModelGroup(),
            "good": FakeModelGroup(),
        })
        gen_response_mock.side_effect = [Exception("boom"), SimpleNamespace(response_content="ok")]
        result = run(llm.chat(ctx, cfg(MODEL_GROUP="bad", FALLBACK_MODEL_GROUPS=["good"]), core,
                              [{"role": "user", "content": "x"}]))
        assert result == "ok"
        assert gen_response_mock.await_count == 2

    def test_all_fail_aggregates(self, ctx):
        core = SimpleNamespace(MODEL_GROUPS={"a": FakeModelGroup(), "b": FakeModelGroup()})
        gen_response_mock.side_effect = Exception("down")
        with pytest.raises(VideoError, match="a:.*down.*b:.*down|调用失败"):
            run(llm.chat(ctx, cfg(MODEL_GROUP="a", FALLBACK_MODEL_GROUPS=["b"]), core,
                         [{"role": "user", "content": "x"}]))


class TestMessages:
    def test_caption_message_structure(self, ctx, core_cfg, tmp_path):
        frame = tmp_path / "f.jpg"
        frame.write_bytes(b"\xff\xd8\xff")
        run(llm.caption_frame(ctx, cfg(MODEL_GROUP="vision"), core_cfg, frame, 3.0))
        messages = gen_response_mock.await_args.kwargs["messages"]
        content = messages[0]["content"]
        kinds = [c["type"] for c in content]
        assert "image_url" in kinds and "text" in kinds
        img = next(c for c in content if c["type"] == "image_url")
        assert img["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_summarize_prompt_contains_captions_and_asr(self, ctx, core_cfg, tmp_path):
        frame = tmp_path / "f.jpg"
        frame.write_bytes(b"\xff\xd8\xff")
        run(llm.summarize(ctx, cfg(MODEL_GROUP="vision"), core_cfg,
                          name="v.mp4", duration=12.0, frame_count=3,
                          captions=["开头是猫", "中间是狗"], asr_text="有人说话", last_frame=frame))
        messages = gen_response_mock.await_args.kwargs["messages"]
        text = "\n".join(c["text"] for c in messages[0]["content"] if c["type"] == "text")
        assert "开头是猫" in text and "有人说话" in text and "时长: 12s" in text


class TestTranscribe:
    def test_disabled_returns_empty(self, ctx, core_cfg, tmp_path):
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF")
        assert run(llm.transcribe(ctx, cfg(), core_cfg, wav)) == ""

    def test_missing_group_raises(self, ctx, core_cfg, tmp_path):
        wav = tmp_path / "a.wav"
        wav.write_bytes(b"RIFF")
        with pytest.raises(VideoError, match="不存在"):
            run(llm.transcribe(ctx, cfg(ASR_MODEL_GROUP="nope"), core_cfg, wav))
