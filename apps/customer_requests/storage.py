"""Private, worker-readable storage for customer messenger attachments."""

from __future__ import annotations

import secrets
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

    def get_available_name(self, name, max_length=None):
        # The original name is audit metadata only, never a filesystem path.
        extension = Path(name).suffix.lower()
        if extension not in {".png", ".jpg", ".jpeg", ".webp", ".pdf"}:
            extension = ".bin"
        return super().get_available_name(
            f"customer_requests/{secrets.token_hex(20)}{extension}", max_length=max_length
        )

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
