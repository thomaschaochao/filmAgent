"""Bound request bodies before buffering them or writing application state."""

from fastapi import HTTPException, Request


async def read_limited_body(request: Request, *, max_bytes: int) -> bytes:
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            length = int(declared_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if length > max_bytes:
            raise HTTPException(status_code=413, detail="Image must be between 1 byte and 10 MB")

    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > max_bytes:
            raise HTTPException(status_code=413, detail="Image must be between 1 byte and 10 MB")
        content.extend(chunk)
    if not content:
        raise HTTPException(status_code=413, detail="Image must be between 1 byte and 10 MB")
    return bytes(content)
