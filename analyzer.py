"""视频分析编排：获取 -> 边界校验 -> 抽帧/直传 -> 逐帧描述 -> 汇总。"""

import asyncio
import tempfile
from pathlib import Path

from nekro_agent.api import core
from nekro_agent.core.config import CoreConfig
from nekro_agent.schemas.agent_ctx import AgentCtx

from . import llm, media
from .host import settings
from .models import VideoError, VideoResult
from .plugin import VideoConfig
from .sources import acquire_video


def _is_gif(video: Path) -> bool:
    try:
        return video.read_bytes()[:6] in {b"GIF87a", b"GIF89a"}
    except OSError:
        return False


async def _run(ctx: AgentCtx, source: str, message_id: str, cfg: VideoConfig, core_cfg: CoreConfig) -> VideoResult:
    warnings: list[str] = []
    with tempfile.TemporaryDirectory(prefix="na_video_") as tmp:
        work = Path(tmp)
        video = work / "input.mp4"
        await acquire_video(ctx, source, message_id, video, cfg)

        duration = await media.probe_duration(video, cfg)
        if duration is None:
            warnings.append("无法探测视频时长，按配置上限截断抽帧")
        elif duration > cfg.MAX_DURATION:
            raise VideoError(f"视频时长 {int(duration)}s 超过上限 {cfg.MAX_DURATION}s")

        if _is_gif(video):
            duration = duration or 1.0

        if cfg.DIRECT_MODE != "off":
            summary = await llm.analyze_direct(ctx, cfg, core_cfg, video, duration)
            return VideoResult(
                summary=summary, duration_seconds=duration, mode=f"direct_{cfg.DIRECT_MODE}", warnings=warnings,
            )

        frames_dir = work / "frames"
        frames_dir.mkdir()
        if _is_gif(video):
            frames = await media.sample_frames(video, frames_dir, 1.0, cfg)
            frames = frames[:1]
        else:
            frames = await media.sample_frames(video, frames_dir, duration, cfg)

        asr_text = ""
        if cfg.ASR_MODEL_GROUP.strip() and not _is_gif(video):
            wav = work / "audio.wav"
            try:
                await media.extract_audio(video, wav, cfg)
                if wav.is_file() and wav.stat().st_size:
                    asr_text = await llm.transcribe(ctx, cfg, core_cfg, wav)
            except Exception as e:
                core.logger.warning(f"视频音频转写失败，跳过: {e}")
                warnings.append("音频转写失败，仅依据画面总结")

        captions: list[str] = []
        for index, frame in enumerate(frames[:-1]):
            try:
                captions.append(await llm.caption_frame(ctx, cfg, core_cfg, frame.path, frame.timestamp))
            except Exception as e:
                core.logger.warning(f"第 {index + 1} 帧描述失败: {e}")
                captions.append("未识别")

        summary = await llm.summarize(
            ctx, cfg, core_cfg,
            name=Path(source).name if source else "会话视频",
            duration=duration, frame_count=len(frames), captions=captions,
            asr_text=asr_text, last_frame=frames[-1].path,
        )
        return VideoResult(
            summary=summary,
            duration_seconds=duration,
            frame_timestamps=[f.timestamp for f in frames],
            audio_transcribed=bool(asr_text),
            warnings=warnings,
        )


async def analyze(ctx: AgentCtx, source: str, message_id: str) -> VideoResult:
    if bool(source.strip()) == bool(message_id.strip()):
        raise VideoError("source 和 message_id 必须且只能提供一个")
    cfg, core_cfg = await settings(ctx)
    return await asyncio.wait_for(_run(ctx, source, message_id, cfg, core_cfg), cfg.TOTAL_TIMEOUT)
