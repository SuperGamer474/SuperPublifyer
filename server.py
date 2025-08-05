import asyncio
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import Response
import uvicorn

app = FastAPI()

clients = {}  # key: username/project, value: websocket

@app.websocket("/ws/{username}/{project}")
async def websocket_endpoint(websocket: WebSocket, username: str, project: str):
    await websocket.accept()
    key = f"{username}/{project}"
    clients[key] = websocket
    print(f"🟢 Client connected: {key}")
    try:
        while True:
            # Just keep connection alive, actual messages handled elsewhere
            msg = await websocket.receive_text()  # or receive_bytes()
            # Could process control messages here if needed
    except WebSocketDisconnect:
        print(f"🔴 Client disconnected: {key}")
        clients.pop(key, None)

async def tcp_proxy_handler(reader, writer, client_ws):
    try:
        async def tcp_to_ws():
            while True:
                data = await reader.read(1024)
                if not data:
                    break
                await client_ws.send_bytes(data)

        async def ws_to_tcp():
            while True:
                data = await client_ws.receive_bytes()
                if not data:
                    break
                writer.write(data)
                await writer.drain()

        await asyncio.gather(tcp_to_ws(), ws_to_tcp())
    except Exception as e:
        print(f"TCP proxy error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()

@app.post("/tcp_start/{username}/{project}")
async def tcp_start(username: str, project: str):
    key = f"{username}/{project}"
    client_ws = clients.get(key)
    if not client_ws:
        raise HTTPException(404, "Client not connected")

    server = await asyncio.start_server(
        lambda r, w: tcp_proxy_handler(r, w, client_ws),
        host="0.0.0.0",
        port=0
    )
    port = server.sockets[0].getsockname()[1]
    print(f"TCP proxy started on port {port} for {key}")

    asyncio.create_task(server.serve_forever())
    return {"tcp_port": port}

@app.api_route("/{username}/{project}/{path:path}", methods=["GET","POST","PUT","DELETE","PATCH","OPTIONS","HEAD"])
async def http_proxy(username: str, project: str, path: str, request: Request):
    key = f"{username}/{project}"
    client_ws = clients.get(key)
    if not client_ws:
        raise HTTPException(404, "Client not connected")

    # Receive HTTP request details
    body = await request.body()
    headers = dict(request.headers)
    req_data = {
        "method": request.method,
        "path": path,
        "headers": headers,
        "body": body.decode("utf-8", errors="ignore")
    }

    # Send request details to client
    await client_ws.send_json(req_data)
    # Wait for client response
    response_data = await client_ws.receive_json()
    return Response(
        content=response_data.get("body", ""),
        status_code=response_data.get("status_code", 200),
        headers=response_data.get("headers", {})
    )

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0")
