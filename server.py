import re
import asyncio
import json
import logging
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import Response, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from pydantic import BaseModel, constr

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SuperPublifyer")

app = FastAPI()

# Serve static files (including 404.html) from the "static" directory
app.mount("/static", StaticFiles(directory="static"), name="static")

# Store connected clients: key = project_name
# Value = {
#    "ws": WebSocket,
#    "queue": asyncio.Queue[str],
#    "registered": bool,
#    "local_url": str,
#    "protocol": str
# }
clients: Dict[str, dict] = {}

PROJECT_REGEX = r"^[a-zA-Z0-9_-]{3,50}$"

class RegisterRequest(BaseModel):
    project_name: constr(regex=PROJECT_REGEX)
    local_url: str  # host:port (no protocol)
    protocol: constr(regex="^(http|https)$")

@app.websocket("/ws/{project}")
async def websocket_endpoint(websocket: WebSocket, project: str):
    # Reject duplicate project names
    if project in clients:
        await websocket.accept()
        await websocket.send_json({"error": "Duplicate project name connected"})
        await websocket.close(code=1000)
        logger.warning(f"Rejected duplicate WS connection for project: {project}")
        return

    await websocket.accept()
    message_queue: asyncio.Queue = asyncio.Queue()

    clients[project] = {
        "ws": websocket,
        "queue": message_queue,
        "registered": False,
        "local_url": None,
        "protocol": None,
    }

    logger.info(f"🟢 WebSocket client connected: {project}")
    try:
        while True:
            msg = await websocket.receive_text()
            await message_queue.put(msg)
    except WebSocketDisconnect:
        logger.info(f"🔴 WebSocket client disconnected: {project}")
        del clients[project]

@app.post("/register")
async def register(req: RegisterRequest):
    project = req.project_name
    if project not in clients:
        raise HTTPException(status_code=400, detail="WebSocket client not connected yet")
    if clients[project]["registered"]:
        raise HTTPException(status_code=409, detail="Project name taken")

    if ":" not in req.local_url:
        raise HTTPException(status_code=400, detail="Invalid local_url, must be host:port")
    host, port_str = req.local_url.split(":")
    try:
        int(port_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Port must be an integer")

    clients[project]["registered"] = True
    clients[project]["local_url"] = req.local_url
    clients[project]["protocol"] = req.protocol

    logger.info(f"Registered project={project} protocol={req.protocol} local_url={req.local_url}")
    return {"status": "success", "message": "Registration successful"}

@app.api_route("/{project}/{path:path}", methods = [
    "ACL",
    "BASELINE-CONTROL",
    "BIND",
    "CHECKIN",
    "CHECKOUT",
    "CONNECT",
    "COPY",
    "DELETE",
    "GET",
    "HEAD",
    "LABEL",
    "LINK",
    "LOCK",
    "MERGE",
    "MKACTIVITY",
    "MKCALENDAR",
    "MKCOL",
    "MKREDIRECTREF",
    "MKWORKSPACE",
    "MOVE",
    "OPTIONS",
    "ORDERPATCH",
    "PATCH",
    "POST",
    "PRI",
    "PROPFIND",
    "PROPPATCH",
    "PUT",
    "REBIND",
    "REPORT",
    "SEARCH",
    "TRACE",
    "UNBIND",
    "UNCHECKOUT",
    "UNLINK",
    "UNLOCK",
    "UPDATE",
    "UPDATEREDIRECTREF",
    "VERSION-CONTROL",
    "*"
])
async def http_proxy(project: str, path: str, request: Request):
    client = clients.get(project)
    if not client or not client["registered"]:
        return FileResponse("static/404.html", status_code=404)

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

    await ws.send_json(data)

    try:
        response_msg = await asyncio.wait_for(queue.get(), timeout=30)
        response_data = json.loads(response_msg)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Timeout waiting for client response")

    content = response_data.get("body", "")
    status_code = response_data.get("status_code", 200)
    headers = response_data.get("headers", {})

    # --- rewrite URLs in HTML safely ---
    if headers.get("content-type", "").startswith("text/html"):
        app_name = project
        # Only match local paths starting with / but not external links
        pattern = r'(href|src|action)=["\']/(?!https?:|//)([^"\']*)["\']'
        content = re.sub(pattern, rf'\1="/{app_name}/\2"', content)

        # Remove Content-Length to avoid mismatch
        headers.pop("content-length", None)

    return Response(
        content=content,
        status_code=status_code,
        headers=headers
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0")
