"""Storage for collected data: one key space on local disk or in S3 (PRD §8.7, §11.3).

Workers write through the :class:`Storage` protocol with keys from
:mod:`lookout_common.paths`, so the same code runs on the laptop (``LocalStorage``
rooted at ``$LOOKOUT_DATA_DIR``) and on Lambda (``S3Storage`` under a bucket prefix).

Guarantees:

- ``put_bytes`` and ``write_json`` are atomic: readers see the old object or the new
  one, never a partial file. ``put_bytes(..., if_absent=True)`` is a race-free create
  (used to never store anything twice). It returns False only when the key already
  exists (S3 412). S3's 409 ``ConditionalRequestConflict`` means a concurrent
  conditional write and is retried, since nothing may have been stored.
- ``append_jsonl`` adds whole lines and never glues a record onto a torn last line.
  Locally it is an ``flock``-ed ``O_APPEND`` write. S3 has no append, so it is a
  read-modify-write guarded by ``If-Match`` / ``If-None-Match`` and retried on conflict.
- Keys never have a segment that starts with ``.`` (:func:`~lookout_common.paths.validate_key`),
  and ``list_prefix`` skips such names in both backends (local temp files, ``.DS_Store``),
  so the two backends list the same keys.
- JSON is written as valid RFC 8259 text: NaN and ±Infinity become ``null``.
- ``lock(name)`` is a mutual-exclusion lock with the same name space in both
  backends (key ``state/locks/<name>.lock``). Locally it is ``fcntl.flock``, released by
  the OS if the process dies. In S3 it is an object created with ``If-None-Match: *``.
  It carries an expiry time: a waiter may take over an expired lock with a put
  conditioned on the stale lock's ETag (``If-Match``), and release is a delete
  conditioned on our own ETag. Expiry uses the writer's wall clock, so ``ttl_s``
  must comfortably exceed both the critical section and any clock skew.

There is deliberately no delete method: CLAUDE.md forbids deleting collected data.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import random
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from .jsonl import append_jsonl_many as _append_local
from .jsonl import dumps_json, encode_line, make_record
from .paths import check_data_dir, data_dir, state_key, validate_key
from .timeutil import isoformat_z, utcnow

__all__ = [
    "ENV_S3_BUCKET",
    "ENV_S3_PREFIX",
    "ENV_STORAGE",
    "KeyNotFoundError",
    "LocalStorage",
    "LockTimeoutError",
    "S3Storage",
    "Storage",
    "content_type_for",
    "lock_key",
    "storage_from_env",
]

ENV_STORAGE = "LOOKOUT_STORAGE"
ENV_S3_BUCKET = "LOOKOUT_S3_BUCKET"
ENV_S3_PREFIX = "LOOKOUT_S3_PREFIX"

log = logging.getLogger(__name__)

_MISSING: Any = object()
_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".txt": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})
#: The condition failed: the object exists (If-None-Match) or changed (If-Match).
_PRECONDITION_CODES = frozenset({"PreconditionFailed", "412"})
#: A concurrent conditional write was in progress; AWS says to retry the request.
_RETRY_CODES = frozenset({"ConditionalRequestConflict", "409"})
_CONFLICT_CODES = _PRECONDITION_CODES | _RETRY_CODES


class KeyNotFoundError(FileNotFoundError):
    """The key does not exist in this storage backend."""


class LockTimeoutError(TimeoutError):
    """The named lock was not acquired within the timeout."""


def content_type_for(key: str) -> str:
    """Return the media type implied by a key's extension (octet-stream if unknown)."""
    return _CONTENT_TYPES.get(PurePosixPath(key).suffix.lower(), "application/octet-stream")


def lock_key(name: str) -> str:
    """Return the storage key of a named lock: ``state/locks/<name>.lock``."""
    return state_key(f"locks/{name}.lock")


def _validate_prefix(prefix: str) -> str:
    if prefix == "":
        return prefix
    if "\\" in prefix or "\x00" in prefix or prefix.startswith("/"):
        raise ValueError(f"invalid list prefix {prefix!r}")
    if any(segment.startswith(".") for segment in prefix.split("/")):
        raise ValueError(f"invalid list prefix {prefix!r}: no segment may start with '.'")
    return prefix


def _hidden(key: str) -> bool:
    """True if a segment of ``key`` starts with '.', which no valid key has."""
    return any(segment.startswith(".") for segment in key.split("/"))


def _dumps_json(obj: Any) -> bytes:
    return (dumps_json(obj, indent=2) + "\n").encode("utf-8")


@runtime_checkable
class Storage(Protocol):
    """What every worker may do with collected data; implemented by Local and S3 storage."""

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str | None = None, if_absent: bool = False
    ) -> bool:
        """Store ``data`` atomically; with ``if_absent`` only if the key is new. True if written."""
        ...

    def get_bytes(self, key: str) -> bytes:
        """Return the object's bytes; raise :class:`KeyNotFoundError` if missing."""
        ...

    def exists(self, key: str) -> bool:
        """Return True if the key exists."""
        ...

    def list_prefix(self, prefix: str = "") -> list[str]:
        """Return the sorted keys that start with ``prefix`` (S3 string-prefix semantics)."""
        ...

    def read_json(self, key: str, default: Any = _MISSING) -> Any:
        """Parse a JSON object; return ``default`` if given and the key is missing."""
        ...

    def write_json(self, key: str, obj: Any) -> None:
        """Write ``obj`` as indented UTF-8 JSON, atomically."""
        ...

    def append_jsonl(self, key: str, record: Mapping[str, Any]) -> dict[str, Any]:
        """Append one record (UTC ``t`` added if absent) and return it as written."""
        ...

    def append_jsonl_many(
        self, key: str, records: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """Append several records in one write (one S3 round trip)."""
        ...

    def lock(
        self, name: str, *, timeout_s: float = 60.0, ttl_s: float = 300.0
    ) -> AbstractContextManager[None]:
        """Hold the named mutual-exclusion lock for the duration of a ``with`` block."""
        ...


class LocalStorage:
    """Storage rooted at a local directory (normally ``$LOOKOUT_DATA_DIR``)."""

    def __init__(self, root: str | os.PathLike[str], *, check: bool = True) -> None:
        path = Path(root).expanduser()
        self.root = check_data_dir(path) if check else path.resolve()

    def __repr__(self) -> str:
        return f"LocalStorage({str(self.root)!r})"

    def path(self, key: str) -> Path:
        """Absolute local path of ``key`` (for tools that need a file, such as Pillow)."""
        return self.root.joinpath(*PurePosixPath(validate_key(key)).parts)

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str | None = None, if_absent: bool = False
    ) -> bool:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.chmod(0o644)
            if if_absent:
                try:
                    os.link(tmp, target)  # atomic create-only publish
                except FileExistsError:
                    return False
                finally:
                    tmp.unlink(missing_ok=True)
            else:
                os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        _fsync_dir(target.parent)
        return True

    def get_bytes(self, key: str) -> bytes:
        try:
            return self.path(key).read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise KeyNotFoundError(key) from exc

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def list_prefix(self, prefix: str = "") -> list[str]:
        _validate_prefix(prefix)
        head = prefix.rpartition("/")[0]
        base = self.root.joinpath(*PurePosixPath(head).parts) if head else self.root
        if not base.is_dir():
            return []
        keys: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for name in filenames:
                if name.startswith("."):
                    continue  # temp files of atomic writes, .DS_Store
                key = (Path(dirpath) / name).relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    keys.append(key)
        return sorted(keys)

    def read_json(self, key: str, default: Any = _MISSING) -> Any:
        try:
            raw = self.get_bytes(key)
        except KeyNotFoundError:
            if default is _MISSING:
                raise
            return default
        return json.loads(raw)

    def write_json(self, key: str, obj: Any) -> None:
        self.put_bytes(key, _dumps_json(obj), content_type="application/json")

    def append_jsonl(self, key: str, record: Mapping[str, Any]) -> dict[str, Any]:
        return self.append_jsonl_many(key, [record])[0]

    def append_jsonl_many(
        self, key: str, records: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        return _append_local(self.path(key), records)

    @contextmanager
    def lock(
        self, name: str, *, timeout_s: float = 60.0, ttl_s: float = 300.0, poll_s: float = 0.05
    ) -> Iterator[None]:
        """``flock`` on ``state/locks/<name>.lock``; ``ttl_s`` is unused (the OS frees it)."""
        target = self.path(lock_key(name))
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeoutError(f"lock {name!r} busy for {timeout_s} s") from None
                    time.sleep(poll_s)
            try:
                info = {"pid": os.getpid(), "acquired_at": isoformat_z(utcnow(), "milliseconds")}
                os.ftruncate(fd, 0)
                os.pwrite(fd, json.dumps(info).encode(), 0)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _fsync_dir(path: Path) -> None:
    with suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        return str(code) if code is not None else None
    return None


class S3Storage:
    """Storage in an S3 bucket under ``prefix``; keys match :class:`LocalStorage` exactly.

    ``boto3`` is imported only when no ``client`` is passed (install the ``s3`` extra).
    Credentials and region come from the usual AWS environment; this class never
    creates buckets or other resources.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        client: Any = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not bucket:
            raise ValueError("S3Storage needs a bucket name")
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise ImportError("S3Storage needs boto3: install 'lookout-common[s3]'") from exc
            client = boto3.client("s3")
        cleaned = prefix.strip().strip("/")
        if cleaned:
            validate_key(cleaned)
        self.bucket = bucket
        self.prefix = f"{cleaned}/" if cleaned else ""
        self._s3 = client
        self._clock = clock
        self._sleep = sleep

    def __repr__(self) -> str:
        return f"S3Storage({self.bucket!r}, {self.prefix!r})"

    def _full(self, key: str) -> str:
        return self.prefix + validate_key(key)

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str | None = None,
        if_absent: bool = False,
        max_attempts: int = 5,
    ) -> bool:
        """Store ``data``; with ``if_absent`` (``If-None-Match: *``) only if the key is new.

        Returns False only on 412 PreconditionFailed: the key exists and was kept. A 409
        ConditionalRequestConflict (another conditional write to the key was in
        flight) is retried up to ``max_attempts`` times with jittered back-off, then
        raised: nothing may have been stored, so it must never read as "exists".
        """
        params: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._full(key),
            "Body": data,
            "ContentType": content_type or content_type_for(key),
        }
        if if_absent:
            params["IfNoneMatch"] = "*"
        for attempt in range(max(1, max_attempts)):
            try:
                self._s3.put_object(**params)
                return True
            except Exception as exc:
                code = _error_code(exc)
                if not if_absent or code not in _CONFLICT_CODES:
                    raise
                if code in _PRECONDITION_CODES:
                    return False
                if attempt + 1 >= max_attempts:
                    raise
            self._sleep(min(0.05 * 2**attempt, 1.0) * random.uniform(0.5, 1.0))
        raise AssertionError("unreachable")  # pragma: no cover

    def _get(self, key: str) -> tuple[bytes, str]:
        try:
            response = self._s3.get_object(Bucket=self.bucket, Key=self._full(key))
        except Exception as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise KeyNotFoundError(key) from exc
            raise
        return response["Body"].read(), response["ETag"]

    def get_bytes(self, key: str) -> bytes:
        return self._get(key)[0]

    def exists(self, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=self.bucket, Key=self._full(key))
        except Exception as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                return False
            raise
        return True

    def list_prefix(self, prefix: str = "") -> list[str]:
        _validate_prefix(prefix)
        paginator = self._s3.get_paginator("list_objects_v2")
        keys: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            for item in page.get("Contents", []):
                key = item["Key"][len(self.prefix) :]
                if not _hidden(key):  # as LocalStorage, which skips dot-files
                    keys.append(key)
        return sorted(keys)

    def read_json(self, key: str, default: Any = _MISSING) -> Any:
        try:
            raw = self.get_bytes(key)
        except KeyNotFoundError:
            if default is _MISSING:
                raise
            return default
        return json.loads(raw)

    def write_json(self, key: str, obj: Any) -> None:
        self.put_bytes(key, _dumps_json(obj), content_type="application/json")

    def append_jsonl(self, key: str, record: Mapping[str, Any]) -> dict[str, Any]:
        return self.append_jsonl_many(key, [record])[0]

    def append_jsonl_many(
        self, key: str, records: Iterable[Mapping[str, Any]], *, max_attempts: int = 12
    ) -> list[dict[str, Any]]:
        rows = [make_record(record) for record in records]
        if not rows:
            return rows
        payload = b"".join(encode_line(row) for row in rows)
        full = self._full(key)
        for attempt in range(max_attempts):
            try:
                body, etag = self._get(key)
            except KeyNotFoundError:
                body, etag = b"", None
            if body and not body.endswith(b"\n"):
                body += b"\n"  # never glue a new record onto a torn line
            params: dict[str, Any] = {
                "Bucket": self.bucket,
                "Key": full,
                "Body": body + payload,
                "ContentType": "application/x-ndjson",
            }
            if etag is None:
                params["IfNoneMatch"] = "*"
            else:
                params["IfMatch"] = etag
            try:
                self._s3.put_object(**params)
                return rows
            except Exception as exc:
                if _error_code(exc) not in _CONFLICT_CODES:
                    raise
            self._sleep(min(0.05 * 2**attempt, 1.0) * random.uniform(0.5, 1.0))
        raise TimeoutError(f"append to {key!r} lost {max_attempts} write races in a row")

    @contextmanager
    def lock(
        self, name: str, *, timeout_s: float = 60.0, ttl_s: float = 300.0, poll_s: float = 1.0
    ) -> Iterator[None]:
        """Conditional-put lock on ``state/locks/<name>.lock`` (see the module docstring)."""
        full = self._full(lock_key(name))
        token = uuid.uuid4().hex
        deadline = self._clock() + timeout_s
        held_etag: str | None = None
        while True:
            now = self._clock()
            body = json.dumps(
                {
                    "owner": token,
                    "pid": os.getpid(),
                    "acquired_unix": now,
                    "expires_unix": now + ttl_s,
                    "expires_at": isoformat_z(datetime.fromtimestamp(now + ttl_s, UTC), "seconds"),
                }
            ).encode()
            base = {
                "Bucket": self.bucket,
                "Key": full,
                "Body": body,
                "ContentType": "application/json",
            }
            try:
                held_etag = self._s3.put_object(**base, IfNoneMatch="*")["ETag"]
                break
            except Exception as exc:
                if _error_code(exc) not in _CONFLICT_CODES:
                    raise
            try:
                current = self._s3.get_object(Bucket=self.bucket, Key=full)
                current_etag = current["ETag"]
                try:
                    expires = float(json.loads(current["Body"].read())["expires_unix"])
                except (ValueError, KeyError, TypeError):
                    expires = 0.0  # unreadable lock body: treat as expired
            except Exception as exc:
                if _error_code(exc) in _NOT_FOUND_CODES:
                    continue  # released between our put and get; try again now
                raise
            if now >= expires:
                try:
                    held_etag = self._s3.put_object(**base, IfMatch=current_etag)["ETag"]
                    log.warning("took over expired S3 lock %s", name)
                    break
                except Exception as exc:
                    if _error_code(exc) not in _CONFLICT_CODES:
                        raise
                    continue
            if now >= deadline:
                raise LockTimeoutError(f"lock {name!r} busy for {timeout_s} s")
            self._sleep(max(0.0, min(poll_s, deadline - now)))
        try:
            yield
        finally:
            try:
                self._s3.delete_object(Bucket=self.bucket, Key=full, IfMatch=held_etag)
            except Exception as exc:
                code = _error_code(exc)
                if code in _CONFLICT_CODES or code in _NOT_FOUND_CODES:
                    log.warning("S3 lock %s expired and was taken over while held", name)
                else:
                    log.exception("could not release S3 lock %s; it expires after ttl", name)


def storage_from_env() -> LocalStorage | S3Storage:
    """Build storage from ``LOOKOUT_STORAGE`` (``local`` default, or ``s3``).

    ``s3`` needs ``LOOKOUT_S3_BUCKET`` and takes an optional ``LOOKOUT_S3_PREFIX``.
    ``local`` roots at :func:`lookout_common.paths.data_dir`.
    """
    kind = (os.environ.get(ENV_STORAGE) or "local").strip().lower()
    if kind == "local":
        return LocalStorage(data_dir())
    if kind == "s3":
        bucket = (os.environ.get(ENV_S3_BUCKET) or "").strip()
        if not bucket:
            raise ValueError(f"{ENV_STORAGE}=s3 needs {ENV_S3_BUCKET}")
        return S3Storage(bucket, os.environ.get(ENV_S3_PREFIX, ""))
    raise ValueError(f"{ENV_STORAGE} must be 'local' or 's3', got {kind!r}")
