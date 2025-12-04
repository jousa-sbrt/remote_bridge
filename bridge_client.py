"""Bridge client running on the Ubuntu box.

Connects to the Render relay via websocket, answers data requests by reading
the local SQLite DB (read-only, WAL-friendly). Designed to avoid blocking the
live writer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
from typing import Any, Dict

import websockets


def _open_db(path: str) -> sqlite3.Connection:
    # Read-only, shared cache, WAL-friendly
    conn = sqlite3.connect(
        f"file:{path}?mode=ro&cache=shared",
        uri=True,
        check_same_thread=False,
        isolation_level=None,
    )
    conn.execute("PRAGMA busy_timeout=2000;")
    conn.execute("PRAGMA query_only=ON;")
    return conn


async def query_db(conn: sqlite3.Connection, resource: str, params: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(params.get("limit", 100))
    limit = max(1, min(limit, 500))
    if resource == "probabilities":
        sql = """
        SELECT ts, prob_short, prob_neutral, prob_long, trend, raw_signal, final_signal, threshold
        FROM probabilities
        ORDER BY ts DESC
        LIMIT ?
        """
    elif resource == "trades":
        sql = """
        SELECT ts, event, side, entry_price, close_price, size, pnl_pct, pnl_abs, symbol, note
        FROM trades
        ORDER BY ts DESC
        LIMIT ?
        """
    else:
        return {"status": "error", "error": "unknown_resource"}

    cur = conn.execute(sql, (limit,))
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    data = [dict(zip(cols, r)) for r in rows]
    return {"status": "ok", "data": data}


async def handle_requests(uri: str, token: str, db_path: str):
    conn = _open_db(db_path)
    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"type": "auth", "role": "producer", "token": token}))
                auth_reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                if auth_reply.get("type") != "auth_ok":
                    raise RuntimeError("auth failed")
                print("connected to relay, waiting for requests...")
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    if msg.get("type") != "request":
                        continue
                    req_id = msg.get("request_id")
                    resource = msg.get("resource")
                    params = msg.get("params") or {}
                    result = await asyncio.to_thread(query_db, conn, resource, params)
                    result["type"] = "response"
                    result["request_id"] = req_id
                    await ws.send(json.dumps(result))
        except Exception as exc:
            print(f"connection error: {exc}, retrying in 3s...")
            await asyncio.sleep(3)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.getenv("RELAY_URL", "ws://localhost:8080"), help="ws(s):// relay url")
    ap.add_argument("--token", default=os.getenv("PRODUCER_TOKEN", "producer-secret"), help="producer auth token")
    ap.add_argument("--db", default=os.getenv("SQLITE_PATH", "live_signals.db"), help="path to SQLite db")
    return ap.parse_args()


def main():
    args = parse_args()
    asyncio.run(handle_requests(args.url, args.token, args.db))


if __name__ == "__main__":
    main()
