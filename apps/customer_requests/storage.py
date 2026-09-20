"""Private, worker-readable storage for customer messenger attachments."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible


@deconstructible
class PrivateAttachmentStorage(FileSystemStorage):
    """Store new attachments outside the Caddy-served media tree.

    The legacy fallback keeps already queued rows readable while the
    reconciliation command copies their bytes to the private volume.
    """

    def __init__(self):
        # ``upload_to`` already contributes ``customer_requests/`` to the
        # name; the root itself must therefore be the private volume root.
        private_root = Path(settings.PRIVATE_MEDIA_ROOT)
        super().__init__(location=private_root, base_url=None)
        self.legacy = FileSystemStorage(location=settings.MEDIA_ROOT)

    def open(self, name, mode="rb"):
        if super().exists(name):
            return super().open(name, mode)
        return self.legacy.open(name, mode)

    def exists(self, name):
        return super().exists(name) or self.legacy.exists(name)

    def size(self, name):
        if super().exists(name):
            return super().size(name)
        return self.legacy.size(name)

    def delete(self, name):
        super().delete(name)
        self.legacy.delete(name)

    def url(self, name):
        raise ValueError("Customer attachments have no public URL.")
