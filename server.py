import asyncio
import json
import logging
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import Response
import uvicorn
from pydantic import BaseModel, constr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SuperPublifyer")

app = FastAPI()

# Store connected clients: key = "username/project"
# Value = {
#    "ws": WebSocket,
#    "queue": asyncio.Queue[str],
#    "registered": bool,
#    "local_url": str,
#    "protocol": str ("http" or "https")
# }
clients: Dict[str, dict] = {}

CREDENTIAL_REGEX = r"^[a-zA-Z0-9_-]{3,50}$"
PROJECT_REGEX = r"^[a-zA-Z0-9_-]{3,50}$"

class RegisterRequest(BaseModel):
    username: constr(regex=CREDENTIAL_REGEX)
    project_name: constr(regex=PROJECT_REGEX)
    local_url: str  # host:port (no protocol)
    protocol: str  # http/https

@app.websocket("/ws/{username}/{project}")
async def websocket_endpoint(websocket: WebSocket, username: str, project: str):
    key = f"{username}/{project}"
    await websocket.accept()
    message_queue = asyncio.Queue()

    clients[key] = {
        "ws": websocket,
        "queue": message_queue,
        "registered": False,
        "local_url": None,
        "protocol": None,
    }

    logger.info(f"🟢 WebSocket client connected: {key}")

    try:
        while True:
            msg = await websocket.receive_text()
            await message_queue.put(msg)
    except WebSocketDisconnect:
        logger.info(f"🔴 WebSocket client disconnected: {key}")
        del clients[key]

@app.post("/register")
async def register(req: RegisterRequest):
    key = f"{req.username}/{req.project_name}"

    if key not in clients:
        raise HTTPException(status_code=400, detail="WebSocket client not connected yet")

    if clients[key].get("registered"):
        raise HTTPException(status_code=409, detail="Project already registered")

    # Validate local_url format host:port
    if ":" not in req.local_url:
        raise HTTPException(status_code=400, detail="Invalid local_url, must be host:port")

    host, port_str = req.local_url.split(":")
    try:
        port = int(port_str)
    except:
        raise HTTPException(status_code=400, detail="Port must be an integer")

    protocol = req.protocol.lower()
    if protocol not in ["http", "https"]:
        raise HTTPException(status_code=400, detail="Protocol must be http or https")

    clients[key]["registered"] = True
    clients[key]["local_url"] = req.local_url
    clients[key]["protocol"] = protocol

    logger.info(f"Registered {key} protocol={protocol} local_url={req.local_url}")

    return {
        "status": "success",
        "message": "Registration successful"
    }

@app.api_route("/{username}/{project}/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS","HEAD"])
async def http_proxy(username: str, project: str, path: str, request: Request):
    key = f"{username}/{project}"
    client = clients.get(key)
    if not client or not client.get("registered"):
        raise HTTPException(404, "Project not registered or WebSocket not connected")

    ws = client["ws"]
    queue = client["queue"]

    req_body = await request.body()
    data = {
        "method": request.method,
        "path": path,
        "headers": dict(request.headers),
        "body": req_body.decode("utf-8", errors="ignore"),
        "query": str(request.query_params)
    }

    # Send request to client via WS
    await ws.send_json(data)

    try:
        # Wait for client response via WS (JSON)
        response_msg = await asyncio.wait_for(queue.get(), timeout=30)
        response_data = json.loads(response_msg)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Timeout waiting for client response")

    return Response(
        content=response_data.get("body", ""),
        status_code=response_data.get("status_code", 200),
        headers=response_data.get("headers", {})
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
