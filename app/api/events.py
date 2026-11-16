from collections.abc import AsyncIterator

from app.api.routes.files import FileStreamingResponse


def event_response(messages: AsyncIterator[bytes]) -> FileStreamingResponse:
    return FileStreamingResponse(
        messages,
        media_type="text/event-stream",
        headers={"Cache-Control": "private, no-store, no-transform", "X-Accel-Buffering": "no"},
    )
