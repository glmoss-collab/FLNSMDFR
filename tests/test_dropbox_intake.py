"""
Tests for the Dropbox estimation intake pipeline.

These tests exercise discovery, idempotency, artifact generation, and the
webhook path using a fake Dropbox client and an injected estimator, so no
network access or Anthropic API key is required.
"""

import hashlib
import hmac

import pytest

from dropbox_intake import (
    DropboxEstimationIntake,
    SUMMARY_FILENAME,
    build_quote_artifacts,
)
from dropbox_storage import LocalFolderClient, make_intake_client


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeFile:
    def __init__(self, name, content_hash, path):
        self.name = name
        self.content_hash = content_hash
        self.path = path
        self.rev = "rev-" + content_hash
        self.size = 1024


class FakeDropboxClient:
    """In-memory stand-in for dropbox_storage.DropboxClient."""

    def __init__(self, folders, contents=None, app_secret=None):
        # folders: {folder_path: [FakeFile, ...]}
        self.folders = folders
        self.contents = contents or {}
        self._app_secret = app_secret
        self.uploads = {}  # dest path -> bytes

    def list_project_folders(self, root_path="/"):
        return sorted(self.folders.keys())

    def list_pdf_files(self, folder_path):
        return list(self.folders.get(folder_path, []))

    def download_file(self, path):
        return self.contents.get(path, b"%PDF-1.4 fake")

    def upload_file(self, path, data, overwrite=True):
        self.uploads[path] = data
        return path

    @staticmethod
    def join_path(folder_path, name):
        # Mirror DropboxClient.join_path (Dropbox forward-slash semantics).
        return f"{folder_path.rstrip('/')}/{name}"

    def verify_webhook_signature(self, body, signature):
        if not self._app_secret or not signature:
            return False
        expected = hmac.new(self._app_secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


class FakeCache:
    def __init__(self):
        self.store = {}

    def get(self, key, category="api_responses"):
        return self.store.get((category, key))

    def set(self, key, value, category="api_responses", ttl=None):
        self.store[(category, key)] = value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_session_data():
    return {
        "project_info": {
            "project_name": "ACME Office Tower",
            "project_number": "2026-014",
            "client": "ACME Corp",
            "location": "Boston, MA",
        },
        "specifications": [
            {
                "system_type": "supply_duct",
                "size_range": "all",
                "thickness": 2.0,
                "material": "fiberglass",
                "facing": "FSK",
                "special_requirements": ["mastic_seal"],
                "location": "indoor",
                "page_number": 5,
            }
        ],
        "measurements": [
            {
                "item_id": "D-001",
                "system_type": "duct",
                "size": "18x12",
                "length": 120.0,
                "location": "Mechanical Room",
                "fittings": {"elbow": 3, "tee": 1},
            }
        ],
    }


@pytest.fixture
def estimator(sample_session_data):
    def _estimate(local_paths):
        assert local_paths, "estimator should receive at least one local path"
        return sample_session_data
    return _estimate


# ---------------------------------------------------------------------------
# build_quote_artifacts
# ---------------------------------------------------------------------------

def test_build_quote_artifacts_produces_quote_and_summary(sample_session_data):
    artifacts = build_quote_artifacts("ACME Office Tower", sample_session_data)

    filenames = list(artifacts["files"].keys())
    assert SUMMARY_FILENAME in filenames
    quote_files = [f for f in filenames if f.startswith("Quote_") and f.endswith(".txt")]
    assert quote_files, f"expected a quote file, got {filenames}"

    summary = artifacts["files"][SUMMARY_FILENAME].decode("utf-8")
    assert "Estimate Summary" in summary
    assert "ACME Office Tower" in summary
    assert "Total" in summary

    quote = artifacts["quote"]
    assert quote["spec_count"] == 1
    assert quote["measurement_count"] == 1
    assert quote["total"] >= 0


def test_build_quote_artifacts_handles_empty_session():
    artifacts = build_quote_artifacts("Empty Project", {})
    # Summary is always produced even with nothing extracted.
    assert SUMMARY_FILENAME in artifacts["files"]
    summary = artifacts["files"][SUMMARY_FILENAME].decode("utf-8")
    assert "Empty Project" in summary


# ---------------------------------------------------------------------------
# discovery + processing
# ---------------------------------------------------------------------------

def _make_intake(folders, estimator, cache=None, app_secret=None):
    client = FakeDropboxClient(folders, app_secret=app_secret)
    intake = DropboxEstimationIntake(
        dropbox_client=client,
        root_path="/Projects",
        estimator=estimator,
        cache=cache or FakeCache(),
    )
    return intake, client


def test_discover_skips_folders_without_pdfs(estimator):
    folders = {
        "/Projects/HasPdf": [FakeFile("specs.pdf", "h1", "/Projects/HasPdf/specs.pdf")],
        "/Projects/Empty": [],
    }
    intake, _ = _make_intake(folders, estimator)
    assert intake.discover_new_projects() == ["/Projects/HasPdf"]


def test_process_project_uploads_quote_and_summary(estimator):
    folders = {
        "/Projects/ACME": [FakeFile("specs.pdf", "h1", "/Projects/ACME/specs.pdf")],
    }
    intake, client = _make_intake(folders, estimator)

    result = intake.process_project("/Projects/ACME")

    assert result["success"] is True
    assert result["folder"] == "/Projects/ACME"
    # Two artifacts written back into the same folder.
    assert len(client.uploads) == 2
    assert any(p.endswith(SUMMARY_FILENAME) for p in client.uploads)
    assert any("/Projects/ACME/Quote_" in p for p in client.uploads)


def test_process_project_is_idempotent(estimator):
    folders = {
        "/Projects/ACME": [FakeFile("specs.pdf", "h1", "/Projects/ACME/specs.pdf")],
    }
    cache = FakeCache()
    intake, client = _make_intake(folders, estimator, cache=cache)

    first = intake.process_project("/Projects/ACME")
    assert first["success"] and not first.get("skipped")

    # Re-run with unchanged contents -> skipped, no new uploads.
    client.uploads.clear()
    second = intake.process_project("/Projects/ACME")
    assert second["skipped"] is True
    assert client.uploads == {}

    # Discovery should no longer surface the unchanged project.
    assert intake.discover_new_projects() == []


def test_changed_contents_reprocess(estimator):
    folders = {
        "/Projects/ACME": [FakeFile("specs.pdf", "h1", "/Projects/ACME/specs.pdf")],
    }
    cache = FakeCache()
    intake, client = _make_intake(folders, estimator, cache=cache)
    intake.process_project("/Projects/ACME")

    # Simulate a re-upload of the spec with new contents.
    folders["/Projects/ACME"] = [FakeFile("specs.pdf", "h2", "/Projects/ACME/specs.pdf")]
    assert intake.discover_new_projects() == ["/Projects/ACME"]

    client.uploads.clear()
    again = intake.process_project("/Projects/ACME")
    assert again["success"] and not again.get("skipped")
    assert len(client.uploads) == 2


def test_force_reprocesses_unchanged(estimator):
    folders = {
        "/Projects/ACME": [FakeFile("specs.pdf", "h1", "/Projects/ACME/specs.pdf")],
    }
    cache = FakeCache()
    intake, _ = _make_intake(folders, estimator, cache=cache)
    intake.process_project("/Projects/ACME")

    forced = intake.process_project("/Projects/ACME", force=True)
    assert forced["success"] and not forced.get("skipped")


def test_process_new_projects_processes_all(estimator):
    folders = {
        "/Projects/A": [FakeFile("a.pdf", "ha", "/Projects/A/a.pdf")],
        "/Projects/B": [FakeFile("b.pdf", "hb", "/Projects/B/b.pdf")],
    }
    intake, _ = _make_intake(folders, estimator)
    results = intake.process_new_projects()
    assert len(results) == 2
    assert all(r["success"] for r in results)


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------

def test_handle_webhook_rejects_bad_signature(estimator):
    folders = {"/Projects/A": [FakeFile("a.pdf", "ha", "/Projects/A/a.pdf")]}
    intake, _ = _make_intake(folders, estimator, app_secret="s3cr3t")

    result = intake.handle_webhook(b"{}", "deadbeef")
    assert result["success"] is False
    assert "signature" in result["error"]


def test_handle_webhook_accepts_valid_signature(estimator):
    folders = {"/Projects/A": [FakeFile("a.pdf", "ha", "/Projects/A/a.pdf")]}
    intake, client = _make_intake(folders, estimator, app_secret="s3cr3t")

    body = b'{"list_folder": {}}'
    signature = hmac.new(b"s3cr3t", body, hashlib.sha256).hexdigest()
    result = intake.handle_webhook(body, signature)

    assert result["success"] is True
    assert len(result["processed"]) == 1
    assert client.uploads  # artifacts were written


# ---------------------------------------------------------------------------
# LocalFolderClient (synced Dropbox folder on disk)
# ---------------------------------------------------------------------------

def test_make_intake_client_picks_local_for_existing_dir(tmp_path):
    client = make_intake_client(str(tmp_path))
    assert isinstance(client, LocalFolderClient)


def test_local_intake_end_to_end(tmp_path, estimator):
    root = tmp_path / "Stiles"
    project = root / "ACME Office Tower"
    project.mkdir(parents=True)
    (project / "specs.pdf").write_bytes(b"%PDF-1.4 fake spec")
    (project / "drawings.pdf").write_bytes(b"%PDF-1.4 fake drawing")

    client = LocalFolderClient(root=str(root))
    intake = DropboxEstimationIntake(
        dropbox_client=client,
        root_path=str(root),
        estimator=estimator,
        cache=FakeCache(),
    )

    assert intake.discover_new_projects() == [str(project)]

    result = intake.process_project(str(project))
    assert result["success"] and not result.get("skipped")

    # Artifacts written into the project folder (Dropbox would sync these up).
    written = {p.name for p in project.iterdir()}
    assert SUMMARY_FILENAME in written
    assert any(n.startswith("Quote_") and n.endswith(".txt") for n in written)

    # PDF-only fingerprint means our .txt/.md artifacts don't trigger reprocessing.
    assert intake.discover_new_projects() == []


def test_local_intake_skips_folder_without_pdfs(tmp_path, estimator):
    root = tmp_path / "Stiles"
    (root / "EmptyProject").mkdir(parents=True)

    client = LocalFolderClient(root=str(root))
    intake = DropboxEstimationIntake(
        dropbox_client=client,
        root_path=str(root),
        estimator=estimator,
        cache=FakeCache(),
    )
    assert intake.discover_new_projects() == []
