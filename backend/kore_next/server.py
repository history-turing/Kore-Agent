from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from . import __version__
from .application import Application, RpcFault
from .config import Settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("koreagent_next.server")
HEX_ID = re.compile(r"^[0-9a-f]{32}$")
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


class Hub:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, method: str, params: dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        dead: list[WebSocket] = []
        async with self.lock:
            clients = list(self.clients)
        for client in clients:
            try:
                await client.send_json(payload)
            except Exception:
                dead.append(client)
        for client in dead:
            await self.remove(client)


def create_app(application: Application | None = None) -> FastAPI:
    application = application or Application()
    hub = Hub()
    application.broadcast = hub.broadcast

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await application.start()
        logger.info("KoreAgent Next Host started")
        try:
            yield
        finally:
            await application.close()

    app = FastAPI(title="KoreAgent Next Host", version=__version__, lifespan=lifespan)
    app.state.application, app.state.hub = application, hub

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "provider": application.settings.provider,
            "model": application.settings.model,
            "version": __version__,
        }

    def require_local(request: Request) -> None:
        host = request.client.host if request.client else ""
        if host not in LOOPBACK:
            raise HTTPException(403, "forbidden")

    @app.get("/artifacts/{artifact_id}/content")
    async def artifact_content(artifact_id: str, request: Request) -> Response:
        require_local(request)
        if not HEX_ID.fullmatch(artifact_id):
            raise HTTPException(404, "not found")
        path = await application.artifact_path(artifact_id)
        if not path:
            raise HTTPException(404, "not found")
        result = await application.artifact_get({"id": artifact_id})
        artifact = result["artifact"]
        return FileResponse(
            path,
            media_type=artifact.get("mime_type") or "application/octet-stream",
            filename=artifact.get("filename") or "artifact",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/computer/screenshots/{observation_id}.png")
    async def computer_screenshot(observation_id: str, request: Request) -> Response:
        require_local(request)
        if not HEX_ID.fullmatch(observation_id):
            raise HTTPException(404, "not found")
        root = (application.settings.data_dir / "screenshots").resolve()
        path = (root / f"{observation_id}.png").resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise HTTPException(404, "not found") from None
        if not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    async def handle(ws: WebSocket, payload: Any) -> None:
        request_id: str | int | None = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32600, "message": "Invalid Request"},
                }
            )
            return
        method, params = payload.get("method"), payload.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            await ws.send_json(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32600, "message": "Invalid Request"},
                }
            )
            return
        fn = application.methods().get(method)
        if fn is None:
            if request_id is not None:
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    }
                )
            return
        try:
            result = await fn(params)
            if request_id is not None:
                await ws.send_json({"jsonrpc": "2.0", "id": request_id, "result": result})
        except RpcFault as exc:
            if request_id is not None:
                error = {"code": exc.code, "message": exc.message}
                if exc.data is not None:
                    error["data"] = exc.data
                await ws.send_json({"jsonrpc": "2.0", "id": request_id, "error": error})
        except asyncio.CancelledError:
            if request_id is not None:
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32001, "message": "Request cancelled"},
                    }
                )
        except Exception:
            logger.exception("RPC method %s failed", method)
            if request_id is not None:
                await ws.send_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32603, "message": "Internal error"},
                    }
                )

    @app.websocket("/rpc")
    async def rpc(ws: WebSocket) -> None:
        await ws.accept()
        await hub.add(ws)
        tasks: set[asyncio.Task[Any]] = set()
        try:
            while True:
                text = await ws.receive_text()
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    await ws.send_json(
                        {
                            "jsonrpc": "2.0",
                            "id": None,
                            "error": {"code": -32700, "message": "Parse error"},
                        }
                    )
                    continue
                task = asyncio.create_task(handle(ws, payload))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except WebSocketDisconnect:
            pass
        finally:
            await hub.remove(ws)
            for task in tasks:
                task.cancel()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="KoreAgent Next local host")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    settings = Settings()
    uvicorn.run(
        create_app(Application(settings)),
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
