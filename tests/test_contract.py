"""双版本契约检查：核对插件依赖的 NA API 在两个仓库中均存在。"""

import re
from pathlib import Path

import pytest

NEKRO_ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = NEKRO_ROOT / "nekro-agent"
AKIYO = NEKRO_ROOT / "NekroAgent_ByAkiyo"

REPOS = [pytest.param(UPSTREAM, id="upstream"), pytest.param(AKIYO, id="akiyo")]


def _read(repo: Path, rel: str) -> str:
    path = repo / rel
    if not path.is_file():
        pytest.skip(f"仓库文件不存在: {path}")
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("repo", REPOS)
def test_sandbox_method_type_agent(repo: Path):
    src = _read(repo, "nekro_agent/services/plugin/schema.py")
    assert re.search(r"AGENT\s*=", src), "缺少 SandboxMethodType.AGENT"


@pytest.mark.parametrize("repo", REPOS)
def test_mount_sandbox_method_signature(repo: Path):
    src = _read(repo, "nekro_agent/services/plugin/base.py")
    assert "def mount_sandbox_method" in src
    assert "def mount_config" in src
    assert "def mount_cleanup_method" in src
    assert "def get_config" in src


@pytest.mark.parametrize("repo", REPOS)
def test_agent_ctx_contract(repo: Path):
    src = _read(repo, "nekro_agent/schemas/agent_ctx.py")
    for symbol in ("def get_onebot_v11_bot", "def fs", "def get_core_config", "channel_type", "adapter_key"):
        assert symbol in src, f"AgentCtx 缺少 {symbol}"


@pytest.mark.parametrize("repo", REPOS)
def test_openai_chat_response(repo: Path):
    src = _read(repo, "nekro_agent/services/agent/openai.py")
    assert "async def gen_openai_chat_response" in src
    for param in ("base_url", "api_key", "proxy_url"):
        assert param in src


@pytest.mark.parametrize("repo", REPOS)
def test_creator_segments(repo: Path):
    src = _read(repo, "nekro_agent/services/agent/creator.py")
    for symbol in ("class OpenAIChatMessage", "class ContentSegment", "image_content_from_path", "text_content"):
        assert symbol in src


@pytest.mark.parametrize("repo", REPOS)
def test_filesystem_get_file(repo: Path):
    src = _read(repo, "nekro_agent/tools/file_utils.py")
    assert "def get_file" in src
    assert '"/app/shared"' in src or "'/app/shared'" in src
    assert '"/app/uploads"' in src or "'/app/uploads'" in src


@pytest.mark.parametrize("repo", REPOS)
def test_model_group_fields(repo: Path):
    src = _read(repo, "nekro_agent/core/config.py")
    for field in ("CHAT_MODEL", "BASE_URL", "API_KEY", "CHAT_PROXY", "MODEL_TYPE", "ENABLE_VISION", "MODEL_GROUPS"):
        assert field in src, f"ModelConfigGroup 缺少 {field}"


def test_akiyo_instance_awareness():
    """Akiyo 分支必须提供实例化 Bot 解析与作用域能力（上游允许不存在）。"""
    if not AKIYO.is_dir():
        pytest.skip("Akiyo 仓库不存在")
    ctx_src = _read(AKIYO, "nekro_agent/schemas/agent_ctx.py")
    assert "get_bot_by_chat_key" in ctx_src, "Akiyo 应通过 chat_key 解析实例 Bot"
    scope = _read(AKIYO, "nekro_agent/services/plugin/scope.py")
    for symbol in ("resolve_active_plugins", "resolve_scoped_config"):
        assert f"def {symbol}" in scope or f"async def {symbol}" in scope
    isolation = _read(AKIYO, "nekro_agent/services/plugin/isolation.py")
    assert "def get_quarantine" in isolation
    usage = _read(AKIYO, "nekro_agent/schemas/usage.py")
    assert 'PLUGIN = "plugin"' in usage
