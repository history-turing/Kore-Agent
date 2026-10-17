from __future__ import annotations

import hashlib
import mimetypes
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..config import Settings
from ..database import Database
from ..rpc import RpcFault
from ..util import dumps, loads, now, uid

GetRun = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
Broadcast = Callable[[str, dict[str, Any]], Awaitable[None]]


class ArtifactService:
    """Copies, records, and serves files or URLs produced by a run."""

    def __init__(
        self,
        db: Database,
        *,
        settings: Callable[[], Settings],
        broadcast: Callable[[], Broadcast],
        get_run: GetRun,
    ):
        self.db = db
        self._settings = settings
        self._broadcast = broadcast
        self.get_run = get_run

    async def publish(self, *, path: Path, title: str, description: str | None, run_id: str) -> dict[str, Any]:
        settings = self._settings()
        path = path.resolve()
        path.relative_to(settings.workspace_dir.resolve())
        if not path.is_file():
            raise FileNotFoundError(path)
        if path.stat().st_size > 100 * 1024 * 1024:
            raise ValueError("Artifact exceeds the 100 MiB limit")
        aid, stamp = uid(), now()
        managed = settings.data_dir / "artifacts" / aid / path.name
        managed.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, managed)
        data = managed.read_bytes()
        run = (await self.get_run({"run_id": run_id}))["run"]
        artifact = {
            "id": aid,
            "kind": "file",
            "title": title,
            "description": description,
            "filename": path.name,
            "mime_type": mimetypes.guess_type(path.name)[0],
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "run_id": run_id,
            "conversation_id": run["conversation_id"],
            "task_id": None,
            "source_url": None,
            "created_at": stamp,
            "storage_path": str(managed),
        }
        await self.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (aid, dumps(artifact), stamp))
        public = {k: v for k, v in artifact.items() if k != "storage_path"}
        await self._broadcast()("artifact.created", public)
        return public

    async def publish_url(self, *, url: str, title: str, description: str | None, run_id: str) -> dict[str, Any]:
        parsed = urlsplit(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Artifact URL must use http or https")
        aid, stamp = uid(), now()
        run = (await self.get_run({"run_id": run_id}))["run"]
        artifact = {
            "id": aid,
            "kind": "url",
            "title": title.strip() or url,
            "description": description,
            "filename": None,
            "mime_type": None,
            "size_bytes": 0,
            "sha256": None,
            "run_id": run_id,
            "conversation_id": run["conversation_id"],
            "task_id": None,
            "source_url": url.strip(),
            "created_at": stamp,
            "storage_path": None,
        }
        await self.db.execute("INSERT INTO artifacts VALUES(?,?,?)", (aid, dumps(artifact), stamp))
        public = {key: value for key, value in artifact.items() if key != "storage_path"}
        await self._broadcast()("artifact.created", public)
        return public

    async def list(self, params: dict[str, Any]) -> dict[str, Any]:
        clauses: list[str] = []
        values: list[Any] = []
        for key in ("run_id", "conversation_id"):
            if params.get(key):
                clauses.append(f"json_extract(data_json, '$.{key}')=?")
                values.append(str(params[key]))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(params.get("limit", 50)), 500)))
        rows = await self.db.fetchall(
            f"SELECT data_json FROM artifacts{where} ORDER BY created_at DESC LIMIT ?",
            tuple(values),
        )
        items = [loads(r["data_json"], {}) for r in rows]
        return {"artifacts": [{k: v for k, v in a.items() if k != "storage_path"} for a in items]}

    async def get(self, params: dict[str, Any]) -> dict[str, Any]:
        aid = str(params.get("id") or params.get("artifact_id") or "")
        row = await self.db.fetchone("SELECT data_json FROM artifacts WHERE id=?", (aid,))
        if not row:
            raise RpcFault(-32000, "Artifact not found")
        return {"artifact": {k: v for k, v in loads(row["data_json"], {}).items() if k != "storage_path"}}

    async def path(self, aid: str) -> Path | None:
        settings = self._settings()
        row = await self.db.fetchone("SELECT data_json FROM artifacts WHERE id=?", (aid,))
        if not row:
            return None
        storage_path = loads(row["data_json"], {}).get("storage_path")
        if not storage_path:
            return None
        path = Path(storage_path).resolve()
        try:
            path.relative_to((settings.data_dir / "artifacts").resolve())
        except ValueError:
            return None
        return path if path.is_file() else None
