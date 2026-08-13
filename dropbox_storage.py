"""
Dropbox Integration Module
==========================

Thin wrapper around the official Dropbox SDK used by the estimation intake
pipeline. It mirrors the conventions already used in this repo:

* Credentials resolve through :mod:`secrets_manager` (env var first, then GCP
  Secret Manager), so deployments can mount ``dropbox-access-token`` /
  ``dropbox-app-secret`` the same way ``anthropic-api-key`` is mounted today.
* The ``dropbox`` SDK is imported lazily so the rest of the codebase (and the
  test suite) does not hard-depend on it being installed.

Token wiring is intentionally left as a documented placeholder (see
``DROPBOX_ACCESS_TOKEN`` below); drop a real long-lived access token (or a
refresh token, see ``DropboxClient``) into the environment or Secret Manager
to activate the live integration.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any, List, Optional

from secrets_manager import get_secret

logger = logging.getLogger(__name__)

# Secret Manager IDs / env var names (env var = ID upper-cased, '-' -> '_').
ACCESS_TOKEN_SECRET = "dropbox-access-token"  # env: DROPBOX_ACCESS_TOKEN
APP_SECRET_SECRET = "dropbox-app-secret"      # env: DROPBOX_APP_SECRET
REFRESH_TOKEN_SECRET = "dropbox-refresh-token"  # env: DROPBOX_REFRESH_TOKEN
APP_KEY_SECRET = "dropbox-app-key"            # env: DROPBOX_APP_KEY

_PDF_SUFFIXES = (".pdf",)


def _load_sdk():
    """Lazily import the Dropbox SDK with a clear error if it is missing."""
    try:
        import dropbox  # noqa: F401
        return dropbox
    except ImportError as exc:  # pragma: no cover - exercised only without dep
        raise ImportError(
            "The 'dropbox' package is required for Dropbox intake. "
            "Install it with `pip install dropbox==12.0.2` (already pinned in "
            "requirements.txt)."
        ) from exc


class DropboxFile:
    """Lightweight, SDK-agnostic view of a Dropbox file entry."""

    __slots__ = ("path", "name", "content_hash", "rev", "size")

    def __init__(self, path: str, name: str, content_hash: str = "", rev: str = "", size: int = 0):
        self.path = path
        self.name = name
        self.content_hash = content_hash
        self.rev = rev
        self.size = size

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DropboxFile(path={self.path!r}, content_hash={self.content_hash!r})"


class DropboxClient:
    """Minimal Dropbox client covering the intake pipeline's needs.

    Authentication precedence:

    1. A long-lived ``access_token`` (passed in, env ``DROPBOX_ACCESS_TOKEN``,
       or Secret Manager ``dropbox-access-token``).
    2. A ``refresh_token`` + ``app_key`` pair (env ``DROPBOX_REFRESH_TOKEN`` /
       ``DROPBOX_APP_KEY`` or the matching secrets), which the SDK uses to mint
       short-lived access tokens automatically.

    The ``app_secret`` (env ``DROPBOX_APP_SECRET``) is only needed to verify
    incoming webhook signatures and is resolved lazily.
    """

    def __init__(
        self,
        access_token: Optional[str] = None,
        refresh_token: Optional[str] = None,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
    ):
        self._access_token = access_token or get_secret(ACCESS_TOKEN_SECRET)
        self._refresh_token = refresh_token or get_secret(REFRESH_TOKEN_SECRET)
        self._app_key = app_key or get_secret(APP_KEY_SECRET)
        self._app_secret = app_secret or get_secret(APP_SECRET_SECRET)
        self._dbx = None

    @property
    def app_secret(self) -> Optional[str]:
        return self._app_secret

    def _client(self):
        """Build (once) and return the underlying ``dropbox.Dropbox`` client."""
        if self._dbx is not None:
            return self._dbx

        dropbox = _load_sdk()

        if self._access_token:
            self._dbx = dropbox.Dropbox(oauth2_access_token=self._access_token)
        elif self._refresh_token and self._app_key:
            self._dbx = dropbox.Dropbox(
                oauth2_refresh_token=self._refresh_token,
                app_key=self._app_key,
                app_secret=self._app_secret,
            )
        else:
            raise ValueError(
                "Dropbox credentials are not configured. Set DROPBOX_ACCESS_TOKEN "
                "(or DROPBOX_REFRESH_TOKEN + DROPBOX_APP_KEY) via environment or "
                "Secret Manager."
            )
        return self._dbx

    # -- listing -----------------------------------------------------------

    def _list_all(self, path: str):
        """Yield every entry under ``path`` (handles pagination)."""
        dbx = self._client()
        # Dropbox treats the root as "" rather than "/".
        api_path = "" if path in ("", "/") else path.rstrip("/")
        result = dbx.files_list_folder(api_path)
        for entry in result.entries:
            yield entry
        while result.has_more:
            result = dbx.files_list_folder_continue(result.cursor)
            for entry in result.entries:
                yield entry

    def list_project_folders(self, root_path: str = "/") -> List[str]:
        """Return the immediate sub-folder paths under ``root_path``.

        Each sub-folder is treated as one "project" by the intake pipeline.
        """
        dropbox = _load_sdk()
        folders: List[str] = []
        for entry in self._list_all(root_path):
            if isinstance(entry, dropbox.files.FolderMetadata):
                folders.append(entry.path_display)
        return sorted(folders)

    def list_pdf_files(self, folder_path: str) -> List[DropboxFile]:
        """Return PDF files directly inside ``folder_path``."""
        dropbox = _load_sdk()
        files: List[DropboxFile] = []
        for entry in self._list_all(folder_path):
            if isinstance(entry, dropbox.files.FileMetadata) and entry.name.lower().endswith(_PDF_SUFFIXES):
                files.append(
                    DropboxFile(
                        path=entry.path_display,
                        name=entry.name,
                        content_hash=getattr(entry, "content_hash", "") or "",
                        rev=getattr(entry, "rev", "") or "",
                        size=getattr(entry, "size", 0) or 0,
                    )
                )
        return sorted(files, key=lambda f: f.name)

    # -- transfer ----------------------------------------------------------

    def download_file(self, path: str) -> bytes:
        """Download a file's contents as bytes."""
        dbx = self._client()
        _metadata, response = dbx.files_download(path)
        return response.content

    def upload_file(self, path: str, data: bytes, overwrite: bool = True) -> str:
        """Upload ``data`` to ``path`` and return the resulting display path."""
        dropbox = _load_sdk()
        dbx = self._client()
        mode = (
            dropbox.files.WriteMode.overwrite
            if overwrite
            else dropbox.files.WriteMode.add
        )
        metadata = dbx.files_upload(data, path, mode=mode)
        return metadata.path_display

    def file_exists(self, path: str) -> bool:
        dropbox = _load_sdk()
        dbx = self._client()
        try:
            dbx.files_get_metadata(path)
            return True
        except dropbox.exceptions.ApiError:
            return False

    @staticmethod
    def join_path(folder_path: str, name: str) -> str:
        """Join a folder and file name using Dropbox path semantics."""
        return f"{folder_path.rstrip('/')}/{name}"

    # -- webhooks ----------------------------------------------------------

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:
        """Verify a Dropbox webhook ``X-Dropbox-Signature`` header.

        Dropbox signs the raw request body with HMAC-SHA256 keyed by the app
        secret. Returns False (rather than raising) when no app secret is
        configured so callers can fail closed.
        """
        if not self._app_secret or not signature:
            return False
        expected = hmac.new(
            self._app_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)


class LocalFolderClient:
    """Filesystem-backed client for a locally-synced Dropbox folder.

    When the Dropbox desktop app syncs a team folder to disk, new projects
    appear as ordinary sub-directories. This client exposes the same surface
    the intake pipeline expects (``list_project_folders`` / ``list_pdf_files``
    / ``download_file`` / ``upload_file``), so artifacts written back into a
    project folder are picked up by Dropbox sync automatically -- no API token
    or webhook required.

    File ``content_hash`` is derived from size + modification time, which is
    cheap and sufficient for change detection (re-syncing the same file keeps
    the same fingerprint; editing it changes it).
    """

    def __init__(self, root: Optional[str] = None):
        self.root = root

    def list_project_folders(self, root_path: Optional[str] = None) -> List[str]:
        base = root_path or self.root
        if not base:
            raise ValueError("LocalFolderClient requires a root path")
        base_path = os.path.abspath(base)
        if not os.path.isdir(base_path):
            return []
        return sorted(
            entry.path
            for entry in os.scandir(base_path)
            if entry.is_dir()
        )

    # Sub-folders never scanned for source PDFs. "draft estimate" is the
    # pipeline's own output folder (dropbox_intake.OUTPUT_SUBFOLDER) -- keep
    # the two names in sync if either changes.
    SKIP_DIR_NAMES = {"draft estimate"}

    def list_pdf_files(self, folder_path: str) -> List[DropboxFile]:
        """Return PDFs under ``folder_path``, recursively.

        Project folders keep PDFs in sub-folders (``Drawings/``, ``Specs/``,
        ``Addendum/`` per the Stiles SOP), so the walk descends into them.
        ``name`` is the path relative to the project folder (forward slashes)
        so files with the same basename in different sub-folders stay distinct
        in fingerprints and downloads.
        """
        if not os.path.isdir(folder_path):
            return []
        files: List[DropboxFile] = []
        for dirpath, dirnames, filenames in os.walk(folder_path):
            dirnames[:] = [d for d in dirnames if d.lower() not in self.SKIP_DIR_NAMES]
            for filename in filenames:
                if not filename.lower().endswith(_PDF_SUFFIXES):
                    continue
                full_path = os.path.join(dirpath, filename)
                stat = os.stat(full_path)
                rel_name = os.path.relpath(full_path, folder_path).replace(os.sep, "/")
                files.append(
                    DropboxFile(
                        path=full_path,
                        name=rel_name,
                        content_hash=f"{stat.st_size}-{int(stat.st_mtime)}",
                        size=stat.st_size,
                    )
                )
        return sorted(files, key=lambda f: f.name)

    def download_file(self, path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    def upload_file(self, path: str, data: bytes, overwrite: bool = True) -> str:
        if not overwrite and os.path.exists(path):
            return path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def file_exists(self, path: str) -> bool:
        return os.path.exists(path)

    @staticmethod
    def join_path(folder_path: str, name: str) -> str:
        return os.path.join(folder_path, name)

    def verify_webhook_signature(self, body: bytes, signature: str) -> bool:  # pragma: no cover - n/a for local
        return False


def make_intake_client(root_path: Optional[str] = None) -> Any:
    """Pick the right client for the intake pipeline.

    Uses :class:`LocalFolderClient` when ``DROPBOX_LOCAL_ROOT`` is set or when
    ``root_path`` points at an existing local directory (the common case when
    the Dropbox desktop app syncs a team folder to disk). Otherwise falls back
    to the API-backed :class:`DropboxClient`.
    """
    local_root = os.getenv("DROPBOX_LOCAL_ROOT")
    if local_root:
        return LocalFolderClient(root=local_root)
    if root_path and root_path not in ("/", "") and os.path.isdir(root_path):
        return LocalFolderClient(root=root_path)
    return DropboxClient()
