import os
import re
import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status
from fastapi.responses import Response, StreamingResponse
import httpx
import uvicorn
from pydantic import BaseModel, constr

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("SuperPublifyer")

# Validation regex for credentials
CREDENTIAL_REGEX = r"^[a-zA-Z0-9_-]{3,50}$"
PROJECT_REGEX = r"^[a-zA-Z0-9_-]{3,50}$"

# Global storage for routing information
routes = {}
tcp_proxies = {}

class RegistrationRequest(BaseModel):
    username: constr(regex=CREDENTIAL_REGEX)
    password: constr(regex=CREDENTIAL_REGEX)
    project_name: constr(regex=PROJECT_REGEX)
    local_url: str
    protocol: str

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Context manager for resource management"""
    yield
    # Cleanup TCP proxies on shutdown
    for proxy in tcp_proxies.values():
        proxy.close()

app = FastAPI(lifespan=lifespan)

def validate_registration(data: RegistrationRequest):
    """Validate registration parameters"""
    if not re.match(CREDENTIAL_REGEX, data.username):
        raise ValueError("Invalid username format")
    if not re.match(CREDENTIAL_REGEX, data.password):
        raise ValueError("Invalid password format")
    if not re.match(PROJECT_REGEX, data.project_name):
        raise ValueError("Invalid project name format")
    if data.protocol.lower() not in ["tcp", "http", "https"]:
        raise ValueError("Invalid protocol. Must be TCP, HTTP, or HTTPS")
    
    # Validate local URL format
    if "://" in data.local_url:
        raise ValueError("Local URL should not include protocol (e.g., use 'localhost:8000' instead of 'http://localhost:8000')")
    if not re.match(r"^[\w.-]+(:\d+)?$", data.local_url):
        raise ValueError("Invalid local URL format")

@app.post("/register")
async def register(request: RegistrationRequest):
    """Endpoint to register new forwarding routes"""
    try:
        validate_registration(request)
        key = f"{request.username}/{request.project_name}"
        
        if key in routes:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Project already registered"
            )
        
        # Store route information
        routes[key] = {
            "local_url": request.local_url,
            "protocol": request.protocol.lower(),
            "password": request.password  # In production, use hashed passwords
        }
        
        # Start TCP proxy if needed
        if request.protocol.lower() == "tcp":
            await start_tcp_proxy(key, request.local_url)
        
        logger.info(f"Registered: {key} -> {request.local_url} ({request.protocol})")
        return {"status": "success", "message": "Registration successful"}
    
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

async def start_tcp_proxy(route_key: str, target_url: str):
    """Start TCP proxy server for a registered route"""
    try:
        host, port = target_url.split(":")
        port = int(port)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid TCP target format. Use 'host:port'"
        )
    
    async def handle_tcp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle TCP proxy connections"""
        try:
            # Connect to target server
            target_reader, target_writer = await asyncio.open_connection(host, port)
            
            # Proxy data bidirectionally
            await asyncio.gather(
                pipe_data(reader, target_writer),
                pipe_data(target_reader, writer)
            )
        except Exception as e:
            logger.error(f"TCP Proxy error: {str(e)}")
        finally:
            writer.close()
            await writer.wait_closed()
            if 'target_writer' in locals():
                target_writer.close()
                await target_writer.wait_closed()
    
    # Start TCP server
    server = await asyncio.start_server(
        handle_tcp,
        host='0.0.0.0',
        port=0  # Automatically assign available port
    )
    
    # Store server and port information
    tcp_port = server.sockets[0].getsockname()[1]
    tcp_proxies[route_key] = {
        "server": server,
        "port": tcp_port
    }
    
    logger.info(f"TCP proxy started for {route_key} on port {tcp_port}")

async def pipe_data(reader, writer):
    """Pipe data between streams"""
    try:
        while not reader.at_eof():
            data = await reader.read(4096)
            if data:
                writer.write(data)
                await writer.drain()
            else:
                break
    except ConnectionError:
        pass
    finally:
        writer.close()

@app.api_route("/{username}/{project_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy_request(username: str, project_name: str, path: str, request: Request):
    """Proxy HTTP/HTTPS requests to registered services"""
    route_key = f"{username}/{project_name}"
    
    if route_key not in routes:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    route = routes[route_key]
    
    # Handle TCP routes differently
    if route["protocol"] == "tcp":
        return Response(
            content="TCP route cannot be accessed via HTTP",
            status_code=status.HTTP_400_BAD_REQUEST
        )
    
    # Build target URL
    target_url = f"http://{route['local_url']}/{path}"
    if request.query_params:
        target_url += f"?{request.query_params}"
    
    # Forward request
    async with httpx.AsyncClient() as client:
        try:
            # Prepare headers (remove hop-by-hop headers)
            headers = {
                k: v for k, v in request.headers.items()
                if k.lower() not in [
                    "host", "connection", "content-length"
                ]
            }
            
            # Forward request
            response = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=await request.body(),
                timeout=30.0
            )
            
            # Return streaming response
            return StreamingResponse(
                content=response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers)
            )
        except httpx.ConnectError:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Backend service unavailable"
            )
        except Exception as e:
            logger.error(f"Proxy error: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal proxy error"
            )

if __name__ == "__main__":
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        log_level="info"
    )
