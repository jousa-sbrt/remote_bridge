"""Render relay server using aiohttp (handles GET/HEAD health checks + websocket at /ws)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from typing import Dict, Optional

from aiohttp import web, WSMsgType

PRODUCER_TOKEN = os.environ.get("PRODUCER_TOKEN", "producer-secret")
CONSUMER_TOKEN = os.environ.get("CONSUMER_TOKEN", "consumer-secret")
REQUEST_TIMEOUT_SEC = 10


class RelayState:
    def __init__(self):
        self.producer: Optional[web.WebSocketResponse] = None
        self.consumers: set[web.WebSocketResponse] = set()
        self.pending: Dict[str, web.WebSocketResponse] = {}
        self.lock = asyncio.Lock()

    async def cleanup_ws(self, ws: web.WebSocketResponse, role: Optional[str]):
        async with self.lock:
            if role == "producer" and self.producer is ws:
                self.producer = None
        self.consumers.discard(ws)
        stale = [rid for rid, consumer in self.pending.items() if consumer is ws]
        for rid in stale:
            self.pending.pop(rid, None)


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def ws_handler(request: web.Request) -> web.StreamResponse:
    state: RelayState = request.app["state"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    role = await auth(ws)
    if not role:
        await ws.close(code=4000, message=b"auth failed")
        return ws
    print(f"client connected as {role}")

    if role == "producer":
        async with state.lock:
            state.producer = ws
        await handle_producer(ws, state)
    else:
        state.consumers.add(ws)
        await handle_consumer(ws, state)

    await state.cleanup_ws(ws, role)
    return ws


async def auth(ws: web.WebSocketResponse) -> Optional[str]:
    try:
        msg = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except Exception:
        return None
    if not isinstance(msg, dict) or msg.get("type") != "auth":
        return None
    role = msg.get("role")
    token = msg.get("token")
    if role == "producer" and token == PRODUCER_TOKEN:
        await ws.send_json({"type": "auth_ok", "role": "producer"})
        return "producer"
    if role == "consumer" and token == CONSUMER_TOKEN:
        await ws.send_json({"type": "auth_ok", "role": "consumer"})
        return "consumer"
    return None


async def handle_producer(ws: web.WebSocketResponse, state: RelayState):
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("type") != "response":
            continue
        req_id = data.get("request_id")
        consumer = state.pending.pop(req_id, None)
        if not consumer:
            continue
        try:
            await consumer.send_json(data)
        except Exception:
            pass


async def handle_consumer(ws: web.WebSocketResponse, state: RelayState):
    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("type") != "get":
            continue
        client_req_id = data.get("request_id")
        req_id = client_req_id or str(uuid.uuid4())
        if not state.producer:
            await ws.send_json(
                {"type": "response", "request_id": req_id, "status": "error", "error": "producer_offline"}
            )
            continue
        forward = {
            "type": "request",
            "request_id": req_id,
            "resource": data.get("resource"),
            "params": data.get("params") or {},
        }
        state.pending[req_id] = ws
        try:
            await state.producer.send_json(forward)
        except Exception:
            state.pending.pop(req_id, None)
            await ws.send_json(
                {"type": "response", "request_id": req_id, "status": "error", "error": "send_failed"}
            )
            continue
        asyncio.create_task(timeout_request(req_id, ws, state, REQUEST_TIMEOUT_SEC))


async def timeout_request(req_id: str, consumer: web.WebSocketResponse, state: RelayState, timeout: int):
    await asyncio.sleep(timeout)
    pending_consumer = state.pending.pop(req_id, None)
    if pending_consumer and pending_consumer is consumer:
        try:
            await consumer.send_json(
                {"type": "response", "request_id": req_id, "status": "error", "error": "timeout"}
            )
        except Exception:
            pass


def create_app() -> web.Application:
    app = web.Application()
    app["state"] = RelayState()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)  # handles GET/HEAD probes
    app.router.add_get("/ws", ws_handler)
    return app


async def async_main():
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
    await site.start()
    print("Relay server listening on /ws (health at /health)")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(async_main())
