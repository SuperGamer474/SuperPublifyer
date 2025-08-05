import os
import re
import uuid
import asyncio
import base64
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, status
from fastapi.responses import Response
import uvicorn

# Regex pattern for validating credentials
CREDENTIAL_PATTERN = r"^[a-zA-Z0-9_-]{3,50}$"

app = FastAPI()

# Store connected clients and pending request futures
clients: dict[str, WebSocket] = {}
pending: dict[str, asyncio.Future] = {}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        msg = await websocket.receive_json()
        if msg.get("type") != "register":
            await websocket.close(code=1008)
            return
        key = f"{msg['username']}/{msg['project']}"
        clients[key] = websocket
        print(f"Client registered: {key}")
        # Keep connection alive
        while True:
            resp = await websocket.receive_json()
            if resp.get("type") == "response":
                req_id = resp.get("id")
                fut = pending.pop(req_id, None)
                if fut:
                    body = base64.b64decode(resp.get("body", ""))
                    fut.set_result({
                        "status": resp.get("status", 500),
                        "headers": resp.get("headers", {}),
                        "body": body
                    })
    except WebSocketDisconnect:
        # Clean up on disconnect
        to_remove = [k for k, ws in clients.items() if ws == websocket]
        for k in to_remove:
            clients.pop(k)
            print(f"Client disconnected: {k}")

@app.api_route("/{username}/{project}/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS","HEAD"])
async def proxy(request: Request, username: str, project: str, path: str):
    key = f"{username}/{project}"
    ws = clients.get(key)
    if not ws:
        raise HTTPException(status_code=503, detail="Client not connected")

    # Build request envelope
    req_id = str(uuid.uuid4())
    data = {
        "type": "request",
        "id": req_id,
        "method": request.method,
        "path": path,
        "headers": dict(request.headers),
        "body": base64.b64encode(await request.body()).decode()
    }

    # Send to client and wait for reply
    await ws.send_json(data)
    fut = asyncio.get_event_loop().create_future()
    pending[req_id] = fut
    try:
        resp = await asyncio.wait_for(fut, timeout=30)
    except asyncio.TimeoutError:
        pending.pop(req_id, None)
        raise HTTPException(status_code=504, detail="Client timed out")

    return Response(
        content=resp["body"],
        status_code=resp["status"],
        headers=resp["headers"]
    )

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
