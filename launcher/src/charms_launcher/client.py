"""
LauncherClient: dials out to the server, registers as a Rune, heartbeats, and
serves assigned tasks. Reconnects with exponential backoff (1s → 30s); the
rune_id is opaque to the launcher — the server usually hands back the same one
after a reconnect, but a fresh one is fine too.
"""

import asyncio
import contextlib
import logging
import socket
import time
from typing import Any

import websockets

from charms_core import protocol
from charms_core.chunk import Chunk, decode_chunk_message, encode_chunk_message
from charms_core.seed import Seed, SeedDescriptor
from charms_core.types import ProtocolError
from charms_launcher.executor import SeedExecutor

logger = logging.getLogger(__name__)


def detect_hardware() -> protocol.HardwareInfo:
    try:
        import torch

        if torch.cuda.is_available():
            props = torch.cuda.get_device_properties(0)
            return protocol.HardwareInfo(
                gpu=props.name, vram_mb=int(props.total_memory // (1 << 20))
            )
    except Exception:  # torch absent or GPU probing failed — report nothing
        pass
    return protocol.HardwareInfo()


class LauncherClient:
    def __init__(
        self,
        seed: Seed[Any, Any, Any],
        *,
        server_url: str,
        rune_key: str,
        name: str,
        heartbeat_interval: float = 10.0,
    ) -> None:
        self._seed = seed
        self._descriptor = SeedDescriptor.from_seed(type(seed))
        self._server_url = server_url.rstrip("/")
        self._rune_key = rune_key
        self._name = name
        self._heartbeat_interval = heartbeat_interval
        self._executor = SeedExecutor(seed)
        self._stop = False
        self._stream_compute_ms: dict[str, float] = {}  # active compute per open stream

    def stop(self) -> None:
        self._stop = True

    async def run_forever(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                await self._serve_once()
                backoff = 1.0
            except (OSError, websockets.WebSocketException, TimeoutError) as exc:
                logger.warning("connection lost (%s); reconnecting in %.0fs", exc, backoff)
            if self._stop:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _serve_once(self) -> None:
        url = f"{self._server_url}/ws/launcher"
        async with websockets.connect(url, max_size=64 * (1 << 20)) as ws:
            await ws.send(
                protocol.encode_message(
                    protocol.RegisterMsg(
                        rune_key=self._rune_key,
                        launcher=protocol.LauncherInfo(name=self._name, host=socket.gethostname()),
                        hardware=detect_hardware(),
                        seed=self._descriptor,
                    )
                )
            )
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            registered = protocol.parse_server_to_launcher(protocol.parse_json_frame(raw))
            if not isinstance(registered, protocol.RegisteredMsg):
                raise ProtocolError("registration was not acknowledged")
            rune_id = registered.rune_id
            logger.info("registered as rune %s (seed=%s)", rune_id, self._descriptor.manifest.id)

            heartbeat = asyncio.create_task(self._heartbeat(ws, rune_id))
            try:
                await self._serve_messages(ws)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _heartbeat(self, ws: websockets.ClientConnection, rune_id: str) -> None:
        while True:
            await ws.send(
                protocol.encode_message(
                    protocol.HeartbeatMsg(rune_id=rune_id, busy=self._executor.busy)
                )
            )
            await asyncio.sleep(self._heartbeat_interval)

    async def _serve_messages(self, ws: websockets.ClientConnection) -> None:
        pending_chunk_header: dict[str, Any] | None = None
        async for raw in ws:
            if isinstance(raw, bytes):
                if pending_chunk_header is not None:
                    header, pending_chunk_header = pending_chunk_header, None
                    await self._on_stream_chunk(ws, decode_chunk_message(header, raw))
                continue
            try:
                obj = protocol.parse_json_frame(raw)
            except ProtocolError:
                logger.warning("server sent a malformed frame")
                continue
            if protocol.is_chunk_message(obj):
                if obj.get("binary"):
                    pending_chunk_header = obj  # payload arrives in the next frame
                else:
                    await self._on_stream_chunk(ws, decode_chunk_message(obj, None))
                continue
            try:
                message = protocol.parse_server_to_launcher(obj)
            except ProtocolError as exc:
                logger.warning("malformed message: %s", exc)
                continue
            if isinstance(message, protocol.TaskAssignMsg):
                await self._on_assign(ws, message)
            elif isinstance(message, protocol.TaskCancelMsg):
                self._executor.cancel(message.task_id)
            elif isinstance(message, protocol.StreamOpenMsg):
                await self._on_stream_open(ws, message)
            elif isinstance(message, protocol.StreamCloseMsg):
                await self._on_stream_close(ws, message)

    async def _on_assign(
        self, ws: websockets.ClientConnection, msg: protocol.TaskAssignMsg
    ) -> None:
        if self._executor.busy:
            await ws.send(
                protocol.encode_message(
                    protocol.TaskErrorMsg(task_id=msg.task_id, error="rune is busy")
                )
            )
            return
        await ws.send(protocol.encode_message(protocol.TaskAcceptMsg(task_id=msg.task_id)))

        async def _run() -> None:
            try:
                result = await self._executor.run_task(msg.task_id, msg.input, msg.config)
            except asyncio.CancelledError:
                return  # server cancelled the task; it no longer wants a reply
            with contextlib.suppress(Exception):
                await ws.send(protocol.encode_message(result))

        self._executor.track(msg.task_id, asyncio.create_task(_run()))

    # ------------------------------------------------------------------ #
    #  Realtime streams                                                    #
    # ------------------------------------------------------------------ #

    async def _on_stream_open(
        self, ws: websockets.ClientConnection, msg: protocol.StreamOpenMsg
    ) -> None:
        config_obj = (
            self._seed.config_model(**msg.config)
            if msg.config is not None and self._seed.config_model is not None
            else None
        )
        try:
            started = time.perf_counter()
            await self._seed.stream_open(msg.stream_id, config_obj)
            self._stream_compute_ms[msg.stream_id] = (time.perf_counter() - started) * 1000
            await ws.send(
                protocol.encode_message(protocol.StreamOpenedMsg(stream_id=msg.stream_id))
            )
        except Exception as exc:
            await ws.send(
                protocol.encode_message(
                    protocol.StreamErrorMsg(stream_id=msg.stream_id, error=str(exc))
                )
            )

    async def _on_stream_chunk(self, ws: websockets.ClientConnection, chunk: Chunk) -> None:
        try:
            started = time.perf_counter()
            reply = await self._seed.stream_chunk(chunk.stream_id, chunk)
            self._stream_compute_ms[chunk.stream_id] = (
                self._stream_compute_ms.get(chunk.stream_id, 0.0)
                + (time.perf_counter() - started) * 1000
            )
        except Exception as exc:
            await ws.send(
                protocol.encode_message(
                    protocol.StreamErrorMsg(stream_id=chunk.stream_id, error=str(exc))
                )
            )
            return
        if reply is not None:
            text, binary = encode_chunk_message(protocol.CHUNK_TYPE_LAUNCHER, reply)
            await ws.send(text)
            if binary is not None:
                await ws.send(binary)

    async def _on_stream_close(
        self, ws: websockets.ClientConnection, msg: protocol.StreamCloseMsg
    ) -> None:
        try:
            started = time.perf_counter()
            final = await self._seed.stream_close(msg.stream_id)
            compute_ms = self._stream_compute_ms.pop(msg.stream_id, 0.0) + (
                (time.perf_counter() - started) * 1000
            )
            await ws.send(
                protocol.encode_message(
                    protocol.StreamClosedMsg(
                        stream_id=msg.stream_id,
                        final_output=final.model_dump(mode="json") if final is not None else None,
                        compute_ms=compute_ms,
                        billable_units=self._seed.billable_units,
                    )
                )
            )
        except Exception as exc:
            self._stream_compute_ms.pop(msg.stream_id, None)
            await ws.send(
                protocol.encode_message(
                    protocol.StreamErrorMsg(stream_id=msg.stream_id, error=str(exc))
                )
            )
