"""视频来源解析：沙盒路径、URL 校验、消息回查与会话归属。"""

import asyncio

import pytest

from nekro_plugin_video.models import MessagePayload, Segment, VideoError
from nekro_plugin_video.plugin import VideoConfig
from nekro_plugin_video.sources import (
    acquire_video, scan_segments, source_from_text, verify_channel,
)


def run(coro):
    return asyncio.run(coro)


class TestSourceFromText:
    def test_http_url(self):
        src = source_from_text("看这个 https://example.com/a.mp4 吧")
        assert src and src.url == "https://example.com/a.mp4"

    def test_bv_code(self):
        src = source_from_text("BV1xx411c7mD 这个视频")
        assert src and "BV1xx411c7mD" in src.url

    def test_no_match(self):
        assert source_from_text("没有链接") is None

    def test_video_links_only_skips_plain_url(self):
        assert source_from_text("https://example.com/page", video_links_only=True) is None
        assert source_from_text("https://b23.tv/abc", video_links_only=True) is not None


class TestScanSegments:
    def test_video_segment(self):
        segs = [Segment(type="video", data={"url": "https://v.example.com/x.mp4", "file_id": "fid1"})]
        src = scan_segments(segs, [])
        assert src and src.url == "https://v.example.com/x.mp4" and src.file_id == "fid1"

    def test_file_segment_video_ext(self):
        segs = [Segment(type="file", data={"name": "clip.mp4", "file_id": "f2"})]
        src = scan_segments(segs, [])
        assert src and src.file_id == "f2"

    def test_non_video_file_ignored(self):
        segs = [Segment(type="file", data={"name": "doc.pdf", "file_id": "f3"})]
        assert scan_segments(segs, []) is None

    def test_text_url(self):
        segs = [Segment(type="text", data={"text": "https://cdn.example.com/v.mp4"})]
        src = scan_segments(segs, [])
        assert src and src.url.endswith("v.mp4")


class TestChannelVerify:
    def test_group_match(self, ctx):
        payload = MessagePayload(group_id=123456, message_type="group")
        verify_channel(payload, ctx)

    def test_group_mismatch_rejected(self, ctx):
        payload = MessagePayload(group_id=999, message_type="group")
        with pytest.raises(VideoError, match="当前会话"):
            verify_channel(payload, ctx)

    def test_private_match(self, tmp_path):
        from conftest import FakeCtx

        c = FakeCtx(tmp_path, chat_key="onebot_v11-private_777",
                    channel_id="private_777", channel_type="private")
        payload = MessagePayload(group_id=None, message_type="private", user_id=777)
        verify_channel(payload, c)

    def test_unknown_channel_rejected(self, tmp_path):
        from conftest import FakeCtx

        c = FakeCtx(tmp_path, channel_id="", channel_type="")
        with pytest.raises(VideoError):
            verify_channel(MessagePayload(group_id=1, message_type="group"), c)


class TestAcquireVideo:
    def test_xor_required(self, ctx):
        cfg = VideoConfig()
        with pytest.raises(VideoError, match="只能提供一个"):
            run(acquire_video(ctx, "a", "1", ctx.fs.shared_path / "o.bin", cfg))
        with pytest.raises(VideoError):
            run(acquire_video(ctx, "", "", ctx.fs.shared_path / "o.bin", cfg))

    def test_sandbox_path_copy(self, ctx, tmp_path):
        src = ctx.fs.upload_path / "v.mp4"
        src.write_bytes(b"fake-video-data")
        dest = ctx.fs.shared_path / "out.bin"
        run(acquire_video(ctx, "/app/uploads/v.mp4", "", dest, VideoConfig()))
        assert dest.read_bytes() == b"fake-video-data"

    def test_host_path_rejected(self, ctx):
        with pytest.raises(VideoError, match="宿主机路径"):
            run(acquire_video(ctx, "/etc/passwd", "", ctx.fs.shared_path / "o.bin", VideoConfig()))

    def test_size_limit(self, ctx):
        big = ctx.fs.upload_path / "big.mp4"
        big.write_bytes(b"x" * (2 * 1024 * 1024))
        cfg = VideoConfig.model_construct(MAX_SIZE_MB=1)
        with pytest.raises(VideoError, match="大小上限"):
            run(acquire_video(ctx, "/app/uploads/big.mp4", "", ctx.fs.shared_path / "o.bin", cfg))

    def test_message_id_requires_onebot(self, tmp_path):
        from conftest import FakeCtx

        c = FakeCtx(tmp_path, adapter_key="sse", channel_id="c1", channel_type="group")
        with pytest.raises(VideoError, match="OneBot"):
            run(acquire_video(c, "", "42", c.fs.shared_path / "o.bin", VideoConfig()))

    def test_message_id_invalid(self, ctx):
        with pytest.raises(VideoError, match="消息 ID"):
            run(acquire_video(ctx, "", "not-a-number", ctx.fs.shared_path / "o.bin", VideoConfig()))
