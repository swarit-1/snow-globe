"""The hindcast's own storage tree, ``<data dir>/forecast/hindcast/`` (PRD §13.2, §13.4).

The hindcast mode runs the live extractor (:class:`..extract.Extractor`) unchanged, so it
writes the keys the live worker writes. :class:`HindcastStorage` maps exactly those keys into
the hindcast's root and refuses every other one, so a hindcast can never write under
``forecast/hrrr/`` or ``forecast_log/`` (the live stand-in's, D-030):

=================================  ==============================================
key the extractor writes           where it lands, under ``<data dir>/forecast/hindcast/``
=================================  ==============================================
``forecast/hrrr/<vp>/<run>z.json`` ``hrrr/<vp>/<run>z.json`` (the live files' format)
``forecast_log/<day>.jsonl``       ``log/<day>.jsonl`` (the hindcast's fetch log)
``state/...`` (markers, locks)     ``state/...``
=================================  ==============================================

The hindcast's own files (plans, progress, reports, the response cache) use keys of the root
storage directly (:attr:`HindcastStorage.root`): ``plan/``, ``reports/``, ``cache/``.

Everything the hindcast reads from the live data dir goes through :class:`ReadOnlyStorage`,
which refuses every write.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from snow_globe_common.paths import validate_key
from snow_globe_common.storage import LocalStorage, Storage

__all__ = [
    "CACHE_PREFIX",
    "HINDCAST_ROOT",
    "LOG_PREFIX",
    "PLAN_PREFIX",
    "REPORTS_PREFIX",
    "HindcastStorage",
    "ReadOnlyStorage",
    "ReadOnlyStorageError",
]

#: The hindcast's root, relative to the data dir (the task's storage root; D-030 keeps
#: ``forecast/hrrr/`` and ``forecast_log/`` for the live stand-in alone).
HINDCAST_ROOT = "forecast/hindcast"
#: Prefixes of the root storage (relative to :data:`HINDCAST_ROOT`).
LOG_PREFIX = "log/"
PLAN_PREFIX = "plan/"
REPORTS_PREFIX = "reports/"
CACHE_PREFIX = "cache/"

#: (key prefix the extractor writes, prefix under the hindcast root).
_MAPPING: tuple[tuple[str, str], ...] = (
    ("forecast/hrrr/", "hrrr/"),
    ("forecast_log/", LOG_PREFIX),
    ("state/", "state/"),
)

_MISSING: Any = object()


class ReadOnlyStorageError(PermissionError):
    """A write was attempted through a :class:`ReadOnlyStorage`."""


class HindcastStorage:
    """A :class:`~snow_globe_common.storage.Storage` that maps the extractor's keys into the
    hindcast root (see the module docstring) and refuses any other key.

    ``root`` is a storage rooted at ``<data dir>/forecast/hindcast``; :meth:`at` builds the
    local one. Locks are the root's, so they live in ``forecast/hindcast/state/locks/`` and
    never contend with the live worker's.
    """

    def __init__(self, root: Storage) -> None:
        self.root = root

    @classmethod
    def at(cls, data_root: str | Path) -> HindcastStorage:
        """The hindcast storage of a local data dir (``<data_root>/forecast/hindcast``)."""
        return cls(LocalStorage(Path(data_root) / HINDCAST_ROOT))

    def __repr__(self) -> str:
        return f"HindcastStorage({self.root!r})"

    @staticmethod
    def inner_key(key: str) -> str:
        """The root key a mapped key is stored under; ``ValueError`` for any other key."""
        validate_key(key)
        for outer, inner in _MAPPING:
            if key.startswith(outer):
                return inner + key[len(outer) :]
        raise ValueError(
            f"the hindcast storage refuses {key!r}: only forecast/hrrr/, forecast_log/ and "
            f"state/ keys are mapped (into {HINDCAST_ROOT}/)"
        )

    @staticmethod
    def outer_key(inner: str) -> str | None:
        """The mapped key of a root key, or None for the hindcast's own files (plan, ...)."""
        for outer, prefix in _MAPPING:
            if inner.startswith(prefix):
                return outer + inner[len(prefix) :]
        return None

    # -- the Storage protocol ---------------------------------------------------------
    def put_bytes(
        self, key: str, data: bytes, *, content_type: str | None = None, if_absent: bool = False
    ) -> bool:
        return self.root.put_bytes(
            self.inner_key(key), data, content_type=content_type, if_absent=if_absent
        )

    def get_bytes(self, key: str) -> bytes:
        return self.root.get_bytes(self.inner_key(key))

    def exists(self, key: str) -> bool:
        return self.root.exists(self.inner_key(key))

    def list_prefix(self, prefix: str = "") -> list[str]:
        keys: list[str] = []
        for outer, inner in _MAPPING:
            if outer.startswith(prefix):
                listed = self.root.list_prefix(inner)
            elif prefix.startswith(outer):
                listed = self.root.list_prefix(inner + prefix[len(outer) :])
            else:
                continue
            for key in listed:
                mapped = self.outer_key(key)
                if mapped is not None and mapped.startswith(prefix):
                    keys.append(mapped)
        return sorted(keys)

    def read_json(self, key: str, default: Any = _MISSING) -> Any:
        if default is _MISSING:
            return self.root.read_json(self.inner_key(key))
        return self.root.read_json(self.inner_key(key), default)

    def write_json(self, key: str, obj: Any) -> None:
        self.root.write_json(self.inner_key(key), obj)

    def append_jsonl(self, key: str, record: Mapping[str, Any]) -> dict[str, Any]:
        return self.root.append_jsonl(self.inner_key(key), record)

    def append_jsonl_many(
        self, key: str, records: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        return self.root.append_jsonl_many(self.inner_key(key), records)

    def lock(
        self, name: str, *, timeout_s: float = 60.0, ttl_s: float = 300.0
    ) -> AbstractContextManager[None]:
        return self.root.lock(name, timeout_s=timeout_s, ttl_s=ttl_s)


class ReadOnlyStorage:
    """A view of a storage that reads and never writes (the live data dir, for the hindcast).

    Every write, append and lock raises :class:`ReadOnlyStorageError`, so a hindcast that
    reads ``frames/``, ``thumbs/``, ``forecast/hrrr/`` or ``forecast_log/`` cannot change them.
    """

    def __init__(self, inner: Storage) -> None:
        self.inner = inner

    def __repr__(self) -> str:
        return f"ReadOnlyStorage({self.inner!r})"

    def _refuse(self, what: str, key: str) -> ReadOnlyStorageError:
        return ReadOnlyStorageError(f"read-only storage: refusing to {what} {key!r}")

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str | None = None, if_absent: bool = False
    ) -> bool:
        raise self._refuse("write", key)

    def get_bytes(self, key: str) -> bytes:
        return self.inner.get_bytes(key)

    def exists(self, key: str) -> bool:
        return self.inner.exists(key)

    def list_prefix(self, prefix: str = "") -> list[str]:
        return self.inner.list_prefix(prefix)

    def read_json(self, key: str, default: Any = _MISSING) -> Any:
        if default is _MISSING:
            return self.inner.read_json(key)
        return self.inner.read_json(key, default)

    def write_json(self, key: str, obj: Any) -> None:
        raise self._refuse("write", key)

    def append_jsonl(self, key: str, record: Mapping[str, Any]) -> dict[str, Any]:
        raise self._refuse("append to", key)

    def append_jsonl_many(
        self, key: str, records: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        raise self._refuse("append to", key)

    def lock(
        self, name: str, *, timeout_s: float = 60.0, ttl_s: float = 300.0
    ) -> AbstractContextManager[None]:
        raise self._refuse("lock", name)
