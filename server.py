"""Lightweight websocket relay for Render.

Roles:
- producer: connects from the Ubuntu box (behind firewall), supplies data.
- consumer: connects from the mobile/desktop app, requests data.

Authentication: both roles must send an auth message with the correct token.
Transport security: Render terminates TLS, use wss://<your-service>.onrender.com.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from typing import Dict, Optional

import websockets
from websockets.server import WebSocketServerProtocol


PRODUCER_TOKEN = os.environ.get("PRODUCER_TOKEN", "producer-secret")
CONSUMER_TOKEN = os.environ.get("CONSUMER_TOKEN", "consumer-secret")
HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
REQUEST_TIMEOUT_SEC = 10


class RelayServer:
    def __init__(self):
        self.producer: Optional[WebSocketServerProtocol] = None
        self.consumers: set[WebSocketServerProtocol] = set()
        self.pending: Dict[str, WebSocketServerProtocol] = {}
        self.lock = asyncio.Lock()

    async def handler(self, websocket: WebSocketServerProtocol):
        role = await self._auth(websocket)
        if not role:
            return
        try:
            if role == "producer":
                await self._handle_producer(websocket)
            else:
                await self._handle_consumer(websocket)
        finally:
            await self._cleanup(websocket, role)

    async def _auth(self, websocket: WebSocketServerProtocol) -> Optional[str]:
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10)
            msg = json.loads(raw)
        except Exception:
            await websocket.close(code=4000, reason="auth failed")
            return None
        if not isinstance(msg, dict) or msg.get("type") != "auth":
            await websocket.close(code=4001, reason="auth message expected")
            return None
        role = msg.get("role")
        token = msg.get("token")
        if role == "producer" and token == PRODUCER_TOKEN:
            await websocket.send(json.dumps({"type": "auth_ok", "role": "producer"}))
            return "producer"
        if role == "consumer" and token == CONSUMER_TOKEN:
            await websocket.send(json.dumps({"type": "auth_ok", "role": "consumer"}))
            return "consumer"
        await websocket.close(code=4002, reason="invalid token")
        return None

    async def _handle_producer(self, websocket: WebSocketServerProtocol):
        async with self.lock:
            self.producer = websocket
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") != "response":
                continue
            req_id = msg.get("request_id")
            consumer = self.pending.pop(req_id, None)
            if not consumer:
                continue
            try:
                await consumer.send(json.dumps(msg))
            except Exception:
                pass

    async def _handle_consumer(self, websocket: WebSocketServerProtocol):
        self.consumers.add(websocket)
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") != "get":
                continue
            req_id = str(uuid.uuid4())
            if not self.producer:
                await websocket.send(
                    json.dumps(
                        {"type": "response", "request_id": req_id, "status": "error", "error": "producer_offline"}
                    )
                )
                continue
            forward = {
                "type": "request",
                "request_id": req_id,
                "resource": msg.get("resource"),
                "params": msg.get("params") or {},
            }
            self.pending[req_id] = websocket
            try:
                await self.producer.send(json.dumps(forward))
            except Exception:
                self.pending.pop(req_id, None)
                await websocket.send(
                    json.dumps(
                        {"type": "response", "request_id": req_id, "status": "error", "error": "send_failed"}
                    )
                )
                continue
            asyncio.create_task(self._timeout_request(req_id, websocket, REQUEST_TIMEOUT_SEC))

    async def _timeout_request(self, req_id: str, websocket: WebSocketServerProtocol, timeout: int):
        await asyncio.sleep(timeout)
        consumer = self.pending.pop(req_id, None)
        if consumer and consumer == websocket:
            try:
                await consumer.send(
                    json.dumps(
                        {"type": "response", "request_id": req_id, "status": "error", "error": "timeout"}
                    )
                )
            except Exception:
                pass

    async def _cleanup(self, websocket: WebSocketServerProtocol, role: Optional[str]):
        async with self.lock:
            if role == "producer" and self.producer is websocket:
                self.producer = None
        if websocket in self.consumers:
            self.consumers.discard(websocket)
        # Drop pending requests bound to this consumer
        stale = [rid for rid, ws in self.pending.items() if ws is websocket]
        for rid in stale:
            self.pending.pop(rid, None)


async def main():
    server = RelayServer()
    stop = asyncio.Future()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)

    async with websockets.serve(server.handler, HOST, PORT, ping_interval=20, ping_timeout=20):
        print(f"Relay server listening on ws://{HOST}:{PORT}")
        await stop


if __name__ == "__main__":
    asyncio.run(main())
