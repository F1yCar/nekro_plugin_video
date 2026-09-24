"""模型调用层：模型组解析、多模态对话、可选 ASR 与视频直传。"""

import asyncio
import base64
from pathlib import Path
from typing import Any

from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.schemas.agent_ctx import AgentCtx
from nekro_agent.services.agent.creator import ContentSegment, OpenAIChatMessage
from nekro_agent.services.agent.openai import gen_openai_chat_response

from .host import usage_scope
from .models import VideoError
from .plugin import VideoConfig

# 与 astrbot_zssm_explain/prompt_utils.py 保持一致的分析口径
FRAME_CAPTION_PROMPT = (
    "请根据这张关键帧图片，用一句中文描述画面要点；少于25字。若无法判断，请回答'未识别'。禁止输出政治有关内容。"
)
FINAL_PROMPT = (
    "请根据以下关键帧描述与最后一张关键帧图片，总结整段视频的主要内容（中文，不超过100字）。"
    "仅依据已给信息，信息不足请说明'无法判断'，不要编造未出现的内容。\n"
    "{meta_block}\n关键帧描述：\n{caps_block}\n{asr_block}"
)
DIRECT_PROMPT = (
    "请观看这段视频并用中文总结主要内容，不超过100字。"
    "仅依据视频实际内容作答，信息不足请说明'无法判断'。禁止输出政治有关内容。"
)
DIRECT_BASE64_LIMIT = 10 * 1024 * 1024
DIRECT_DASHSCOPE_LIMIT = 100 * 1024 * 1024


def _group(core_cfg: CoreConfig, name: str) -> ModelConfigGroup:
    try:
        return core_cfg.MODEL_GROUPS[name]
    except KeyError as e:
        raise VideoError(f"模型组 '{name}' 不存在，请在插件配置中选择有效的模型组") from e


def _group_names(cfg: VideoConfig, core_cfg: CoreConfig) -> list[str]:
    """候选模型组顺序：显式配置 → 频道主模型组 → 显式后备 → 全部视觉组兜底。

    兜底扫描对齐原版 select_vision_provider "取首个支持图片的 provider" 的行为。
    """
    names = []
    primary = cfg.MODEL_GROUP.strip() or str(getattr(core_cfg, "USE_MODEL_GROUP", "") or "").strip()
    if primary:
        names.append(primary)
    names.extend(n.strip() for n in cfg.FALLBACK_MODEL_GROUPS if n and n.strip())
    for name, group in core_cfg.MODEL_GROUPS.items():
        if name not in names and group.MODEL_TYPE == "chat" and group.ENABLE_VISION:
            names.append(name)
    return names


def _check_group(name: str, group: ModelConfigGroup, *, need_vision: bool) -> None:
    if group.MODEL_TYPE != "chat":
        raise VideoError(f"模型组 '{name}' 不是 chat 类型")
    if need_vision and not group.ENABLE_VISION:
        raise VideoError(f"模型组 '{name}' 未开启视觉能力，无法分析视频画面")


async def _chat_once(
    ctx: AgentCtx,
    name: str,
    group: ModelConfigGroup,
    messages: list[Any],
    timeout: int,
    model_override: str = "",
) -> str:
    with usage_scope(ctx.chat_key, name, group):
        response = await asyncio.wait_for(
            gen_openai_chat_response(
                model=model_override or group.CHAT_MODEL,
                messages=messages,
                base_url=group.BASE_URL,
                api_key=group.API_KEY,
                proxy_url=group.CHAT_PROXY or None,
                max_wait_time=timeout,
            ),
            timeout + 30,
        )
    text = (response.response_content or "").strip()
    if not text:
        raise VideoError(f"模型组 '{name}' 未返回有效内容")
    return text


async def chat(
    ctx: AgentCtx,
    cfg: VideoConfig,
    core_cfg: CoreConfig,
    messages: list[Any],
    *,
    need_vision: bool = True,
) -> str:
    """按候选顺序调用模型组，全部失败时抛出聚合错误。"""
    names = _group_names(cfg, core_cfg)
    if not names:
        raise VideoError("未配置视频分析模型组 (MODEL_GROUP)，且频道主模型组不可用")
    errors = []
    for name in names:
        try:
            group = _group(core_cfg, name)
            _check_group(name, group, need_vision=need_vision)
            return await _chat_once(ctx, name, group, messages, cfg.MODEL_TIMEOUT, cfg.MODEL_OVERRIDE.strip())
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise VideoError("视频分析模型调用失败: " + "；".join(errors))


async def caption_frame(ctx: AgentCtx, cfg: VideoConfig, core_cfg: CoreConfig, frame: Path, timestamp: float) -> str:
    msg = OpenAIChatMessage.create_empty("user")
    msg.batch_add([
        ContentSegment.text_content(f"时间点约 {timestamp:.0f}s。"),
        ContentSegment.image_content_from_path(frame),
        ContentSegment.text_content(FRAME_CAPTION_PROMPT),
    ])
    return await chat(ctx, cfg, core_cfg, [msg.to_dict()])


async def summarize(
    ctx: AgentCtx,
    cfg: VideoConfig,
    core_cfg: CoreConfig,
    *,
    name: str,
    duration: float | None,
    frame_count: int,
    captions: list[str],
    asr_text: str,
    last_frame: Path,
) -> str:
    meta_items = [f"视频: {name}"]
    if duration is not None:
        meta_items.append(f"时长: {int(duration)}s")
    meta_items.append(f"关键帧: {frame_count} 张")
    caps_block = "\n".join(f"- {c}" for c in captions if c.strip())
    asr_block = f"音频转写要点: \n{asr_text.strip()}" if asr_text.strip() else ""
    prompt = FINAL_PROMPT.format(meta_block="\n".join(meta_items), caps_block=caps_block, asr_block=asr_block)
    msg = OpenAIChatMessage.create_empty("user")
    msg.batch_add([
        ContentSegment.image_content_from_path(last_frame),
        ContentSegment.text_content(prompt),
    ])
    return await chat(ctx, cfg, core_cfg, [msg.to_dict()])


async def transcribe(ctx: AgentCtx, cfg: VideoConfig, core_cfg: CoreConfig, wav: Path) -> str:
    """通过所选模型组的 OpenAI 兼容接口转写音频；未配置返回空串。"""
    name = cfg.ASR_MODEL_GROUP.strip()
    if not name:
        return ""
    group = _group(core_cfg, name)
    _check_group(name, group, need_vision=False)
    import httpx
    from openai import AsyncOpenAI

    async with httpx.AsyncClient(
        proxy=group.CHAT_PROXY or None,
        trust_env=False,
        timeout=cfg.MODEL_TIMEOUT,
    ) as http_client:
        client = AsyncOpenAI(base_url=group.BASE_URL, api_key=group.API_KEY, http_client=http_client)
        with wav.open("rb") as audio:
            result = await client.audio.transcriptions.create(model=group.CHAT_MODEL, file=audio)
    return (result.text or "").strip()


async def analyze_direct(ctx: AgentCtx, cfg: VideoConfig, core_cfg: CoreConfig, video: Path, duration: float | None) -> str:
    """视频直传模式：base64（OpenAI 兼容 video_url）或 dashscope（百炼）。"""
    if cfg.DIRECT_MODE == "base64":
        target = DIRECT_BASE64_LIMIT
        if duration is not None and video.stat().st_size > target:
            from .media import compress_video

            video = await compress_video(video, video.with_name("compressed.mp4"), duration, target, cfg)
        if video.stat().st_size > target:
            raise VideoError("视频超过直传大小限制，请改用抽帧模式")
        payload = base64.b64encode(video.read_bytes()).decode()
        message = {
            "role": "user",
            "content": [
                {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{payload}"}},
                {"type": "text", "text": DIRECT_PROMPT},
            ],
        }
        return await chat(ctx, cfg, core_cfg, [message])

    if cfg.DIRECT_MODE == "dashscope":
        if video.stat().st_size > DIRECT_DASHSCOPE_LIMIT:
            raise VideoError("视频超过百炼直传上限（100MB），请改用抽帧模式")
        try:
            import dashscope
        except ImportError as e:
            raise VideoError("百炼直传需要 dashscope 依赖，请在插件 requirements 中启用") from e
        names = _group_names(cfg, core_cfg)
        if not names:
            raise VideoError("未配置视频分析模型组 (MODEL_GROUP)，且频道主模型组不可用")
        errors = []
        for name in names:
            try:
                group = _group(core_cfg, name)
                _check_group(name, group, need_vision=False)
                def call() -> Any:
                    return dashscope.MultiModalConversation.call(
                        model=group.CHAT_MODEL,
                        messages=[{"role": "user", "content": [{"video": f"file://{video}"}, {"text": DIRECT_PROMPT}]}],
                        api_key=group.API_KEY,
                        fps=cfg.DIRECT_FPS,
                    )

                response = await asyncio.wait_for(asyncio.to_thread(call), cfg.MODEL_TIMEOUT)
                if getattr(response, "status_code", 200) != 200:
                    raise VideoError(f"百炼返回错误: {getattr(response, 'code', response.status_code)}")
                content = response.output.choices[0].message.content
                text = content[0]["text"] if isinstance(content, list) else str(content)
                if not text.strip():
                    raise VideoError("百炼未返回有效内容")
                return text.strip()
            except Exception as e:
                errors.append(f"{name}: {e}")
        raise VideoError("百炼视频直传失败: " + "；".join(errors))

    raise VideoError(f"未知的直传模式: {cfg.DIRECT_MODE}")
