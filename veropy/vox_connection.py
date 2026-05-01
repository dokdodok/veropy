from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import hashlib
import inspect
import os
import ssl
import traceback
from typing import Any, Self

from Crypto.Util.Padding import pad, unpad

from . import simple_bson
from .seed_crypto import SeedCBCDecrypt, SeedCBCEncrypt
from .simple_bson.etc import I32, I64


VOX_IV = bytes.fromhex("6d10dd11ba3761c9771e22f36e10c19d")

VoxPayload = dict[str, Any]
VoxPacketHandler = Callable[[int, VoxPayload], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class VoxPacket:
    method: int
    body: VoxPayload


class VoxResponseError(RuntimeError):
    def __init__(self, method: int, response: VoxPayload) -> None:
        self.method = method
        self.response = response
        self.res_code = bson_int(response["resCode"])
        super().__init__(f"vox request {method} failed: resCode={self.res_code}")


class VoxConnection:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        response_timeout: float | None = 15.0,
        on_packet: VoxPacketHandler | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._response_timeout = response_timeout
        self._on_packet = on_packet
        self._write_lock = asyncio.Lock()
        self._pending: dict[int, deque[asyncio.Future[VoxPayload]]] = defaultdict(deque)
        self._waiters: dict[int, deque[asyncio.Future[VoxPacket]]] = defaultdict(deque)
        self._closed = False
        self._recv_task = asyncio.create_task(self._recv_loop())

    @classmethod
    async def connect(
        cls,
        host: str,
        port: int,
        *,
        ssl_context: ssl.SSLContext | None = None,
        response_timeout: float | None = 15.0,
        on_packet: VoxPacketHandler | None = None,
    ) -> Self:
        if ssl_context is None:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        reader, writer = await asyncio.open_connection(
            host,
            port,
            ssl=ssl_context,
        )
        return cls(
            reader,
            writer,
            response_timeout=response_timeout,
            on_packet=on_packet,
        )

    async def request(
        self,
        method: int,
        data: VoxPayload,
        *,
        timeout: float | None = None,
    ) -> VoxPayload:
        self._raise_if_closed()

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[method].append(future)

        try:
            await self.send(method, data)
            wait_timeout = self._response_timeout if timeout is None else timeout
            if wait_timeout is None:
                response = await future
            else:
                response = await asyncio.wait_for(future, wait_timeout)

            self._raise_for_response(method, response)
            return response
        except Exception:
            self._remove_pending(method, future)
            raise

    async def send(self, method: int, data: VoxPayload) -> None:
        self._raise_if_closed()
        packet = self._encode_packet(method, data)

        async with self._write_lock:
            self._writer.write(packet)
            await self._writer.drain()

    def wait_event(
        self,
        method: int,
        *,
        timeout: float | None = None,
    ) -> asyncio.Task[VoxPacket]:
        self._raise_if_closed()

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._waiters[method].append(future)

        async def wait() -> VoxPacket:
            try:
                if timeout is None:
                    return await future
                return await asyncio.wait_for(future, timeout)
            except BaseException:
                self._remove_waiter(method, future)
                raise

        return asyncio.create_task(wait())

    def set_on_packet(self, on_packet: VoxPacketHandler | None) -> None:
        self._on_packet = on_packet

    async def close(self) -> None:
        if self._closed:
            return

        self._closed = True
        self._recv_task.cancel()
        await asyncio.gather(self._recv_task, return_exceptions=True)

        self._writer.close()
        await self._writer.wait_closed()
        self._fail_waiters(ConnectionError("Vox connection closed"))
        self._fail_pending(ConnectionError("Vox connection closed"))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _recv_loop(self) -> None:
        try:
            while True:
                packet = await self._read_packet()
                await self._dispatch_packet(packet)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._closed = True
            self._fail_waiters(exc)
            self._fail_pending(exc)
            self._writer.close()

    async def _read_packet(self) -> VoxPacket:
        header = await self._reader.readexactly(12)
        key = hashlib.md5(header[:8]).digest()
        method = int.from_bytes(header[6:8], "little")
        length = int.from_bytes(header[8:12], "little")
        ciphertext = await self._reader.readexactly(length)
        plaintext = unpad(SeedCBCDecrypt(ciphertext, key, VOX_IV), 16)
        body = simple_bson.loads(plaintext)
        return VoxPacket(method=method, body=body)

    async def _dispatch_packet(self, packet: VoxPacket) -> None:
        pending = self._pending.get(packet.method)
        if pending:
            future = pending.popleft()
            if not pending:
                self._pending.pop(packet.method, None)
            if not future.done():
                future.set_result(packet.body)
            return

        waiters = self._waiters.get(packet.method)
        if waiters:
            future = waiters.popleft()
            if not waiters:
                self._waiters.pop(packet.method, None)
            if not future.done():
                future.set_result(packet)
            return

        if self._on_packet is not None:
            result = self._on_packet(packet.method, packet.body)
            if inspect.isawaitable(result):
                task = asyncio.ensure_future(result)
                task.add_done_callback(self._log_on_packet_error)

    @staticmethod
    def _log_on_packet_error(future: asyncio.Future[Any]) -> None:
        try:
            future.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            traceback.print_exc()

    def _encode_packet(self, method: int, data: VoxPayload) -> bytes:
        header = os.urandom(6) + method.to_bytes(2, "little")
        key = hashlib.md5(header).digest()
        body = simple_bson.dumps(data)
        ciphertext = SeedCBCEncrypt(pad(body, 16), key, VOX_IV)
        length = len(ciphertext).to_bytes(4, "little")
        return header + length + ciphertext

    def _remove_pending(
        self,
        method: int,
        future: asyncio.Future[VoxPayload],
    ) -> None:
        pending = self._pending.get(method)
        if pending is None:
            return

        try:
            pending.remove(future)
        except ValueError:
            pass

        if not pending:
            self._pending.pop(method, None)

    def _remove_waiter(
        self,
        method: int,
        future: asyncio.Future[VoxPacket],
    ) -> None:
        waiters = self._waiters.get(method)
        if waiters is None:
            return

        try:
            waiters.remove(future)
        except ValueError:
            pass

        if not waiters:
            self._waiters.pop(method, None)

    def _fail_pending(self, exc: BaseException) -> None:
        for pending in self._pending.values():
            for future in pending:
                if not future.done():
                    future.set_exception(exc)
        self._pending.clear()

    def _fail_waiters(self, exc: BaseException) -> None:
        for waiters in self._waiters.values():
            for future in waiters:
                if not future.done():
                    future.set_exception(exc)
        self._waiters.clear()

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise ConnectionError("Vox connection is closed")

    def _raise_for_response(self, method: int, response: VoxPayload) -> None:
        if bson_int(response["resCode"]) != 0:
            raise VoxResponseError(method, response)


def bson_int(value: Any) -> int:
    if isinstance(value, I32 | I64):
        return value.v
    return int(value)
