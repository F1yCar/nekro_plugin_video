from typing import Literal

from nekro_agent.api.plugin import ConfigBase, NekroPlugin
from pydantic import Field

plugin = NekroPlugin(
    name="视频理解",
    module_name="nekro_plugin_video",
    description="供 Agent 自行调用的视频理解工具，支持沙盒视频、QQ 视频消息、B站及视频直链。",
    version="0.1.0",
    author="F1yCar",
    url="",
    support_adapter=["onebot_v11", "sse", "telegram", "discord"],
)

_MODEL = {"ref_model_groups": True, "model_type": "chat"}


@plugin.mount_config()
class VideoConfig(ConfigBase):
    MODEL_GROUP: str = Field(default="", title="视频分析模型组", json_schema_extra=_MODEL,
                             description="抽帧需要视觉模型；直传需要所选模型支持视频输入。"
                                         "留空时使用频道主聊天模型组（USE_MODEL_GROUP）。")
    MODEL_OVERRIDE: str = Field(default="gemini-3-flash-preview", title="模型名覆盖",
                                description="非空时覆盖所选模型组的模型名。仅适用于同一端点下切换型号，"
                                            "跨服务商使用会导致端点/密钥不匹配。")
    FALLBACK_MODEL_GROUPS: list[str] = Field(default_factory=list, title="后备模型组",
                                            description="按顺序尝试；之后还会自动兜底扫描所有支持视觉的 chat 模型组。")
    ASR_MODEL_GROUP: str = Field(default="", title="音频转写模型组", json_schema_extra=_MODEL,
                                 description="留空关闭。使用模型组的模型名称调用 OpenAI audio/transcriptions。")
    DIRECT_MODE: Literal["off", "base64", "dashscope"] = Field(default="off", title="视频直传模式")
    DIRECT_FPS: int = Field(default=2, ge=1, le=30, title="百炼视频采样 FPS")
    COMPRESS_DIRECT_VIDEO: bool = Field(default=True, title="直传超限时尝试压缩")
    FRAME_INTERVAL: int = Field(default=6, ge=1, le=120, title="目标抽帧间隔（秒）")
    MAX_FRAMES: int = Field(default=20, ge=1, le=30, title="最大抽帧数")
    MAX_SIZE_MB: int = Field(default=50, ge=1, le=512, title="视频大小上限（MB）")
    MAX_DURATION: int = Field(default=120, ge=1, le=3600, title="视频时长上限（秒）")
    MAX_FRAME_EDGE: int = Field(default=1280, ge=128, le=2048, title="帧图最大边长")
    FFMPEG: str = Field(default="ffmpeg", title="ffmpeg 路径")
    FFPROBE: str = Field(default="ffprobe", title="ffprobe 路径")
    NETWORK_TIMEOUT: int = Field(default=180, ge=5, le=600, title="下载超时（秒）")
    PROCESS_TIMEOUT: int = Field(default=90, ge=1, le=300, title="单次媒体处理超时（秒）")
    MODEL_TIMEOUT: int = Field(default=90, ge=5, le=600, title="单次模型调用超时（秒）")
    TOTAL_TIMEOUT: int = Field(default=300, ge=10, le=1800, title="完整分析超时（秒）")
