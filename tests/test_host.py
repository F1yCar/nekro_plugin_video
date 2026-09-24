"""宿主适配：上游路径与 Akiyo 实例作用域路径的分支行为。"""

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from conftest import FakeCtx
from nekro_plugin_video import host
from nekro_plugin_video.models import VideoError
from nekro_plugin_video.plugin import VideoConfig, plugin


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def channel():
    return SimpleNamespace(
        is_active=True, observe_mode=False, instance_key="", adapter_key="onebot_v11",
        chat_key="onebot_v11-group_123456",
        get_effective_config=AsyncMock(return_value=SimpleNamespace(MODEL_GROUPS={})),
    )


def _akiyo_modules(monkeypatch, *, active=True, scoped_cfg=None, quarantine=None, instance_active=True):
    """注入 Akiyo 独有模块桩（monkeypatch.setitem 会在用例结束时自动还原 sys.modules）。"""
    scope = ModuleType("nekro_agent.services.plugin.scope")
    scope.resolve_active_plugins = AsyncMock(return_value=[plugin] if active else [])
    scope.resolve_scoped_config = lambda key, cls, global_cfg, instance_key: scoped_cfg or global_cfg
    monkeypatch.setitem(sys.modules, "nekro_agent.services.plugin.scope", scope)

    isolation = ModuleType("nekro_agent.services.plugin.isolation")
    isolation.get_quarantine = lambda key: quarantine
    monkeypatch.setitem(sys.modules, "nekro_agent.services.plugin.isolation", isolation)

    dai = ModuleType("nekro_agent.models.db_adapter_instance")
    dai.DBAdapterInstance = SimpleNamespace(is_instance_active=AsyncMock(return_value=instance_active))
    monkeypatch.setitem(sys.modules, "nekro_agent.models.db_adapter_instance", dai)
    return scope


class TestUpstreamPath:
    def test_uses_global_config_when_no_scope_module(self, tmp_path, channel):
        ctx = FakeCtx(tmp_path, channel=channel)
        cfg, _ = run(host.settings(ctx))
        assert isinstance(cfg, VideoConfig)

    def test_inactive_channel_rejected(self, tmp_path, channel):
        channel.is_active = False
        ctx = FakeCtx(tmp_path, channel=channel)
        with pytest.raises(VideoError, match="未启用"):
            run(host.settings(ctx))


class TestAkiyoPath:
    def test_scoped_config_resolved_with_instance_key(self, tmp_path, channel, monkeypatch):
        channel.instance_key = "inst1"
        scoped = VideoConfig(MAX_FRAMES=7)
        scope = _akiyo_modules(monkeypatch, scoped_cfg=scoped)
        ctx = FakeCtx(tmp_path, channel=channel)
        cfg, _ = run(host.settings(ctx))
        assert cfg.MAX_FRAMES == 7
        scope.resolve_active_plugins.assert_awaited_with(instance_key="inst1", chat_key=ctx.chat_key)

    def test_plugin_not_active_in_scope_rejected(self, tmp_path, channel, monkeypatch):
        _akiyo_modules(monkeypatch, active=False)
        ctx = FakeCtx(tmp_path, channel=channel)
        with pytest.raises(VideoError, match="未在当前实例"):
            run(host.settings(ctx))

    def test_quarantined_plugin_rejected(self, tmp_path, channel, monkeypatch):
        _akiyo_modules(monkeypatch, quarantine=SimpleNamespace(reason="crash"))
        ctx = FakeCtx(tmp_path, channel=channel)
        with pytest.raises(VideoError, match="隔离"):
            run(host.settings(ctx))

    def test_inactive_instance_rejected(self, tmp_path, channel, monkeypatch):
        _akiyo_modules(monkeypatch, instance_active=False)
        ctx = FakeCtx(tmp_path, channel=channel)
        with pytest.raises(VideoError, match="实例已停用"):
            run(host.settings(ctx))


class TestUsageScope:
    def test_noop_without_usage_module(self, ctx):
        group = SimpleNamespace()
        with host.usage_scope("k", "n", group):
            pass  # 上游版本：空上下文不抛异常

    def test_akiyo_usage_context(self, ctx, monkeypatch):
        entered = []

        class FakeScene:
            PLUGIN = "plugin"

        class FakeUsageContext:
            def __init__(self, scene, chat_key):
                self.scene, self.chat_key = scene, chat_key
                self.model_group_name = ""

            def with_model_group(self, name, group):
                self.model_group_name = name
                return self

        import contextlib

        usage_ctx = ModuleType("nekro_agent.services.usage.context")
        usage_ctx.LLMUsageContext = FakeUsageContext
        usage_ctx.usage_scope = lambda c: contextlib.nullcontext(entered.append(c))
        usage_schema = ModuleType("nekro_agent.schemas.usage")
        usage_schema.LLMUsageScene = FakeScene
        monkeypatch.setitem(sys.modules, "nekro_agent.services.usage.context", usage_ctx)
        monkeypatch.setitem(sys.modules, "nekro_agent.schemas.usage", usage_schema)

        with host.usage_scope("chat1", "grp", SimpleNamespace()):
            pass
        assert entered and entered[0].chat_key == "chat1" and entered[0].model_group_name == "grp"
