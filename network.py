import asyncio
import ipaddress
import json
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlsplit

import aiofiles
import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver

from .models import VideoError


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise VideoError("只接受无内嵌凭据的 HTTP/HTTPS 视频地址")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local")) or "%" in host:
        raise VideoError("不允许访问本机或内网地址，请改用当前会话的沙盒视频文件")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return
    if not address.is_global:
        raise VideoError("不允许访问非公网地址")


class PublicResolver(AbstractResolver):
    def __init__(self) -> None:
        self.resolver = DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[Any]:
        records = await self.resolver.resolve(host, port, family)
        if not records or any(not ipaddress.ip_address(record["host"]).is_global for record in records):
            raise VideoError("下载域名解析到了非公网地址")
        return records

    async def close(self) -> None:
        await self.resolver.close()


class VideoHTTP:
    def __init__(self, timeout: int) -> None:
        self.timeout = timeout
        self.session: aiohttp.ClientSession | None = None
        self.resolver: PublicResolver | None = None

    async def __aenter__(self) -> "VideoHTTP":
        self.resolver = PublicResolver()
        self.session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(resolver=self.resolver, use_dns_cache=False),
            timeout=aiohttp.ClientTimeout(total=self.timeout),
            headers={"User-Agent": "Mozilla/5.0"},
            trust_env=False, auto_decompress=False,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self.session is not None:
            await self.session.close()
        if self.resolver is not None:
            await self.resolver.close()

    @asynccontextmanager
    async def open(self, url: str, headers: dict[str, str] | None = None) -> AsyncIterator[aiohttp.ClientResponse]:
        if self.session is None:
            raise RuntimeError("HTTP 会话尚未初始化")
        for _ in range(6):
            validate_url(url)
            response = await self.session.get(url, headers=headers, allow_redirects=False)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise VideoError("视频地址重定向缺少目标")
                url = urljoin(url, location)
                continue
            try:
                if response.status != 200:
                    raise VideoError(f"视频服务返回 HTTP {response.status}")
                if response.headers.get("Content-Encoding", "identity") not in {"identity", ""}:
                    raise VideoError("暂不接受压缩传输的视频响应")
                yield response
            finally:
                response.close()
            return
        raise VideoError("视频地址重定向次数过多")

    async def json(self, url: str) -> dict[str, Any]:
        async with self.open(url) as response:
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise VideoError("视频信息响应过大")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise VideoError("视频信息响应格式不正确")
            return value

    async def final_url(self, url: str) -> str:
        async with self.open(url) as response:
            return str(response.url)

    async def download(self, url: str, dest: Path, limit: int, headers: dict[str, str] | None = None) -> None:
        async with self.open(url, headers) as response:
            if response.content_type in {"text/html", "application/json", "application/vnd.apple.mpegurl", "application/x-mpegurl"}:
                raise VideoError("地址不是可下载的视频文件；不解析普通网页或直播播放列表")
            if response.content_length is not None and response.content_length > limit:
                raise VideoError("视频超过配置的大小上限")
            total = 0
            started = time.monotonic()
            async with aiofiles.open(dest, "xb") as output:
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > limit:
                        raise VideoError("视频超过配置的大小上限")
                    await output.write(chunk)
                    await asyncio.sleep(max(0, total / (8 * 1024 * 1024) - (time.monotonic() - started)))
            if total == 0:
                raise VideoError("下载的视频为空")
