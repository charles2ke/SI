"""Bounded outbound HTTP with DNS validation and connection pinning."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections.abc import Collection
from urllib.parse import urljoin, urlsplit

import httpx


class UnsafeURLError(ValueError):
    pass


def allowed_hosts() -> set[str]:
    return {
        host.strip().lower()
        for host in os.getenv("HTTP_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    }


async def safe_fetch(
    url: str,
    *,
    allowed_hosts: Collection[str] | None = None,
    max_bytes: int = 5_000_000,
    timeout: float = 15,
    max_redirects: int = 3,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    json: object = None,
    response_headers: dict[str, str] | None = None,
) -> tuple[bytes, str]:
    """Resolve every redirect, reject non-public addresses, and connect to that IP.

    None permits public hosts; an empty allowlist permits no hosts. Environment
    proxies are intentionally ignored, otherwise they could bypass IP pinning.
    """
    async with asyncio.timeout(timeout):
        async with httpx.AsyncClient(
            trust_env=False, follow_redirects=False, timeout=timeout
        ) as client:
            for redirect in range(max_redirects + 1):
                parsed = urlsplit(url)
                if (
                    parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                    or parsed.username is not None
                    or parsed.password is not None
                    or len(url) > 8192
                ):
                    raise UnsafeURLError("Only credential-free HTTP(S) URLs are allowed")
                hostname = parsed.hostname.lower().rstrip(".")
                if allowed_hosts is not None and hostname not in {
                    host.lower().rstrip(".") for host in allowed_hosts
                }:
                    raise UnsafeURLError("Host is not allowlisted")
                try:
                    port = parsed.port or (443 if parsed.scheme == "https" else 80)
                    addresses = await asyncio.get_running_loop().getaddrinfo(
                        hostname, port, type=socket.SOCK_STREAM
                    )
                except (OSError, ValueError) as exc:
                    raise UnsafeURLError("Host cannot be resolved") from exc
                ips = {entry[4][0] for entry in addresses}
                if not ips or any(
                    not ipaddress.ip_address(ip).is_global
                    or ipaddress.ip_address(ip).is_multicast
                    for ip in ips
                ):
                    raise UnsafeURLError("Non-public network destinations are forbidden")
                pinned = httpx.URL(url).copy_with(host=sorted(ips)[0])
                request_headers = dict(headers or {})
                request_headers["Host"] = httpx.URL(url).netloc.decode("ascii")
                request = client.build_request(
                    method,
                    pinned,
                    headers=request_headers,
                    json=json if method != "GET" else None,
                    extensions={"sni_hostname": hostname},
                )
                async with client.stream(
                    request.method,
                    request.url,
                    headers=request.headers,
                    content=request.content,
                    extensions=request.extensions,
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect == max_redirects:
                            raise UnsafeURLError("Too many redirects")
                        location = response.headers.get("location")
                        if not location:
                            raise UnsafeURLError("Redirect has no location")
                        # Never forward credentials or POST bodies across a redirect.
                        if method != "GET" or headers:
                            raise UnsafeURLError("Redirects forbidden for authenticated requests")
                        url = urljoin(url, location)
                        continue
                    response.raise_for_status()
                    if response_headers is not None:
                        response_headers.update(response.headers)
                    length = response.headers.get("content-length")
                    if length and int(length) > max_bytes:
                        raise ValueError("Response exceeds byte limit")
                    data = bytearray()
                    async for block in response.aiter_bytes():
                        data.extend(block)
                        if len(data) > max_bytes:
                            raise ValueError("Response exceeds byte limit")
                    return bytes(data), response.headers.get("content-type", "")
    raise UnsafeURLError("No response")
