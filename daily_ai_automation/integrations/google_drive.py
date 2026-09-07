"""Google Drive access, limited to downloading one configured file.

Read-only by construction: the credential carries ``drive.readonly``, so even a
bug here cannot modify the user's Drive (PRD non-goal: "modify arbitrary Google
Drive files").
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"


class DriveFileError(RuntimeError):
    """The configured file is missing, inaccessible, or the wrong type."""


@dataclass
class DriveFile:
    file_id: str
    name: str
    mime_type: str
    size: int
    modified_time: str


class DriveClient:
    def __init__(self, credentials) -> None:  # noqa: ANN001 - google Credentials
        self._service = build(
            "drive", "v3", credentials=credentials, cache_discovery=False
        )

    def describe(self, file_id: str) -> DriveFile:
        """Fetch metadata, translating Google's errors into actionable ones."""
        try:
            meta = (
                self._service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,size,modifiedTime",
                    supportsAllDrives=True,
                )
                .execute()
            )
        except HttpError as exc:
            status = exc.resp.status
            if status == 404:
                raise DriveFileError(
                    f"No Drive file with ID {file_id!r} is visible to this "
                    "account. Check occasion.drive_file_id in config.yaml - it "
                    "is the long token in the file's share URL, between /d/ "
                    "and /edit."
                ) from exc
            if status == 403:
                raise DriveFileError(
                    f"Access denied to Drive file {file_id!r}. Either the Drive "
                    "API is not enabled on the Google Cloud project, or this "
                    "account does not have read access to the file."
                ) from exc
            raise

        return DriveFile(
            file_id=meta["id"],
            name=meta.get("name", ""),
            mime_type=meta.get("mimeType", ""),
            size=int(meta.get("size", 0) or 0),
            modified_time=meta.get("modifiedTime", ""),
        )

    def download_xlsx(self, file_id: str) -> bytes:
        """Download the configured workbook as .xlsx bytes.

        A native Google Sheet is rejected rather than silently exported: the
        agreed contract is a real .xlsx file, and quietly accepting a Sheet
        would hide a misconfiguration until the column validation failed in a
        confusing way.
        """
        meta = self.describe(file_id)

        if meta.mime_type == GOOGLE_SHEET_MIME:
            raise DriveFileError(
                f"Drive file {meta.name!r} is a native Google Sheet, but this "
                "build expects a real .xlsx file. Either upload the workbook as "
                "an .xlsx, or use File > Download > Microsoft Excel and put the "
                "result in Drive."
            )
        if meta.mime_type != XLSX_MIME:
            raise DriveFileError(
                f"Drive file {meta.name!r} has type {meta.mime_type!r}; "
                f"expected an .xlsx workbook ({XLSX_MIME})."
            )

        buffer = io.BytesIO()
        request = self._service.files().get_media(fileId=file_id, supportsAllDrives=True)
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()

        data = buffer.getvalue()
        logger.info(
            "Downloaded %s (%d bytes, modified %s)",
            meta.name,
            len(data),
            meta.modified_time,
        )
        return data
