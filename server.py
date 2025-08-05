import asyncio
import json
import logging
from typing import Dict
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("SuperPublifyer")

app = FastAPI()

# Store connected clients: key = "username/project"
# Value = {
#    "ws": WebSocket,
#    "queue": asyncio.Queue[str],
#    "tcp_server": TCPServer info if tcp,
#    "tcp_clients": set of active TCP connections
# }
clients: Dict[str, dict] = {}

# --- TCP proxy helpers ---

async def tcp_proxy_handler(local_host, local_port, remote_reader, remote_writer):
    try:
        reader, writer = await asyncio.open_connection(local_host, local_port)
        logger.info(f"TCP proxy connected to local {local_host}:{local_port}")

        async def pipe(reader, writer):
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    writer.write(data)
                    await writer.drain()
            except Exception as e:
                logger.error(f"TCP pipe error: {e}")
            finally:
                writer.close()
                await writer.wait_closed()

        await asyncio.gather(
            pipe(remote_reader, writer),
            pipe(reader, remote_writer)
        )
    except Exception as e:
        logger.error(f"TCP proxy main error: {e}")
    finally:
        remote_writer.close()
        await remote_writer.wait_closed()

async def start_tcp_server(key: str, target_host: str, target_port: int):
    try:
        async def handle_tcp_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            logger.info(f"TCP client connected for {key}")
            try:
                await tcp_proxy_handler(target_host, target_port, reader, writer)
            except Exception as e:
                logger.error(f"TCP client error: {e}")

        server = await asyncio.start_server(handle_tcp_client, host="0.0.0.0", port=0)
        sock = server.sockets[0]
        port = sock.getsockname()[1]
        logger.info(f"TCP proxy server started for {key} on port {port}")
        return server, port
    except Exception as e:
        logger.error(f"Failed to start TCP proxy server: {e}")
        raise

# --- WebSocket Endpoint ---

@app.websocket("/ws/{username}/{project}")
async def websocket_endpoint(websocket: WebSocket, username: str, project: str):
    key = f"{username}/{project}"
    await websocket.accept()

    # Create message queue for this client
    message_queue = asyncio.Queue()
    clients[key] = {
        "ws": websocket,
        "queue": message_queue,
        "tcp_server": None,
        "tcp_port": None,
    }

    logger.info(f"🟢 WebSocket client connected: {key}")

    try:
        while True:
            # Only one coroutine reading at a time, so no recv clash
            msg = await websocket.receive_text()
            await message_queue.put(msg)
    except WebSocketDisconnect:
        logger.info(f"🔴 WebSocket client disconnected: {key}")
        # Close TCP server if exists
        if clients[key]["tcp_server"]:
            clients[key]["tcp_server"].close()
            await clients[key]["tcp_server"].wait_closed()
        del clients[key]

# --- Registration HTTP Endpoint ---

from pydantic import BaseModel, constr

CREDENTIAL_REGEX = r"^[a-zA-Z0-9_-]{3,50}$"
PROJECT_REGEX = r"^[a-zA-Z0-9_-]{3,50}$"

class RegisterRequest(BaseModel):
    username: constr(regex=CREDENTIAL_REGEX)
    password: constr(regex=CREDENTIAL_REGEX)  # In prod: hash it!
    project_name: constr(regex=PROJECT_REGEX)
    local_url: str  # host:port (no protocol)
    protocol: str  # tcp/http/https

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
    if protocol not in ["tcp", "http", "https"]:
        raise HTTPException(status_code=400, detail="Protocol must be tcp/http/https")

    clients[key]["registered"] = True
    clients[key]["local_url"] = req.local_url
    clients[key]["protocol"] = protocol

    tcp_port = None
    if protocol == "tcp":
        # Start TCP proxy server and save it
        server, tcp_port = await start_tcp_server(key, host, port)
        clients[key]["tcp_server"] = server
        clients[key]["tcp_port"] = tcp_port

    logger.info(f"Registered {key} protocol={protocol} local_url={req.local_url}")

    return {
        "status": "success",
        "tcp_port": tcp_port,
        "message": "Registration successful"
    }

# --- HTTP Proxy ---

import httpx

@app.api_route("/{username}/{project}/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS","HEAD"])
async def http_proxy(username: str, project: str, path: str, request: Request):
    key = f"{username}/{project}"
    client = clients.get(key)
    if not client or not client.get("registered"):
        raise HTTPException(404, "Project not registered or WebSocket not connected")

    protocol = client["protocol"]
    if protocol == "tcp":
        # TCP accessed via TCP port, not HTTP
        raise HTTPException(400, "TCP projects not accessible via HTTP")

    ws = client["ws"]
    queue = client["queue"]

    # Prepare data to send to client via WS for forwarding
    req_body = await request.body()
    data = {
        "method": request.method,
        "path": path,
        "headers": dict(request.headers),
        "body": req_body.decode("utf-8", errors="ignore"),
        "query": str(request.query_params)
    }

    # Send request to client
    await ws.send_json(data)

    try:
        # Await response message from client (JSON)
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
