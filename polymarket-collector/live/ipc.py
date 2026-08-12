from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable


MAX_MESSAGE_BYTES = 1_048_576


class TraderIPCError(RuntimeError):
    pass


class TraderIPCClient:
    def __init__(self, socket_path: str | Path, timeout_seconds: float = 10.0):
        self.socket_path = str(socket_path)
        self.timeout_seconds = float(timeout_seconds)

    @staticmethod
    def _request(command: str, payload: dict[str, Any] | None) -> bytes:
        encoded = json.dumps(
            {
                "request_id": uuid.uuid4().hex,
                "command": str(command),
                "payload": payload or {},
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            raise TraderIPCError("IPC request is too large")
        return encoded + b"\n"

    @staticmethod
    def _result(raw: bytes) -> Any:
        if len(raw) > MAX_MESSAGE_BYTES:
            raise TraderIPCError("IPC response is too large")
        try:
            response = json.loads(raw)
        except Exception as exc:
            raise TraderIPCError("Invalid IPC response") from exc
        if not response.get("ok"):
            error = response.get("error") or {}
            raise TraderIPCError(
                f"{error.get('code') or 'TRADER_ERROR'}: "
                f"{error.get('message') or 'Trader command failed'}"
            )
        return response.get("result")

    def call(self, command: str, payload: dict[str, Any] | None = None) -> Any:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout_seconds)
            client.connect(self.socket_path)
            client.sendall(self._request(command, payload))
            chunks = bytearray()
            while b"\n" not in chunks:
                part = client.recv(65_536)
                if not part:
                    break
                chunks.extend(part)
                if len(chunks) > MAX_MESSAGE_BYTES:
                    raise TraderIPCError("IPC response is too large")
        return self._result(bytes(chunks).split(b"\n", 1)[0])

    async def call_async(
        self, command: str, payload: dict[str, Any] | None = None
    ) -> Any:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(self.socket_path),
            timeout=self.timeout_seconds,
        )
        try:
            writer.write(self._request(command, payload))
            await writer.drain()
            raw = await asyncio.wait_for(
                reader.readline(), timeout=self.timeout_seconds
            )
            return self._result(raw.rstrip(b"\n"))
        finally:
            writer.close()
            await writer.wait_closed()


class TraderIPCServer:
    def __init__(
        self,
        socket_path: str | Path,
        handler: Callable[[str, dict[str, Any]], Awaitable[Any] | Any],
        *,
        allowed_uids: set[int] | None = None,
    ):
        self.socket_path = Path(socket_path)
        self.handler = handler
        self.allowed_uids = allowed_uids or {os.getuid(), 0}
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        if self.socket_path.exists() or self.socket_path.is_socket():
            self.socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=str(self.socket_path)
        )
        os.chmod(self.socket_path, 0o660)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.socket_path.exists() or self.socket_path.is_socket():
            self.socket_path.unlink()

    @staticmethod
    def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
        sock = writer.get_extra_info("socket")
        if sock is None or not hasattr(socket, "SO_PEERCRED"):
            return None
        try:
            _pid, uid, _gid = struct.unpack(
                "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            return int(uid)
        except OSError:
            return None

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_id = ""
        try:
            peer_uid = self._peer_uid(writer)
            if peer_uid is not None and peer_uid not in self.allowed_uids:
                raise PermissionError("IPC peer UID is not allowed")
            raw = await reader.readline()
            if not raw or len(raw) > MAX_MESSAGE_BYTES:
                raise ValueError("Invalid IPC request size")
            request = json.loads(raw)
            request_id = str(request.get("request_id") or "")
            command = str(request.get("command") or "")
            payload = request.get("payload") or {}
            if not command or not isinstance(payload, dict):
                raise ValueError("Invalid IPC request")
            result = self.handler(command, payload)
            if asyncio.iscoroutine(result):
                result = await result
            response = {"ok": True, "request_id": request_id, "result": result}
        except Exception as exc:
            response = {
                "ok": False,
                "request_id": request_id,
                "error": {
                    "code": type(exc).__name__.upper(),
                    "message": str(exc)[:500],
                },
            }
        encoded = json.dumps(
            response, separators=(",", ":"), sort_keys=True, default=str
        ).encode("utf-8")
        if len(encoded) > MAX_MESSAGE_BYTES:
            encoded = json.dumps(
                {
                    "ok": False,
                    "request_id": request_id,
                    "error": {
                        "code": "RESPONSE_TOO_LARGE",
                        "message": "Trader response exceeded IPC limit",
                    },
                }
            ).encode("utf-8")
        writer.write(encoded + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
