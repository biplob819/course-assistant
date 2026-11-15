"""A fake vector store whose every answer is one of the OpenAI SDK's own models (L-9)."""

from __future__ import annotations

from typing import Any

import httpx2 as httpx
import openai
from openai.types import FileDeleted, FileObject
from openai.types.vector_stores import VectorStoreFile, VectorStoreFileDeleted

WRITES = frozenset({"upload", "attach", "update", "detach", "delete_file"})


def status_error(code: int, path: str = "/v1/files") -> openai.APIStatusError:
    req = httpx.Request("POST", "https://api.openai.com" + path)
    resp = httpx.Response(code, request=req, json={"error": {"message": "x"}})
    cls = {404: openai.NotFoundError, 500: openai.InternalServerError}.get(
        code, openai.APIStatusError
    )
    return cls(
        "boom sk-proj-SECRETSECRETSECRETSECRET", response=resp, body=None
    )  # secret-scan: allow


class FakeVectorStore:
    """In-memory files and one vector store. `final` maps a filename to the status its
    processing ends in (default "completed"); each file reports in_progress once first."""

    def __init__(self, vector_store_id: str = "vs_test", final: dict[str, str] | None = None,
                 fail: dict[str, BaseException] | None = None) -> None:  # fmt: skip
        self.vector_store_id = vector_store_id
        self.final = dict(final or {})
        self.fail = dict(fail or {})  # method name -> exception raised on its next call
        self.files: dict[str, FileObject] = {}
        self.attached: dict[str, VectorStoreFile] = {}
        self.polls: dict[str, int] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.hidden: set[str] = set()  # attached ids the listing leaves out
        self.foreign: list[str] = []  # ids attached by someone else, shown in the listing
        self._n = 0

    # helpers for tests
    def writes(self) -> list[tuple[str, tuple[Any, ...]]]:
        return [c for c in self.calls if c[0] in WRITES]

    def names(self, method: str) -> list[tuple[Any, ...]]:
        return [args for name, args in self.calls if name == method]

    def _record(self, method: str, *args: Any) -> None:
        self.calls.append((method, args))
        if method in self.fail:
            raise self.fail.pop(method)

    def _vs_file(self, file_id: str, status: str, attributes: dict[str, Any]) -> VectorStoreFile:
        return VectorStoreFile(id=file_id, created_at=1_763_190_000, object="vector_store.file",
                               status=status,  # type: ignore[arg-type]
                               usage_bytes=self.files[file_id].bytes,
                               vector_store_id=self.vector_store_id, attributes=attributes,
                               last_error=None)  # fmt: skip

    # the VectorStoreAPI protocol
    def upload(self, filename: str, data: bytes) -> FileObject:
        self._record("upload", filename, data)
        self._n += 1
        f = FileObject(id=f"file-{self._n:04d}", bytes=len(data), created_at=1_763_190_000,
                       filename=filename, object="file", purpose="assistants",
                       status="processed")  # fmt: skip
        self.files[f.id] = f
        return f

    def attach(self, vector_store_id: str, file_id: str,
               attributes: dict[str, str | int]) -> VectorStoreFile:  # fmt: skip
        self._record("attach", vector_store_id, file_id, dict(attributes))
        vsf = self._vs_file(file_id, "in_progress", dict(attributes))
        self.attached[file_id] = vsf
        return vsf

    def retrieve(self, vector_store_id: str, file_id: str) -> VectorStoreFile:
        self._record("retrieve", vector_store_id, file_id)
        vsf = self.attached[file_id]
        self.polls[file_id] = self.polls.get(file_id, 0) + 1
        if self.polls[file_id] >= 2:
            status = self.final.get(self.files[file_id].filename, "completed")
            vsf = self._vs_file(file_id, status, dict(vsf.attributes or {}))
            self.attached[file_id] = vsf
        return vsf

    def update(self, vector_store_id: str, file_id: str,
               attributes: dict[str, str | int]) -> VectorStoreFile:  # fmt: skip
        self._record("update", vector_store_id, file_id, dict(attributes))
        old = self.attached[file_id]
        vsf = self._vs_file(file_id, old.status, dict(attributes))
        self.attached[file_id] = vsf
        return vsf

    def detach(self, vector_store_id: str, file_id: str) -> VectorStoreFileDeleted:
        self._record("detach", vector_store_id, file_id)
        if file_id not in self.attached:
            raise status_error(404, f"/v1/vector_stores/{vector_store_id}/files/{file_id}")
        del self.attached[file_id]
        return VectorStoreFileDeleted(id=file_id, deleted=True, object="vector_store.file.deleted")

    def delete_file(self, file_id: str) -> FileDeleted:
        self._record("delete_file", file_id)
        if file_id not in self.files:
            raise status_error(404, f"/v1/files/{file_id}")
        del self.files[file_id]
        return FileDeleted(id=file_id, deleted=True, object="file")

    def list_files(self, vector_store_id: str) -> list[VectorStoreFile]:
        self._record("list_files", vector_store_id)
        out = [v for k, v in self.attached.items() if k not in self.hidden]
        for fid in self.foreign:
            out.append(VectorStoreFile(id=fid, created_at=1, object="vector_store.file",
                                       status="completed", usage_bytes=1,
                                       vector_store_id=vector_store_id))  # fmt: skip
        return out
