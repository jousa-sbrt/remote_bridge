"""Test consumer client to fetch data via the relay."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid

import websockets


async def consume_once(url: str, token: str, resource: str, limit: int):
    async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
        await ws.send(json.dumps({"type": "auth", "role": "consumer", "token": token}))
        auth_reply = json.loads(await ws.recv())
        if auth_reply.get("type") != "auth_ok":
            raise RuntimeError("auth failed")
        req_id = str(uuid.uuid4())
        await ws.send(json.dumps({"type": "get", "resource": resource, "params": {"limit": limit}, "request_id": req_id}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "response" and msg.get("request_id") == req_id:
                print(json.dumps(msg, indent=2))
                break


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.getenv("RELAY_URL", "ws://localhost:8080/ws"), help="ws(s):// relay url")
    ap.add_argument("--token", default=os.getenv("CONSUMER_TOKEN", "consumer-secret"), help="consumer auth token")
    ap.add_argument("--resource", default="probabilities", choices=["probabilities", "trades"])
    ap.add_argument("--limit", type=int, default=5)
    return ap.parse_args()


def main():
    args = parse_args()
    asyncio.run(consume_once(args.url, args.token, args.resource, args.limit))


if __name__ == "__main__":
    main()
