from __future__ import annotations

import json
import multiprocessing
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from lookout_common import storage as st
from lookout_common.paths import UnsafeDataDirError
from lookout_common.storage import KeyNotFoundError, LocalStorage, LockTimeoutError, S3Storage

from .conftest import REPO_ROOT

BUCKET = "lookout-test-bucket"


@pytest.fixture
def s3_client(aws_env) -> Iterator[object]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture(params=["local", "s3"])
def store(request, tmp_path: Path, aws_env) -> Iterator[st.Storage]:
    if request.param == "local":
        yield LocalStorage(tmp_path / "data")
        return
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield S3Storage(BUCKET, "lookout/dev", client=client)


# --- the shared contract ----------------------------------------------------------------
def test_satisfies_protocol(store):
    assert isinstance(store, st.Storage)


def test_put_get_exists(store):
    key = "frames/point-reyes.beach/2026-11-19/20261119T061500Z_0123456789ab.jpg"
    assert not store.exists(key)
    assert store.put_bytes(key, b"\xff\xd8jpeg") is True
    assert store.exists(key)
    assert store.get_bytes(key) == b"\xff\xd8jpeg"
    assert store.put_bytes(key, b"new") is True  # plain put overwrites
    assert store.get_bytes(key) == b"new"


def test_put_if_absent_never_overwrites(store):
    key = "archive/point-reyes.beach/2026-11-17/20261117T080000Z_50c46ac9.jpg"
    assert store.put_bytes(key, b"first", if_absent=True) is True
    assert store.put_bytes(key, b"second", if_absent=True) is False
    assert store.get_bytes(key) == b"first"


def test_missing_key(store):
    with pytest.raises(KeyNotFoundError):
        store.get_bytes("state/nope.json")
    with pytest.raises(FileNotFoundError):  # KeyNotFoundError is a FileNotFoundError
        store.read_json("state/nope.json")
    assert store.read_json("state/nope.json", default={"n": 0}) == {"n": 0}


def test_json_roundtrip(store):
    store.write_json("state/capture/pore.json", {"last_sha": "abc", "n": 3, "é": True})
    assert store.read_json("state/capture/pore.json") == {"last_sha": "abc", "n": 3, "é": True}


def test_list_prefix_has_s3_semantics(store):
    for key in [
        "frames/a.b/2026-11-19/1.jpg",
        "frames/a.b/2026-11-20/2.jpg",
        "frames/a.b-c/2026-11-19/3.jpg",
        "capture_log/2026-11-19.jsonl",
    ]:
        store.put_bytes(key, b"x")
    assert store.list_prefix("frames/a.b/") == [
        "frames/a.b/2026-11-19/1.jpg",
        "frames/a.b/2026-11-20/2.jpg",
    ]
    assert store.list_prefix("frames/a.b") == [
        "frames/a.b-c/2026-11-19/3.jpg",
        "frames/a.b/2026-11-19/1.jpg",
        "frames/a.b/2026-11-20/2.jpg",
    ]
    assert store.list_prefix("frames/a.b/2026-11-2") == ["frames/a.b/2026-11-20/2.jpg"]
    assert len(store.list_prefix("")) == 4
    assert store.list_prefix("nothing/") == []
    with pytest.raises(ValueError):
        store.list_prefix("../")


def test_append_jsonl(store):
    key = "capture_log/2026-11-19.jsonl"
    first = store.append_jsonl(key, {"vp": "point-reyes.beach", "event": "polled"})
    store.append_jsonl_many(key, [{"event": "stored"}, {"event": "rate_capped"}])
    lines = store.get_bytes(key).decode().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["polled", "stored", "rate_capped"]
    assert json.loads(lines[0])["t"] == first["t"]


@pytest.mark.parametrize(
    "key", ["", "/abs", "a/../b", "trailing/", "back\\slash", "a//b", "frames/./x.jpg"]
)
def test_bad_keys_are_rejected(store, key: str):
    with pytest.raises(ValueError):
        store.put_bytes(key, b"x")


def test_lock_excludes_and_times_out(store):
    with store.lock("nps-archive", timeout_s=5):
        if isinstance(store, LocalStorage):
            # A second flock on the same file from this process must also wait.
            with pytest.raises(LockTimeoutError), store.lock("nps-archive", timeout_s=0.2):
                pass
        else:
            with (
                pytest.raises(LockTimeoutError),
                store.lock("nps-archive", timeout_s=0.3, poll_s=0.05),
            ):
                pass
    with store.lock("nps-archive", timeout_s=1):
        pass  # released, so it can be taken again
    # Same key in both backends; the local flock file stays, the S3 lock object is deleted.
    expected = ["state/locks/nps-archive.lock"] if isinstance(store, LocalStorage) else []
    assert store.list_prefix("state/locks/") == expected


# --- local specifics --------------------------------------------------------------------
def test_local_refuses_repo_root():
    with pytest.raises(UnsafeDataDirError):
        LocalStorage(REPO_ROOT / "data")


def test_local_atomic_write_leaves_no_temp_files(tmp_path: Path):
    store = LocalStorage(tmp_path / "data")
    store.write_json("state/x.json", {"a": 1})
    store.put_bytes("state/x.json", b"{}", if_absent=True)
    names = [p.name for p in (tmp_path / "data" / "state").iterdir()]
    assert names == ["x.json"]


def test_local_list_skips_hidden_files(tmp_path: Path):
    store = LocalStorage(tmp_path / "data")
    store.put_bytes("frames/a.b/x.jpg", b"x")
    (tmp_path / "data" / "frames" / ".DS_Store").write_bytes(b"junk")
    (tmp_path / "data" / "frames" / "a.b" / ".x.jpg.123.tmp").write_bytes(b"partial")
    assert store.list_prefix("frames/") == ["frames/a.b/x.jpg"]


def test_local_path_and_repr(tmp_path: Path):
    store = LocalStorage(tmp_path / "data")
    assert store.path("a/b.json") == (tmp_path / "data").resolve() / "a" / "b.json"
    assert "LocalStorage" in repr(store)


def _locked_increment(root: str, rounds: int) -> None:
    store = LocalStorage(root, check=False)
    for _ in range(rounds):
        with store.lock("counter", timeout_s=30):
            value = store.read_json("state/counter.json", default={"n": 0})["n"]
            time.sleep(0.001)
            store.write_json("state/counter.json", {"n": value + 1})


def test_local_lock_is_exclusive_across_processes(tmp_path: Path):
    root = tmp_path / "data"
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_locked_increment, args=(str(root), 40)) for _ in range(3)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
        assert proc.exitcode == 0
    assert LocalStorage(root).read_json("state/counter.json") == {"n": 120}


# --- S3 specifics -------------------------------------------------------------------------
def test_s3_prefix_is_applied_and_stripped(s3_client):
    store = S3Storage(BUCKET, "/lookout/dev/", client=s3_client)
    assert store.prefix == "lookout/dev/"
    store.put_bytes("frames/a.b/x.jpg", b"x")
    raw = s3_client.list_objects_v2(Bucket=BUCKET)["Contents"]
    assert [o["Key"] for o in raw] == ["lookout/dev/frames/a.b/x.jpg"]
    head = s3_client.head_object(Bucket=BUCKET, Key="lookout/dev/frames/a.b/x.jpg")
    assert head["ContentType"] == "image/jpeg"
    assert store.list_prefix("frames/") == ["frames/a.b/x.jpg"]
    assert S3Storage(BUCKET, "", client=s3_client).prefix == ""


def test_s3_concurrent_appends_lose_nothing(s3_client):
    store = S3Storage(BUCKET, "", client=s3_client)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(15):
                store.append_jsonl("forecast_log/2026-11-19.jsonl", {"w": n, "i": i})
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    lines = store.get_bytes("forecast_log/2026-11-19.jsonl").decode().splitlines()
    assert len(lines) == 60
    for n in range(4):
        assert [json.loads(x)["i"] for x in lines if json.loads(x)["w"] == n] == list(range(15))


def test_s3_append_repairs_a_torn_last_line(s3_client):
    s3_client.put_object(Bucket=BUCKET, Key="capture_log/2026-11-19.jsonl", Body=b'{"a":1}')
    store = S3Storage(BUCKET, "", client=s3_client)
    store.append_jsonl("capture_log/2026-11-19.jsonl", {"a": 2})
    lines = store.get_bytes("capture_log/2026-11-19.jsonl").decode().splitlines()
    assert [json.loads(x)["a"] for x in lines] == [1, 2]


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_s3_expired_lock_is_taken_over(s3_client):
    clock = FakeClock()
    stale = {"owner": "crashed", "expires_unix": clock.now - 1}
    s3_client.put_object(
        Bucket=BUCKET, Key="state/locks/forecast.lock", Body=json.dumps(stale).encode()
    )
    store = S3Storage(BUCKET, "", client=s3_client, clock=clock, sleep=clock.sleep)
    with store.lock("forecast", timeout_s=5, ttl_s=60):
        body = json.loads(store.get_bytes("state/locks/forecast.lock"))
        assert body["owner"] != "crashed"
        assert body["expires_unix"] == pytest.approx(clock.now + 60)
    assert not store.exists("state/locks/forecast.lock")


def test_s3_live_lock_blocks_until_timeout(s3_client):
    clock = FakeClock()
    live = {"owner": "other", "expires_unix": clock.now + 600}
    s3_client.put_object(
        Bucket=BUCKET, Key="state/locks/capture.lock", Body=json.dumps(live).encode()
    )
    store = S3Storage(BUCKET, "", client=s3_client, clock=clock, sleep=clock.sleep)
    with pytest.raises(LockTimeoutError), store.lock("capture", timeout_s=30, poll_s=5):
        pass
    assert clock.now >= 1_000_030.0  # it waited (fake time) rather than spinning
    assert json.loads(store.get_bytes("state/locks/capture.lock"))["owner"] == "other"


def test_s3_lock_lost_does_not_delete_the_new_owner(s3_client, caplog):
    store = S3Storage(BUCKET, "", client=s3_client)
    with caplog.at_level("WARNING"), store.lock("capture", ttl_s=60):
        s3_client.put_object(Bucket=BUCKET, Key="state/locks/capture.lock", Body=b'{"owner":"x"}')
    assert json.loads(store.get_bytes("state/locks/capture.lock")) == {"owner": "x"}
    assert "taken over" in caplog.text


def test_s3_unreadable_lock_counts_as_expired(s3_client):
    s3_client.put_object(Bucket=BUCKET, Key="state/locks/x.lock", Body=b"not json")
    store = S3Storage(BUCKET, "", client=s3_client)
    with store.lock("x", timeout_s=2):
        pass


# --- storage_from_env -----------------------------------------------------------------------
def test_storage_from_env_local_default(data_root: Path):
    store = st.storage_from_env()
    assert isinstance(store, LocalStorage) and store.root == data_root.resolve()


def test_storage_from_env_s3(monkeypatch: pytest.MonkeyPatch, s3_client):
    monkeypatch.setenv("LOOKOUT_STORAGE", "s3")
    with pytest.raises(ValueError, match="LOOKOUT_S3_BUCKET"):
        st.storage_from_env()
    monkeypatch.setenv("LOOKOUT_S3_BUCKET", BUCKET)
    monkeypatch.setenv("LOOKOUT_S3_PREFIX", "lookout")
    store = st.storage_from_env()
    assert isinstance(store, S3Storage) and store.prefix == "lookout/"
    store.write_json("state/ping.json", {"ok": True})
    assert store.read_json("state/ping.json") == {"ok": True}


def test_storage_from_env_rejects_unknown(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOOKOUT_STORAGE", "gcs")
    with pytest.raises(ValueError):
        st.storage_from_env()


def test_content_types():
    assert st.content_type_for("a/b.JPG") == "image/jpeg"
    assert st.content_type_for("a/b.jsonl") == "application/x-ndjson"
    assert st.content_type_for("a/b.grib2") == "application/octet-stream"
    assert st.lock_key("nps-archive") == "state/locks/nps-archive.lock"
