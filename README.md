# Remote Bridge (Render Relay + Ubuntu Client)

Ziel: App <-> Render Relay <-> Ubuntu-Box (hinter Firewall). Ubuntu pusht nicht aktiv Daten, sondern beantwortet Abfragen über den Relay. Transport ist per `wss://` verschlüsselt (Render TLS), Auth via Tokens.

## Komponenten
- `remote_bridge/server.py` (Render): WebSocket-Relay. Rollen: `producer` (Ubuntu) und `consumer` (App).
- `remote_bridge/bridge_client.py` (Ubuntu): Baut WSS-Verbindung auf, liest aus `live_signals.db` (read-only, WAL-freundlich), beantwortet Requests.

## Deployment Render (kostenloses Web Service)
1. Runtime: Python 3.11+, Dependencies: `aiohttp`, `websockets` (siehe requirements.txt).
2. Env Vars:
   - `PRODUCER_TOKEN` (geheim, Ubuntu benutzt ihn)
   - `CONSUMER_TOKEN` (geheim, App benutzt ihn)
   - `PORT` (Render setzt automatisch, Code nutzt Default 8080)
3. Start Command: `python remote_bridge/server.py`
4. Service-URL: `wss://<dein-service>.onrender.com/ws` (Health: `/health`)

## Protokoll (JSON)
### Auth (beide Rollen)
`{"type":"auth","role":"producer"|"consumer","token":"<token>"}`  
Antwort bei Erfolg: `{"type":"auth_ok","role":"<role>"}`

### Consumer -> Relay (WS Pfad `/ws`)
`{"type":"get","resource":"probabilities"|"trades","params":{"limit":100}}`

### Relay -> Producer
`{"type":"request","request_id":"<uuid>","resource":"...","params":{...}}`

### Producer -> Relay
`{"type":"response","request_id":"<same>","status":"ok","data":[...]}`
oder `{"status":"error","error":"..."}`

### Relay -> Consumer
`{"type":"response","request_id":"<same>","status":"ok","data":[...]}`
oder `{"status":"error","error":"producer_offline|timeout|send_failed|unknown_resource"}`

Timeout: 10s pro Request.

## Ubuntu-Client starten
```bash
python remote_bridge/bridge_client.py \
  --url wss://<dein-service>.onrender.com/ws \
  --token "$PRODUCER_TOKEN" \
  --db live_signals.db
```
Flags sind optional; Defaults kommen aus Env `RELAY_URL`, `PRODUCER_TOKEN`, `SQLITE_PATH`.

SQLite-Zugriff: read-only, `cache=shared`, `busy_timeout=2000`, `query_only=ON`, somit blockiert der Live-Writer (WAL) nicht.

Unterstützte Ressourcen:
- `probabilities`: `ts, prob_short, prob_neutral, prob_long, trend, raw_signal, final_signal, threshold` (DESC, limit<=500)
- `trades`: `ts, event, side, entry_price, close_price, size, pnl_pct, pnl_abs, symbol, note` (DESC, limit<=500)

## App-Hinweis für Agenten
- Verbinde als Consumer mit `CONSUMER_TOKEN`.
- Nach `auth_ok` sende `get`-Requests wie oben. Responses kommen mit gleichem `request_id`.
- Verwende `wss://` und halte eine offene Verbindung (Ping/Pong handled durch `websockets`).
