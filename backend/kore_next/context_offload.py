from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .database import Database
from .util import now, uid


class ContextOffloadStore:
    """Content-addressed storage for large tool results."""

    def __init__(self, db: Database, root: Path, *, threshold: int = 64_000, preview_chars: int = 2_000):
        self.db = db
        self.root = root
        self.threshold = threshold
        self.preview_chars = preview_chars

    async def archive(self, text: str) -> dict[str, Any] | None:
        if len(text) <= self.threshold:
            return None
        data = text.encode("utf-8")
        sha256 = hashlib.sha256(data).hexdigest()
        target = self.root / sha256[:2] / f"{sha256}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.is_file():
            temporary = target.with_suffix(f".{uid()}.tmp")
            temporary.write_bytes(data)
            if temporary.stat().st_size != len(data):
                temporary.unlink(missing_ok=True)
                raise OSError("offload write verification failed")
            temporary.replace(target)
        record = {
            "sha256": sha256,
            "size_bytes": len(data),
            "storage_path": str(target),
            "mime_type": "text/plain",
            "preview": text[: self.preview_chars],
            "created_at": now(),
        }
        await self.db.execute(
            "INSERT OR IGNORE INTO context_offloads(sha256,size_bytes,storage_path,mime_type,preview,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                record["sha256"],
                record["size_bytes"],
                record["storage_path"],
                record["mime_type"],
                record["preview"],
                record["created_at"],
            ),
        )
        return record

    async def get(self, sha256: str) -> dict[str, Any]:
        row = await self.db.fetchone(
            "SELECT * FROM context_offloads WHERE sha256=?",
            (sha256,),
        )
        if not row:
            raise KeyError("offloaded result not found")
        return {
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "storage_path": row["storage_path"],
            "mime_type": row["mime_type"],
            "preview": row["preview"],
            "created_at": row["created_at"],
        }

    async def inspect(self, sha256: str) -> dict[str, Any]:
        record = await self.get(sha256)
        return {key: value for key, value in record.items() if key != "storage_path"}

    async def read(self, sha256: str, *, offset: int = 0, limit: int = 4_000) -> dict[str, Any]:
        record = await self.get(sha256)
        path = Path(record["storage_path"]).resolve()
        root = self.root.resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("offloaded result path escapes its store") from exc
        if not path.is_file():
            raise FileNotFoundError(path)
        offset = max(0, int(offset))
        limit = max(1, min(int(limit), 16_000))
        size_bytes = path.stat().st_size
        with path.open("rb") as stream:
            stream.seek(offset)
            chunk = stream.read(limit)
        return {
            "sha256": sha256,
            "offset": offset,
            "size_bytes": size_bytes,
            "content": chunk.decode("utf-8", errors="replace"),
            "truncated": offset + len(chunk) < size_bytes,
            "next_offset": offset + len(chunk),
        }

    def placeholder(self, record: dict[str, Any]) -> str:
        return (
            "[tool result archived; read it with tool_result_read]\n"
            f"sha256: {record['sha256']}\n"
            f"size_bytes: {record['size_bytes']}\n"
            f"preview:\n{record['preview']}"
        )
