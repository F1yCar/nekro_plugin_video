from contextlib import AbstractContextManager, nullcontext
from importlib import import_module
from types import ModuleType
from typing import Any

from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core.config import CoreConfig, ModelConfigGroup
from nekro_agent.models.db_chat_channel import DBChatChannel

from .models import VideoError
from .plugin import VideoConfig, plugin


def optional_module(name: str) -> ModuleType | None:
    """探测 Akiyo 独有模块；目标或其任一父包缺失时返回 None（视为上游版本）。"""
    try:
        return import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name and (exc.name == name or name.startswith(exc.name + ".")):
            return None
        raise


async def settings(ctx: AgentCtx) -> tuple[VideoConfig, CoreConfig]:
    if not plugin.is_enabled:
        raise VideoError("视频插件已停用")
    channel = ctx.db_chat_channel or await DBChatChannel.get_channel(ctx.chat_key)
    if not channel.is_active or channel.observe_mode:
        raise VideoError("当前频道未启用交互")
    scope = optional_module("nekro_agent.services.plugin.scope")
    if scope is not None:
        instance_key = channel.instance_key
        active = await scope.resolve_active_plugins(instance_key=instance_key, chat_key=ctx.chat_key)
        if not any(item.key == plugin.key for item in active):
            raise VideoError("视频插件未在当前实例或会话启用")
        from nekro_agent.models.db_adapter_instance import DBAdapterInstance
        from nekro_agent.services.plugin.isolation import get_quarantine
        if not await DBAdapterInstance.is_instance_active(channel.adapter_key, instance_key):
            raise VideoError("当前实例已停用")
        if get_quarantine(plugin.key) is not None:
            raise VideoError("视频插件当前处于隔离状态")
        cfg = scope.resolve_scoped_config(plugin.key, VideoConfig, plugin.get_global_config(VideoConfig), instance_key)
    else:
        cfg = plugin.get_config(VideoConfig)
    snapshot = VideoConfig.model_validate({name: getattr(cfg, name) for name in VideoConfig.model_fields})
    return snapshot, await channel.get_effective_config()


def usage_scope(chat_key: str, name: str, group: ModelConfigGroup) -> AbstractContextManager[Any]:
    """Akiyo Devin 的用量归属上下文；上游无此模块时退化为空上下文。"""
    usage_ctx = optional_module("nekro_agent.services.usage.context")
    usage_schema = optional_module("nekro_agent.schemas.usage")
    if usage_ctx is None or usage_schema is None:
        return nullcontext()
    context = usage_ctx.LLMUsageContext(scene=usage_schema.LLMUsageScene.PLUGIN, chat_key=chat_key).with_model_group(
        name, group,
    )
    return usage_ctx.usage_scope(context)
