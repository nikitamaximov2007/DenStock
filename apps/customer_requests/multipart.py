"""Bounded multipart request body without copying an entire attachment."""

CHUNK_BYTES = 64 * 1024


class MultipartBody:
    """A reusable, length-known body accepted by urllib.request.

    The original attachment bytes stay owned by the caller. Only one small
    chunk is copied at a time for the HTTP client's iterable interface.
    """

    def __init__(self, *segments: bytes):
        self._segments = segments
        self._length = sum(len(segment) for segment in segments)

    def __len__(self) -> int:
        return self._length

    def __iter__(self):
        for segment in self._segments:
            view = memoryview(segment)
            for offset in range(0, len(view), CHUNK_BYTES):
                yield bytes(view[offset:offset + CHUNK_BYTES])
