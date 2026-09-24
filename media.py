import asyncio
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from .models import VideoError
from .plugin import VideoConfig

_INPUT_LIMITS = ["-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,avi,flv,mpegts,mpeg,mpegvideo,ogg,gif"]


class ProbeStream(BaseModel):
    codec_type: str = ""
    duration: str | float | None = None
    nb_frames: str | int | None = None
    avg_frame_rate: str = "0/1"


class ProbePayload(BaseModel):
    format: dict[str, str | float | int] = Field(default_factory=dict)
    streams: list[ProbeStream] = Field(default_factory=list)


@dataclass(frozen=True)
class Frame:
    path: Path
    timestamp: float


async def run_process(command: list[str], timeout: int, stdin: bytes | None = None) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(stdin), timeout)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    if process.returncode != 0:
        raise VideoError("媒体处理程序执行失败，请检查文件格式与工具依赖")
    return stdout


def executable(configured: str, *, required: bool) -> str | None:
    found = shutil.which(configured)
    if found:
        return found
    if Path(configured).name == "ffmpeg":
        try:
            import imageio_ffmpeg
        except ImportError:
            try:
                from nekro_agent.api.plugin import dynamic_import_pkg

                return dynamic_import_pkg("imageio-ffmpeg", "imageio_ffmpeg").get_ffmpeg_exe()
            except Exception:
                pass
        else:
            return imageio_ffmpeg.get_ffmpeg_exe()
    if required:
        raise VideoError("缺少 ffmpeg，请安装到 NA 运行环境并配置 FFMPEG")
    return None


def positive_number(value: object) -> float | None:
    try:
        number = float(str(value))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


async def _duration_via_ffmpeg(video: Path, cfg: VideoConfig) -> float | None:
    """无 ffprobe 时用 `ffmpeg -i` 的 stderr 解析时长（该调用必然以非零退出）。"""
    ffmpeg = executable(cfg.FFMPEG, required=True)
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-nostdin", "-hide_banner", *_INPUT_LIMITS, "-i", str(video),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), cfg.PROCESS_TIMEOUT)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    text = stderr.decode(errors="replace")
    if " Video:" not in text:
        raise VideoError("文件中没有视频流")
    match = _DURATION_RE.search(text)
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


async def probe_duration(video: Path, cfg: VideoConfig) -> float | None:
    probe = executable(cfg.FFPROBE, required=False)
    if probe is None:
        return await _duration_via_ffmpeg(video, cfg)
    raw = await run_process([
        probe, "-v", "error", *_INPUT_LIMITS, "-show_entries",
        "format=duration:stream=codec_type,duration,nb_frames,avg_frame_rate", "-of", "json", str(video),
    ], cfg.PROCESS_TIMEOUT)
    data = ProbePayload.model_validate(json.loads(raw))
    videos = [stream for stream in data.streams if stream.codec_type == "video"]
    if not videos:
        raise VideoError("文件中没有视频流")
    candidates = [positive_number(data.format.get("duration"))]
    for stream in videos:
        candidates.append(positive_number(stream.duration))
        numerator, _, denominator = stream.avg_frame_rate.partition("/")
        top, bottom = positive_number(numerator), positive_number(denominator)
        count = positive_number(stream.nb_frames)
        if top and bottom and count:
            candidates.append(count * bottom / top)
    return max((value for value in candidates if value is not None), default=None)


def frame_times(duration: float, cfg: VideoConfig) -> list[float]:
    count = min(cfg.MAX_FRAMES, max(1, math.ceil(duration / cfg.FRAME_INTERVAL)))
    return [(index + 1) / (count + 1) * duration for index in range(count)]


async def sample_frames(video: Path, directory: Path, duration: float | None, cfg: VideoConfig) -> list[Frame]:
    ffmpeg = executable(cfg.FFMPEG, required=True)
    if ffmpeg is None:
        raise VideoError("缺少 ffmpeg")
    scale = f"scale={cfg.MAX_FRAME_EDGE}:{cfg.MAX_FRAME_EDGE}:force_original_aspect_ratio=decrease"
    common = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-threads", "2"]
    frames = []
    if duration is not None:
        for index, timestamp in enumerate(frame_times(duration, cfg)):
            output = directory / f"frame_{index:03d}.jpg"
            await run_process([
                *common, *_INPUT_LIMITS, "-ss", f"{timestamp:.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", scale, "-q:v", "2", "-threads", "2", str(output),
            ], cfg.PROCESS_TIMEOUT)
            if output.is_file() and output.stat().st_size:
                frames.append(Frame(output, timestamp))
    else:
        count = min(cfg.MAX_FRAMES, math.ceil(cfg.MAX_DURATION / cfg.FRAME_INTERVAL))
        await run_process([
            *common, *_INPUT_LIMITS, "-i", str(video), "-t", str(cfg.MAX_DURATION),
            "-vf", f"fps=1/{cfg.FRAME_INTERVAL},{scale}", "-frames:v", str(count),
            "-q:v", "2", "-threads", "2", str(directory / "frame_%03d.jpg"),
        ], cfg.PROCESS_TIMEOUT)
        frames = [Frame(path, index * cfg.FRAME_INTERVAL) for index, path in enumerate(sorted(directory.glob("frame_*.jpg")))]
    if not frames:
        raise VideoError("未能生成视频帧，文件可能损坏或不包含视频")
    return frames


async def extract_audio(video: Path, dest: Path, cfg: VideoConfig) -> None:
    ffmpeg = executable(cfg.FFMPEG, required=True)
    if ffmpeg is None:
        raise VideoError("缺少 ffmpeg")
    await run_process([
        ffmpeg, "-nostdin", "-v", "error", *_INPUT_LIMITS, "-i", str(video),
        "-t", str(cfg.MAX_DURATION), "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(dest),
    ], cfg.PROCESS_TIMEOUT)


async def compress_video(video: Path, dest: Path, duration: float, limit: int, cfg: VideoConfig) -> Path:
    if video.stat().st_size <= limit:
        return video
    if not cfg.COMPRESS_DIRECT_VIDEO:
        raise VideoError("视频超过直传大小限制，请启用压缩或改用抽帧模式")
    ffmpeg = executable(cfg.FFMPEG, required=True)
    if ffmpeg is None:
        raise VideoError("缺少 ffmpeg")
    bitrate = int(limit * 8 * 0.8 / duration)
    if bitrate < 50000:
        raise VideoError("视频无法在合理码率下压缩到直传上限")
    await run_process([
        ffmpeg, "-nostdin", "-v", "error", *_INPUT_LIMITS, "-i", str(video),
        "-c:v", "libx264", "-b:v", str(bitrate), "-threads", "2",
        "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(dest),
    ], cfg.PROCESS_TIMEOUT)
    if not dest.is_file() or not 0 < dest.stat().st_size <= limit:
        raise VideoError("压缩后仍超过直传上限，请改用抽帧模式")
    return dest
