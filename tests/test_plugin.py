"""入口契约：仅注册 AGENT 沙盒方法，无指令/关键词触发。"""

import inspect
import sys

import pytest


def test_plugin_metadata():
    import nekro_plugin_video

    plugin = nekro_plugin_video.plugin
    assert plugin.module_name == "nekro_plugin_video"
    assert plugin.is_enabled is True


def test_only_agent_method_registered():
    import nekro_plugin_video

    methods = nekro_plugin_video.plugin.sandbox_methods
    assert len(methods) == 1
    method = methods[0]
    assert method.method_type is sys.modules["nekro_agent.api.plugin"].SandboxMethodType.AGENT
    assert method.name == "视频分析"
    assert "视频" in method.description


def test_no_command_or_matcher_registration():
    """不注册任何指令、关键词或消息监听器。"""
    import nekro_plugin_video

    plugin = nekro_plugin_video.plugin
    for attr in ("commands", "matchers", "message_handlers", "keyword_handlers", "on_message"):
        assert not getattr(plugin, attr, None), f"插件不应存在 {attr} 注册"
    assert not inspect.iscoroutinefunction(getattr(nekro_plugin_video, "on_message", None))


def test_method_signature_and_docstring():
    import nekro_plugin_video

    func = nekro_plugin_video.plugin.sandbox_methods[0].func
    params = list(inspect.signature(func).parameters)
    assert params[0] == "_ctx"
    assert params[1:] == ["source", "message_id"]
    doc = inspect.getdoc(func)
    assert doc and "_ctx" not in doc


def test_errors_raise_not_return():
    """缺少参数时必须抛异常而不是返回错误字符串。"""
    import asyncio

    import nekro_plugin_video

    func = nekro_plugin_video.plugin.sandbox_methods[0].func
    fake_ctx = object()
    with pytest.raises(Exception):
        asyncio.run(func(fake_ctx, source="", message_id=""))
