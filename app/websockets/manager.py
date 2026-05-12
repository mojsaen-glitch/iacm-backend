from typing import Dict, List
from fastapi import WebSocket
import json


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""

    def __init__(self):
        # company_id -> list of connections
        self._company_connections: Dict[str, List[WebSocket]] = {}
        # user_id -> WebSocket
        self._user_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_id: str, company_id: str):
        await websocket.accept()
        self._user_connections[user_id] = websocket
        if company_id not in self._company_connections:
            self._company_connections[company_id] = []
        self._company_connections[company_id].append(websocket)

    def disconnect(self, user_id: str, company_id: str, websocket: WebSocket):
        self._user_connections.pop(user_id, None)
        if company_id in self._company_connections:
            try:
                self._company_connections[company_id].remove(websocket)
            except ValueError:
                pass

    async def send_to_user(self, user_id: str, event: str, data: dict):
        ws = self._user_connections.get(user_id)
        if ws:
            try:
                await ws.send_text(json.dumps({"event": event, "data": data}))
            except Exception:
                self._user_connections.pop(user_id, None)

    async def broadcast_to_company(self, company_id: str, event: str, data: dict):
        connections = self._company_connections.get(company_id, [])
        dead = []
        for ws in connections:
            try:
                await ws.send_text(json.dumps({"event": event, "data": data}))
            except Exception:
                dead.append(ws)
        for ws in dead:
            connections.remove(ws)


# Singleton
ws_manager = ConnectionManager()
