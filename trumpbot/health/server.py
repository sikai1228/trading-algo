"""Tiny localhost-only HTTP server exposing /health and /metrics.

No external dependency on aiohttp/FastAPI — we use asyncio.start_server
with hand-rolled HTTP/1.1 parsing for the two endpoints we need. Listens
on 127.0.0.1 so it is not externally reachable; pair with the firewall
rules from CLAUDE.md.

/health returns 200 if every registered critical task is alive,
otherwise 503.
/metrics returns prometheus_client text format.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Final

from prometheus_client import REGISTRY, generate_latest

from trumpbot.utils.logging import get_logger

log = get_logger(__name__)

HealthCheck = Callable[[], Awaitable[bool]]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9090

_HTTP_OK: Final[bytes] = b"HTTP/1.1 200 OK\r\n"
_HTTP_503: Final[bytes] = b"HTTP/1.1 503 Service Unavailable\r\n"
_HTTP_404: Final[bytes] = b"HTTP/1.1 404 Not Found\r\n"


class HealthcheckServer:
    """Background HTTP server bound to localhost."""

    def __init__(
        self,
        *,
        health_check: HealthCheck,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._health_check = health_check
        self._host = host
        self._port = port
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host=self._host, port=self._port)
        log.info("healthcheck_started", host=self._host, port=self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            # Drain headers; we don't need them.
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("ascii", errors="replace").split()
            if len(parts) < 2:
                writer.write(_HTTP_404 + b"Content-Length: 0\r\n\r\n")
                await writer.drain()
                return
            path = parts[1]

            if path == "/health":
                ok = await self._health_check()
                if ok:
                    body = b'{"status":"ok"}'
                    writer.write(_HTTP_OK)
                else:
                    body = b'{"status":"unhealthy"}'
                    writer.write(_HTTP_503)
                writer.write(b"Content-Type: application/json\r\n")
                writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
                writer.write(body)
            elif path == "/metrics":
                body = generate_latest(REGISTRY)
                writer.write(_HTTP_OK)
                writer.write(b"Content-Type: text/plain; version=0.0.4\r\n")
                writer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
                writer.write(body)
            else:
                writer.write(_HTTP_404 + b"Content-Length: 0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await writer.wait_closed()
