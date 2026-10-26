# ~/peezy_bot/telemetry_server.py
import asyncio
import json
from typing import Any, Dict, List

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Pezzy Live Wire Telemetry Bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update to your Vercel domain in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        for connection in list(self.active_connections):
            try:
                await connection.send_text(json.dumps(message))
            except Exception:
                pass


manager = ConnectionManager()


@app.get("/health")
async def health_check() -> Dict[str, Any]:
    return {
        "status": "ONLINE",
        "service": "Pezzy Live Wire Telemetry Bridge",
        "connections": len(manager.active_connections),
    }


@app.websocket("/ws/telemetry")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            # Keep alive ping-pong loop
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


# Call this function from your bot runner whenever an order fires or sim updates
async def push_live_update(event_type: str, payload: Dict[str, Any]) -> None:
    await manager.broadcast({
        "event": event_type,
        "data": payload,
    })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
