"""测试环境：桩掉 nekro_agent 依赖，使插件包可在 NA 之外独立测试。"""

import base64
import subprocess
import sys
from enum import Enum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))


def module(name: str, **attrs: Any) -> ModuleType:
    result = ModuleType(name)
    result.__path__ = []  # 标记为包，支持子模块导入
    result.__dict__.update(attrs)
    sys.modules[name] = result
    return result


class SandboxMethodType(Enum):
    TOOL = "tool"
    AGENT = "agent"
    BEHAVIOR = "behavior"
    MULTIMODAL_AGENT = "multimodal_agent"


class SandboxMethod:
    def __init__(self, method_type: SandboxMethodType, name: str, description: str, func: Any) -> None:
        self.method_type = method_type
        self.name = name
        self.description = description
        self.func = func


class FakePlugin:
    def __init__(self, **metadata: Any) -> None:
        self.__dict__.update(metadata)
        self.key = f"{self.author}.{self.module_name}"
        self.is_enabled = True
        self.cfg = None
        self.sandbox_methods: list[SandboxMethod] = []
        self.cleanup_methods: list[Any] = []

    def mount_config(self):
        def register(cls):
            self.cfg = cls()
            return cls
        return register

    def get_config(self, cls):
        return self.cfg

    def get_global_config(self, cls):
        return self.cfg

    def mount_sandbox_method(self, method_type, name, description=""):
        def decorator(func):
            self.sandbox_methods.append(SandboxMethod(method_type, name, description, func))
            return func
        return decorator

    def mount_cleanup_method(self):
        return lambda func: (self.cleanup_methods.append(func), func)[1]

    def mount_init_method(self):
        return lambda func: func


class FakeFileSystem:
    """模拟 FileSystem：/app/uploads -> upload_path，/app/shared -> shared_path"""

    def __init__(self, root: Path) -> None:
        self.upload_path = root / "uploads"
        self.shared_path = root / "shared"
        self.upload_path.mkdir(parents=True, exist_ok=True)
        self.shared_path.mkdir(parents=True, exist_ok=True)

    def get_file(self, file_path):
        path = Path(file_path)
        if path.is_relative_to(Path("/app/shared")):
            return self.shared_path / path.relative_to(Path("/app/shared"))
        if path.is_relative_to(Path("/app/uploads")):
            return self.upload_path / path.relative_to(Path("/app/uploads"))
        raise ValueError(f"文件 {path} 不在合法沙盒目录下")


class FakeCtx:
    def __init__(self, root: Path, *, chat_key="onebot_v11-group_123456", adapter_key="onebot_v11",
                 channel_id="group_123456", channel_type="group", channel=None) -> None:
        self.chat_key = chat_key
        self.adapter_key = adapter_key
        self.channel_id = channel_id
        self.channel_type = channel_type
        self.fs = FakeFileSystem(root)
        self.db_chat_channel = channel
        self.bot = AsyncMock()

    async def get_onebot_v11_bot(self):
        if self.adapter_key != "onebot_v11":
            raise ValueError("非 OneBot v11 适配器")
        return self.bot


class FakeModelGroup(BaseModel):
    CHAT_MODEL: str = "test-vision-model"
    BASE_URL: str = "https://api.example.com/v1"
    API_KEY: str = "sk-test"
    CHAT_PROXY: str = ""
    MODEL_TYPE: str = "chat"
    ENABLE_VISION: bool = True
    TOKEN_INPUT_RATE: float = 1.0
    TOKEN_COMPLETION_RATE: float = 1.0
    MODEL_PRICE_RATE: float = 1.0
    GROUP_NAME: str = ""


# --- nekro_agent.services.agent.creator 的真实逻辑最小复刻（用于校验消息结构） ---


class OpenAIChatMessage:
    def __init__(self, role, content):
        self.role = role
        self.content = content

    def to_dict(self):
        if all(c["type"] == "text" for c in self.content):
            return {"role": self.role, "content": "".join(c["text"] for c in self.content)}
        merged, current = [], ""
        for seg in self.content:
            if seg["type"] == "text":
                current += seg["text"]
            else:
                if current:
                    merged.append({"type": "text", "text": current})
                    current = ""
                merged.append(seg)
        if current:
            merged.append({"type": "text", "text": current})
        return {"role": self.role, "content": merged}

    @classmethod
    def create_empty(cls, role):
        return cls(role, [])

    def add(self, segment):
        self.content.append(segment)
        return self

    def batch_add(self, segments):
        self.content.extend(segments)
        return self


class ContentSegment:
    @staticmethod
    def image_content(image_url: str):
        return {"type": "image_url", "image_url": {"url": image_url}}

    @staticmethod
    def image_content_from_path(image_path):
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"图片路径不存在: {path}")
        suffix = "jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "png"
        return {
            "type": "image_url",
            "image_url": {"url": f"data:image/{suffix};base64,{base64.b64encode(path.read_bytes()).decode()}"},
        }

    @staticmethod
    def text_content(text: str):
        return {"type": "text", "text": text}


class _Logger:
    def __init__(self) -> None:
        self.records = []

    def _log(self, level, msg):
        self.records.append((level, str(msg)))

    def debug(self, msg): self._log("debug", msg)
    def info(self, msg): self._log("info", msg)
    def warning(self, msg): self._log("warning", msg)
    def error(self, msg): self._log("error", msg)
    def exception(self, msg): self._log("exception", msg)


test_logger = _Logger()
gen_response_mock = AsyncMock()

api_mod = module("nekro_agent.api")
module("nekro_agent.api.core", logger=test_logger)
api_mod.core = sys.modules["nekro_agent.api.core"]
module("nekro_agent.api.plugin", ConfigBase=BaseModel, NekroPlugin=FakePlugin, SandboxMethodType=SandboxMethodType)
module("nekro_agent.api.schemas", AgentCtx=FakeCtx)
module("nekro_agent.core")
module("nekro_agent.core.config", CoreConfig=SimpleNamespace, ModelConfigGroup=FakeModelGroup)
module("nekro_agent.models")
module("nekro_agent.models.db_chat_channel", DBChatChannel=SimpleNamespace(get_channel=AsyncMock()))
module("nekro_agent.schemas")
module("nekro_agent.schemas.agent_ctx", AgentCtx=FakeCtx)
module("nekro_agent.services")
module("nekro_agent.services.agent")
module("nekro_agent.services.agent.creator", ContentSegment=ContentSegment, OpenAIChatMessage=OpenAIChatMessage)
module("nekro_agent.services.agent.openai", gen_openai_chat_response=gen_response_mock)
module("nekro_agent")  # 最后注册根包属性
sys.modules["nekro_agent"].api = api_mod


@pytest.fixture()
def ctx(tmp_path):
    channel = SimpleNamespace(
        is_active=True, observe_mode=False, instance_key="", adapter_key="onebot_v11",
        chat_key="onebot_v11-group_123456",
        get_effective_config=AsyncMock(return_value=SimpleNamespace(MODEL_GROUPS={})),
    )
    return FakeCtx(tmp_path, channel=channel)


@pytest.fixture()
def core_cfg():
    return SimpleNamespace(MODEL_GROUPS={"vision": FakeModelGroup()})


def make_video(dest: Path, seconds: float = 2.0, with_audio: bool = False) -> Path:
    """用 imageio-ffmpeg 内置 ffmpeg 生成真实小视频。"""
    import imageio_ffmpeg

    exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [exe, "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=128x96:rate=5"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest", "-c:a", "aac"]
    cmd += ["-pix_fmt", "yuv420p", str(dest)]
    subprocess.run(cmd, check=True, capture_output=True)
    return dest
