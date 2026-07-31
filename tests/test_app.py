import asyncio
import json
import logging

import pytest
from fastapi.testclient import TestClient

import app


class FakeBatch:
    def __init__(self, client):
        self.client = client

    def __enter__(self):
        self.client.inside_batch = True
        self.client.batch_entries += 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.client.inside_batch = False
        return False


class FakeStorageClient:
    def __init__(self):
        self.inside_batch = False
        self.batch_entries = 0

    def batch(self):
        return FakeBatch(self)


class FakeBucket:
    def __init__(self, client, error=None):
        self.client = client
        self.error = error
        self.delete_calls = []

    def delete_blobs(self, blob_names, client):
        assert client is self.client
        assert self.client.inside_batch
        self.delete_calls.append(list(blob_names))
        if self.error is not None:
            raise self.error


@pytest.fixture
def fake_storage(monkeypatch):
    client = FakeStorageClient()
    storage_bucket = FakeBucket(client)
    monkeypatch.setattr(app, "storage_client", client)
    monkeypatch.setattr(app, "bucket", storage_bucket)
    return client, storage_bucket


def cleanup_result(**overrides):
    values = {
        "url": "https://example.com",
        "desktop": (
            f"https://storage.googleapis.com/{app.BUCKET_NAME}/"
            "0123abcd-desktop.png"
        ),
        "mobile": (
            f"https://storage.googleapis.com/{app.BUCKET_NAME}/"
            "0123abcd-mobile.png"
        ),
    }
    values.update(overrides)
    return app.CleanupResult(**values)


def test_collect_cleanup_targets_deduplicates_and_skips_error_results():
    results = [
        cleanup_result(),
        cleanup_result(),
        app.CleanupResult(url="https://failed.example", error="capture failed"),
    ]

    blob_names, duplicates, skipped = app.collect_cleanup_targets(results)

    assert blob_names == [
        "0123abcd-desktop.png",
        "0123abcd-mobile.png",
    ]
    assert duplicates == 2
    assert skipped == 1


@pytest.mark.parametrize(
    "image_url",
    [
        "https://storage.googleapis.com/another-bucket/0123abcd-desktop.png",
        "http://storage.googleapis.com/bucket/0123abcd-desktop.png",
        "https://example.com/0123abcd-desktop.png",
        "https://storage.googleapis.com/bucket/unrelated-file.png",
        "https://storage.googleapis.com/bucket/0123abcd-desktop.png?generation=1",
    ],
)
def test_blob_name_from_screenshot_url_rejects_untrusted_urls(image_url):
    with pytest.raises(ValueError):
        app.blob_name_from_screenshot_url(image_url)


def test_empty_cleanup_does_not_initialize_cloud_storage(monkeypatch):
    monkeypatch.setattr(app, "storage_client", None)
    monkeypatch.setattr(app, "bucket", None)

    response = app.cleanup([app.CleanupRequest(results=[])])

    assert response["status"] == "completed"
    assert response["attempted"] == 0
    assert response["deleted"] == 0
    assert response["batches"] == 0
    assert app.storage_client is None
    assert app.bucket is None


def test_delete_blobs_uses_one_batch_for_six_objects(fake_storage):
    client, storage_bucket = fake_storage
    blob_names = [f"{index:08x}-desktop.png" for index in range(6)]

    batch_count = app.delete_blobs_in_batches(blob_names)

    assert batch_count == 1
    assert client.batch_entries == 1
    assert storage_bucket.delete_calls == [blob_names]


def test_delete_blobs_chunks_requests_at_one_hundred(fake_storage):
    client, storage_bucket = fake_storage
    blob_names = [f"{index:08x}-desktop.png" for index in range(205)]

    batch_count = app.delete_blobs_in_batches(blob_names)

    assert batch_count == 3
    assert client.batch_entries == 3
    assert [len(call) for call in storage_bucket.delete_calls] == [100, 100, 5]


def test_cleanup_rejects_all_targets_before_deleting(fake_storage):
    client, storage_bucket = fake_storage
    request = app.CleanupRequest(
        results=[
            cleanup_result(),
            cleanup_result(
                desktop="https://storage.googleapis.com/foreign/bad-desktop.png",
                mobile=None,
            ),
        ]
    )

    response = app.cleanup([request])

    assert response.status_code == 400
    assert client.batch_entries == 0
    assert storage_bucket.delete_calls == []


def test_cleanup_returns_success_summary(fake_storage):
    request = app.CleanupRequest(results=[cleanup_result()])

    response = app.cleanup([request])

    assert response == {
        "status": "completed",
        "attempted": 2,
        "deleted": 2,
        "batches": 1,
        "duplicates_ignored": 0,
        "skipped_records": 0,
    }


def test_cleanup_endpoint_accepts_wrapped_screenshot_response(fake_storage):
    client = TestClient(app.app)
    result = cleanup_result()

    response = client.post(
        "/cleanup",
        json=[
            {
                "results": [
                    {
                        "url": result.url,
                        "desktop": result.desktop,
                        "mobile": result.mobile,
                    }
                ]
            }
        ],
    )

    assert response.status_code == 200
    assert response.json()["deleted"] == 2
    assert response.json()["batches"] == 1


def test_cleanup_endpoint_accepts_direct_n8n_results_array(fake_storage):
    _, storage_bucket = fake_storage
    client = TestClient(app.app)
    payload = [
        {
            "url": f"https://example.com/{index}",
            "desktop": (
                f"https://storage.googleapis.com/{app.BUCKET_NAME}/"
                f"{index:08x}-desktop.png"
            ),
            "mobile": (
                f"https://storage.googleapis.com/{app.BUCKET_NAME}/"
                f"{index:08x}-mobile.png"
            ),
        }
        for index in range(1, 4)
    ]

    response = client.post("/cleanup", json=payload)

    assert response.status_code == 200
    assert response.json()["deleted"] == 6
    assert response.json()["batches"] == 1
    assert len(storage_bucket.delete_calls) == 1
    assert len(storage_bucket.delete_calls[0]) == 6


def test_cleanup_logs_request_batch_and_completion(fake_storage, caplog):
    request = app.CleanupRequest(results=[cleanup_result()])

    with caplog.at_level(logging.INFO, logger="screenshot-api"):
        app.cleanup([request])

    messages = [record.getMessage() for record in caplog.records]
    assert (
        "Received cleanup request with 1 top-level item(s), 1 wrapped payload(s), "
        "and 1 screenshot results"
        in messages
    )
    assert any(message.startswith("Starting cleanup batch 1/1") for message in messages)
    assert "Cleanup completed: deleted 2 objects in 1 batches" in messages


def test_cleanup_reports_batch_failure(monkeypatch):
    client = FakeStorageClient()
    storage_bucket = FakeBucket(client, error=RuntimeError("GCS unavailable"))
    monkeypatch.setattr(app, "storage_client", client)
    monkeypatch.setattr(app, "bucket", storage_bucket)
    request = app.CleanupRequest(results=[cleanup_result()])

    response = app.cleanup([request])
    body = json.loads(response.body)

    assert response.status_code == 502
    assert body == {
        "status": "failed",
        "detail": "Cloud Storage cleanup failed; some deletions may have succeeded",
        "attempted": 2,
    }


def test_capture_url_preserves_successful_upload_when_other_upload_fails(
    monkeypatch,
):
    async def fake_capture_screenshot(*args, **kwargs):
        return None

    def fake_upload(file_path):
        if file_path.endswith("-mobile.png"):
            raise RuntimeError("mobile upload failed")
        return (
            f"https://storage.googleapis.com/{app.BUCKET_NAME}/"
            "0123abcd-desktop.png"
        )

    monkeypatch.setattr(app, "capture_screenshot", fake_capture_screenshot)
    monkeypatch.setattr(app, "upload_to_gcs", fake_upload)

    result = asyncio.run(app.capture_url(object(), "https://example.com"))

    assert result["url"] == "https://example.com"
    assert result["desktop"].endswith("0123abcd-desktop.png")
    assert "mobile" not in result
    assert result["error"] == "mobile: mobile upload failed"
