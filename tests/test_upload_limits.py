import asyncio

import pytest
from fastapi import HTTPException

from app.services.uploads import read_limited_body


class StreamRequest:
    def __init__(self, chunks, headers=None):
        self.headers = headers or {}
        self.chunks = chunks
        self.consumed = 0

    async def stream(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


def test_upload_accepts_body_at_limit():
    request = StreamRequest([b"12", b"345", b""])
    assert asyncio.run(read_limited_body(request, max_bytes=5)) == b"12345"


def test_upload_rejects_declared_oversize_before_reading():
    request = StreamRequest([b"not consumed"], {"content-length": "6"})
    with pytest.raises(HTTPException) as error:
        asyncio.run(read_limited_body(request, max_bytes=5))
    assert error.value.status_code == 413
    assert request.consumed == 0


@pytest.mark.parametrize("headers", [{}, {"content-length": "1"}])
def test_upload_stops_at_stream_limit_even_with_missing_or_false_length(headers):
    request = StreamRequest([b"123", b"456", b"must not be consumed"], headers)
    with pytest.raises(HTTPException) as error:
        asyncio.run(read_limited_body(request, max_bytes=5))
    assert error.value.status_code == 413
    assert request.consumed == 2


@pytest.mark.parametrize("length", ["invalid", "-1"])
def test_upload_rejects_invalid_declared_length(length):
    request = StreamRequest([b"123"], {"content-length": length})
    with pytest.raises(HTTPException) as error:
        asyncio.run(read_limited_body(request, max_bytes=5))
    assert error.value.status_code == 400
    assert request.consumed == 0


def test_upload_rejects_empty_stream():
    with pytest.raises(HTTPException) as error:
        asyncio.run(read_limited_body(StreamRequest([b""]), max_bytes=5))
    assert error.value.status_code == 413


def test_rejected_upload_does_not_create_an_image(client):
    project = client.post("/api/v1/projects", json={"name": "Upload limit"}).json()
    asset = client.post(
        f"/api/v1/projects/{project['id']}/assets",
        json={"asset_type": "character", "name": "Pilot"},
    ).json()
    response = client.put(
        f"/api/v1/assets/{asset['id']}/image",
        content=b"tiny",
        headers={"content-type": "image/png", "content-length": str(11 * 1024 * 1024)},
    )
    assert response.status_code == 413
    assert client.get(f"/api/v1/assets/{asset['id']}/images").json() == []
