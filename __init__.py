"""NA 原生视频理解插件：Agent 自调用，无指令/关键词触发。

参考 astrbot_zssm_explain 的视频分析链路（抽帧 -> 逐帧描述 -> 汇总），
仅保留视频能力，不包含文本解释、网页摘要、知乎、PDF 等其他功能。
"""

from nekro_agent.api.plugin import SandboxMethodType
from nekro_agent.api.schemas import AgentCtx

from .analyzer import analyze
from .plugin import VideoConfig, plugin

__all__ = ["VideoConfig", "plugin"]


@plugin.mount_sandbox_method(
    SandboxMethodType.AGENT,
    name="视频分析",
    description=(
        "分析一段视频的主要内容并返回中文总结。"
        "当用户发来视频、转发包含视频的合并消息、给出 B站视频链接或视频直链、"
        "或要求解释某个视频时调用。结果仅供你组织回复，插件不会直接发送消息。"
    ),
)
async def analyze_video(_ctx: AgentCtx, source: str = "", message_id: str = "") -> str:
    """分析视频内容并返回中文总结

    Args:
        source (str): 视频来源，三选一：沙盒路径（/app/uploads/ 或 /app/shared/ 下的视频文件）、
            HTTP/HTTPS 视频直链、B站视频链接或 BV/av 号。与 message_id 二选一。
        message_id (str): 当前会话中包含视频的消息 ID（仅 OneBot v11 频道可用），
            用于分析用户引用/转发消息中的视频。与 source 二选一。

    Returns:
        str: 视频分析结果（含时长、抽帧数与中文总结），供你整合后回复用户。
    """
    result = await analyze(_ctx, source.strip(), message_id.strip())
    parts = [f"视频分析结果（{result.mode} 模式）：{result.summary}"]
    meta = []
    if result.duration_seconds is not None:
        meta.append(f"时长 {int(result.duration_seconds)}s")
    if result.frame_timestamps:
        meta.append(f"抽取 {len(result.frame_timestamps)} 帧")
    if result.audio_transcribed:
        meta.append("含音频转写")
    if meta:
        parts.append("处理信息：" + "，".join(meta))
    if result.warnings:
        parts.append("注意：" + "；".join(result.warnings))
    return "\n".join(parts)


@plugin.mount_cleanup_method()
async def clean_up() -> None:
    """无持久资源；临时目录均随请求清理。"""
