import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import aiofiles
from nekro_agent.api.schemas import AgentCtx

from .models import MessagePayload, Segment, VideoError, VideoSource, response_data
from .network import VideoHTTP
from .plugin import VideoConfig

_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".webm", ".mkv", ".flv", ".wmv", ".ts", ".mpeg", ".mpg", ".3gp", ".gif"}
_BILI_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv", "bili2233.cn"}
_URL = re.compile(r"https?://[^\s<>\"']+")


def is_bilibili(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in _BILI_HOSTS


def source_from_text(text: str, *, video_links_only: bool = False) -> VideoSource | None:
    for match in _URL.finditer(text):
        url = match.group().rstrip("。，,)]}")
        if not video_links_only or is_bilibili(url) or Path(urlsplit(url).path).suffix.lower() in _VIDEO_EXTS:
            return VideoSource(url=url)
    match = re.search(r"\b(BV[A-Za-z0-9]{10}|av\d+)\b", text)
    if match:
        return VideoSource(url=f"https://www.bilibili.com/video/{match.group()}")
    return None


def scan_segments(segments: list[Segment], records: list[dict[str, Any]]) -> VideoSource | None:
    for segment in segments:
        data = segment.data
        name = str(data.get("name") or data.get("file_name") or data.get("file") or "视频")
        if segment.type == "video" or (segment.type == "file" and Path(name).suffix.lower() in _VIDEO_EXTS):
            url = str(data.get("url") or "")
            file_id = str(data.get("file_id") or data.get("file") or "")
            source = VideoSource(name=name, file_id=file_id, file_uuid=str(data.get("fileUuid") or ""))
            candidate = url or file_id
            if candidate.startswith(("http://", "https://")):
                source.url = candidate
            elif candidate.startswith(("/", "file://")):
                source.path = Path(candidate.removeprefix("file://"))
            for record in records:
                for element in record.get("elements", []):
                    if not isinstance(element, dict):
                        continue
                    detail = element.get("videoElement") or element.get("fileElement") or {}
                    if detail.get("fileName", detail.get("file_name")) == name:
                        source.file_uuid = str(detail.get("fileUuid") or detail.get("file_uuid") or source.file_uuid)
                        path = detail.get("filePath") or detail.get("file_path")
                        if path:
                            source.path = Path(path)
            return source
    for segment in segments:
        if segment.type == "text":
            found = source_from_text(str(segment.data.get("text", "")))
        elif segment.type == "json":
            found = source_from_text(str(segment.data.get("data", "")).replace("\\/", "/"), video_links_only=True)
        else:
            continue
        if found:
            return found
    return None


def channel_target(ctx: AgentCtx) -> tuple[str, str]:
    """当前会话的类型与目标号

    `channel_id` 在两版 NA 中均保持 `group_xxx` / `private_xxx` 形态，
    但按不透明字段对待：提取失败即拒绝，不做猜测性回查。
    """
    kind = str(ctx.channel_type or "")
    channel_id = str(ctx.channel_id or "")
    if "group" in kind:
        match = re.search(r"group_(\d+)$", channel_id)
        if match:
            return "group", match.group(1)
    if "private" in kind:
        match = re.search(r"private_(\d+)$", channel_id)
        if match:
            return "private", match.group(1)
    return "", ""


def verify_channel(payload: MessagePayload, ctx: AgentCtx) -> None:
    kind, target = channel_target(ctx)
    if kind == "group" and payload.group_id is not None and str(payload.group_id) == target:
        return
    if kind == "private" and payload.group_id is None and payload.message_type == "private":
        peer = payload.user_id or payload.sender.get("user_id")
        if str(peer) == target:
            return
    raise VideoError("无法确认该视频消息属于当前会话，拒绝跨会话回查；请改用当前会话的视频文件")


async def message_source(ctx: AgentCtx, message_id: str) -> VideoSource:
    if ctx.adapter_key != "onebot_v11":
        raise VideoError("消息 ID 回查仅支持 OneBot v11，请传入沙盒视频路径或链接")
    if not re.fullmatch(r"-?\d{1,24}", message_id):
        raise VideoError("消息 ID 必须来自当前会话上下文，不可猜测")
    bot = await ctx.get_onebot_v11_bot()
    raw = await asyncio.wait_for(bot.call_api("get_msg", message_id=int(message_id)), 20)
    payload = MessagePayload.model_validate(response_data(raw))
    verify_channel(payload, ctx)
    if isinstance(payload.message, str):
        from nonebot.adapters.onebot.v11 import Message
        segments = [Segment(type=seg.type, data=seg.data) for seg in Message(payload.message)]
    else:
        segments = payload.message
    found = scan_segments(segments, payload.records)
    if found:
        return found
    visited: set[str] = set()
    budget = 64

    async def visit(chain: list[Segment], depth: int) -> VideoSource | None:
        nonlocal budget
        if depth > 3:
            return None
        direct = scan_segments(chain, [])
        if direct:
            return direct
        for segment in chain:
            if budget <= 0:
                break
            budget -= 1
            if segment.type == "forward":
                fid = str(segment.data.get("id") or "")
                if not fid or fid in visited:
                    continue
                visited.add(fid)
                data = response_data(await asyncio.wait_for(bot.call_api("get_forward_msg", id=fid), 20))
                nodes = data.get("messages") or data.get("nodes") or []
            elif segment.type == "node":
                nodes = [segment.data]
            elif segment.type == "json":
                continue
            else:
                continue
            for node in nodes[:budget]:
                budget -= 1
                content = node.get("content", node.get("message", []))
                if isinstance(content, list):
                    found = await visit([Segment.model_validate(item) for item in content], depth + 1)
                    if found:
                        return found
        return None

    found = await visit(segments, 0)
    if found:
        return found
    raise VideoError("当前消息中没有找到视频；此工具不解释文字、图片或普通文件")


def allowed_path(ctx: AgentCtx, path: Path) -> Path:
    resolved = path.resolve()
    roots = (ctx.fs.upload_path.resolve(), ctx.fs.shared_path.resolve())
    if not any(resolved.is_relative_to(root) for root in roots) or not resolved.is_file():
        raise VideoError("视频文件不在当前会话的共享/上传目录内，或文件不存在")
    return resolved


async def resolve_file(ctx: AgentCtx, source: VideoSource) -> VideoSource:
    if source.url:
        return source
    if source.path is not None:
        try:
            source.path = allowed_path(ctx, source.path)
            return source
        except VideoError:
            pass
    fid = source.file_uuid or source.file_id
    if not fid or "/" in fid or "\\" in fid or len(fid) > 160:
        raise VideoError("协议端未返回可访问的视频地址，请重新上传视频")
    if Path(fid).suffix and not re.fullmatch(r"[0-9a-fA-F-]{32,}\.[A-Za-z0-9]+", fid):
        raise VideoError("协议端只返回了文件名，不能用文件名代替视频文件 ID")
    bot = await ctx.get_onebot_v11_bot()
    kind, target = channel_target(ctx)
    calls = []
    if kind == "group":
        calls.append(("get_group_file_url", {"group_id": int(target), "file_id": fid}))
    elif kind == "private":
        calls.append(("get_private_file_url", {"file_id": fid}))
    calls.extend([("get_file", {"file_id": fid}), ("get_file", {"file": fid})])
    for action, params in calls:
        try:
            data = response_data(await asyncio.wait_for(bot.call_api(action, **params), 20))
            candidate = str(data.get("url") or data.get("file") or "")
            if candidate.startswith(("http://", "https://")):
                return VideoSource(url=candidate, name=source.name)
            if candidate:
                path = allowed_path(ctx, Path(candidate.removeprefix("file://")))
                return VideoSource(path=path, name=source.name)
        except Exception:
            continue
    raise VideoError("无法获取该视频文件；NapCat 的本地路径不等于 NA 的沙盒路径")


async def bilibili_stream(http: VideoHTTP, url: str) -> str:
    if urlsplit(url).hostname in {"b23.tv", "bili2233.cn"}:
        url = await http.final_url(url)
    if not is_bilibili(url):
        raise VideoError("B站短链跳转目标不受支持")
    parts = urlsplit(url)
    match = re.fullmatch(r"/video/(BV[A-Za-z0-9]{10}|av\d+)/?", parts.path)
    if not match:
        raise VideoError("仅支持 B站视频，不解析动态、直播或专栏")
    identifier = match.group(1)
    query = {"aid": identifier[2:]} if identifier.startswith("av") else {"bvid": identifier}
    view = await http.json("https://api.bilibili.com/x/web-interface/view?" + urlencode(query))
    if view.get("code") != 0:
        raise VideoError("B站视频信息获取失败，请确认视频公开可访问")
    info = response_data(view)
    page = int(parse_qs(parts.query).get("p", ["1"])[0]) - 1
    pages = info.get("pages", [])
    if page < 0 or page >= len(pages):
        raise VideoError("B站视频分P不存在")
    params = {"avid": info["aid"], "cid": pages[page]["cid"], "qn": 80, "type": "mp4", "platform": "html5"}
    play = await http.json("https://api.bilibili.com/x/player/playurl?" + urlencode(params))
    if play.get("code") != 0:
        raise VideoError("B站未提供可下载的视频流")
    streams = response_data(play).get("durl", [])
    if len(streams) != 1 or not streams[0].get("url"):
        raise VideoError("当前 B站视频未提供单文件视频流；请改为上传视频文件")
    return str(streams[0]["url"])


async def acquire_video(ctx: AgentCtx, source: str, message_id: str, dest: Path, cfg: VideoConfig) -> None:
    if bool(source.strip()) == bool(message_id.strip()):
        raise VideoError("source 和 message_id 必须且只能提供一个")
    if message_id:
        selected = await resolve_file(ctx, await message_source(ctx, message_id.strip()))
    elif source.startswith(("http://", "https://")):
        selected = VideoSource(url=source.strip())
    elif re.fullmatch(r"BV[A-Za-z0-9]{10}|av\d+", source):
        selected = VideoSource(url=f"https://www.bilibili.com/video/{source}")
    else:
        try:
            selected = VideoSource(path=allowed_path(ctx, ctx.fs.get_file(source)))
        except (OSError, ValueError):
            raise VideoError("请使用当前会话的 /app/uploads/ 或 /app/shared/ 视频路径，不能传宿主机路径") from None
    limit = cfg.MAX_SIZE_MB * 1024 * 1024
    if selected.url:
        async with VideoHTTP(cfg.NETWORK_TIMEOUT) as http:
            url = selected.url
            headers = None
            if is_bilibili(url):
                url = await bilibili_stream(http, url)
                headers = {"Referer": "https://www.bilibili.com/"}
            await http.download(url, dest, limit, headers)
        return
    if selected.path is None:
        raise VideoError("视频源为空")
    if selected.path.stat().st_size > limit:
        raise VideoError("视频超过配置的大小上限")
    total = 0
    async with aiofiles.open(selected.path, "rb") as src, aiofiles.open(dest, "xb") as out:
        while chunk := await src.read(65536):
            total += len(chunk)
            if total > limit:
                raise VideoError("视频超过配置的大小上限")
            await out.write(chunk)
    if not total:
        raise VideoError("视频文件为空")
