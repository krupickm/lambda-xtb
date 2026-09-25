"""Acceptance tests for WP5 — frontend async flow + templates (app.py).

Uses the Flask test client against a throwaway SQLite file swapped into
`app._database`, exactly like `tests/test_api.py`. `app._create_compute_job`
(the injected hook that wraps `jobs.create_compute_job`, WP6) is
monkeypatched per-test so no real k8s Job is ever created. The client-side
polling/redirect JS in `templates/pending.html` isn't executed by these
tests (no browser); instead we verify the page renders and wires up to the
same `/api/jobs/<uuid>/status` endpoint the JS calls, and that hitting that
endpoint for a simulated DONE/ERROR row returns the redirect/error the JS
acts on.
"""

import uuid

import pytest

import app as app_module
from db import JobStatus, SQLiteDatabase

TOKEN = "test-callback-token"


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Swap app._database for a throwaway SQLite file and set the auth token."""
    database = SQLiteDatabase(str(tmp_path / "test.db"))
    database.init_db()
    monkeypatch.setattr(app_module, "_database", database)
    monkeypatch.setenv("CALLBACK_TOKEN", TOKEN)
    return database


@pytest.fixture
def client():
    app_module.app.testing = True
    return app_module.app.test_client()


@pytest.fixture
def mock_create_compute_job(monkeypatch):
    """Stub out `app._create_compute_job` and record every call made to it."""
    calls = []

    def _fake(job_uuid, smiles):
        calls.append((job_uuid, smiles))

    monkeypatch.setattr(app_module, "_create_compute_job", _fake)
    return calls


def _result_payload(**overrides):
    base = {
        "lambda_plus_eV": 0.25,
        "lambda_minus_eV": 0.31,
        "partial": {"E0_geo0": -1.0},
        "xyz_neutral": "XYZ:neutral",
        "xyz_cation": "XYZ:cation",
        "xyz_anion": "XYZ:anion",
    }
    base.update(overrides)
    return base


# ── POST /calculate — valid submission ──────────────────────────────────────

def test_valid_submit_creates_pending_row_and_calls_hook_once(db, client, mock_create_compute_job):
    resp = client.post("/calculate", data={"smiles": "c1ccccc1"})

    assert resp.status_code == 302
    assert len(mock_create_compute_job) == 1


def test_valid_submit_redirects_to_pending_page(db, client, mock_create_compute_job):
    resp = client.post("/calculate", data={"smiles": "c1ccccc1"})

    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/pending/")

    job_uuid = resp.headers["Location"].rsplit("/", 1)[-1]
    row = db.get_status(job_uuid)
    assert row is not None
    assert row["status"] == int(JobStatus.PENDING)


def test_valid_submit_passes_canonical_smiles_and_uuid_to_hook(db, client, mock_create_compute_job):
    resp = client.post("/calculate", data={"smiles": "c1ccccc1"})
    job_uuid = resp.headers["Location"].rsplit("/", 1)[-1]

    called_uuid, called_smiles = mock_create_compute_job[0]
    assert called_uuid == job_uuid
    assert called_smiles == "c1ccccc1"


def test_valid_submit_with_email_stores_it_on_the_row(db, client, mock_create_compute_job):
    resp = client.post("/calculate", data={"smiles": "c1ccccc1", "email": "user@example.com"})
    job_uuid = resp.headers["Location"].rsplit("/", 1)[-1]

    job = db.get_job(job_uuid)
    assert job["email"] == "user@example.com"


def test_valid_submit_without_email_is_optional(db, client, mock_create_compute_job):
    resp = client.post("/calculate", data={"smiles": "c1ccccc1"})
    job_uuid = resp.headers["Location"].rsplit("/", 1)[-1]

    job = db.get_job(job_uuid)
    assert job["email"] is None


# ── POST /calculate — invalid submissions ───────────────────────────────────

def test_empty_smiles_flashes_and_creates_no_row(db, client, mock_create_compute_job):
    resp = client.post("/calculate", data={"smiles": ""}, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Please provide a SMILES string." in resp.data
    assert mock_create_compute_job == []


def test_invalid_smiles_flashes_and_creates_no_row(db, client, mock_create_compute_job):
    resp = client.post("/calculate", data={"smiles": "not-a-smiles!!"}, follow_redirects=True)

    assert resp.status_code == 200
    assert b"Invalid SMILES" in resp.data
    assert mock_create_compute_job == []


def test_invalid_smiles_does_not_touch_the_database(db, client, mock_create_compute_job):
    client.post("/calculate", data={"smiles": "not-a-smiles!!"})

    # No way to list all rows via the public interface; canonical lookup for
    # a made-up SMILES must stay empty, and the compute-job hook must be untouched.
    assert db.find_all_by_canonical("not-a-smiles!!") == []
    assert mock_create_compute_job == []


def test_invalid_smiles_redirects_to_index(db, client, mock_create_compute_job):
    resp = client.post("/calculate", data={"smiles": "not-a-smiles!!"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


# ── GET /pending/<uuid> ──────────────────────────────────────────────────────

def test_pending_page_renders_for_a_pending_job(db, client):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")

    resp = client.get(f"/pending/{job_uuid}")

    assert resp.status_code == 200
    # The page's JS must poll the same status endpoint the API contract defines.
    assert f"/api/jobs/{job_uuid}/status".encode() in resp.data


def test_pending_page_unknown_uuid_redirects_to_index(db, client):
    resp = client.get(f"/pending/{uuid.uuid4()}")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/"


# ── client-side redirect target, driven by /status ──────────────────────────
# The pending page's JS reads {status, redirect, error} from GET /status and
# navigates accordingly. These tests simulate each terminal outcome at the
# DB layer (as the worker/reconciliation would produce it) and assert the
# JSON the JS would act on carries the correct navigation target.

def test_status_drives_redirect_to_stats_on_done(db, client):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")
    db.update_result(job_uuid, _result_payload(), "n", "c", "a")

    resp = client.get(f"/api/jobs/{job_uuid}/status")
    body = resp.get_json()

    assert body["status"] == "DONE"
    assert body["redirect"] == f"/stats/{job_uuid}"
    assert body["error"] is None


def test_status_carries_error_message_for_error_redirect(db, client):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")
    db.update_error(job_uuid, "xtb crashed")

    resp = client.get(f"/api/jobs/{job_uuid}/status")
    body = resp.get_json()

    assert body["status"] == "ERROR"
    assert body["redirect"] is None
    assert body["error"] == "xtb crashed"


def test_status_has_no_redirect_while_pending(db, client):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")

    resp = client.get(f"/api/jobs/{job_uuid}/status")
    body = resp.get_json()

    assert body["status"] == "PENDING"
    assert body["redirect"] is None
    assert body["error"] is None


# ── index() surfaces the pending page's error redirect as a flash ──────────

def test_index_error_query_param_becomes_flash_message(db, client):
    resp = client.get("/?error=Calculation+failed%3A+xtb+crashed", follow_redirects=True)

    assert resp.status_code == 200
    assert b"Calculation failed: xtb crashed" in resp.data


# ── index page has the optional email field ─────────────────────────────────

def test_index_page_has_email_field(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b'name="email"' in resp.data


def test_index_page_prefills_email_from_auth_header(client):
    resp = client.get("/", headers={"GAP-Auth": "user@vscht.cz"})
    assert resp.status_code == 200
    assert b'value="user@vscht.cz"' in resp.data


def test_index_page_email_field_blank_without_auth_header(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b'value=""' in resp.data
