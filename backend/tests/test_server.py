from fastapi.testclient import TestClient

from kore_next.application import Application
from kore_next.config import Settings
from kore_next.server import create_app


def test_health_and_json_rpc(tmp_path):
    application = Application(Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"))
    with TestClient(create_app(application)) as client:
        assert client.get("/health").json()["status"] == "ok"
        with client.websocket_connect("/rpc") as ws:
            ws.send_json({"jsonrpc": "2.0", "id": 1, "method": "system.info", "params": {}})
            response = ws.receive_json()
            assert response["id"] == 1
            assert response["result"]["name"] == "KoreAgent Next"


def test_rpc_errors_are_structured(tmp_path):
    application = Application(Settings(data_dir=tmp_path / "data", workspace_dir=tmp_path / "workspace"))
    with TestClient(create_app(application)) as client:
        with client.websocket_connect("/rpc") as ws:
            ws.send_json({"jsonrpc": "2.0", "id": "x", "method": "missing", "params": {}})
            assert ws.receive_json()["error"]["code"] == -32601
            ws.send_text("not json")
            assert ws.receive_json()["error"]["code"] == -32700
