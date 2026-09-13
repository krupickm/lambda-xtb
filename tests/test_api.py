"""Acceptance tests for WP4 — frontend internal API + auth (app.py).

Uses the Flask test client; no real network. DB rows are seeded directly via
the WP1 SQLite helpers against a temp-file database swapped into `app._database`
so tests never touch the real `data/lambda.db`. The dead-worker reconciliation
hook (`app._reconcile_job`) is monkeypatched per-test rather than exercised for
real — real k8s reconciliation is WP6, out of scope here.
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


def _auth_headers(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def _pending_job(db, email=None):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1", email=email)
    return job_uuid


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


# ── /start ───────────────────────────────────────────────────────────────

def test_start_flips_pending_to_processing(db, client):
    job_uuid = _pending_job(db)

    resp = client.post(f"/api/jobs/{job_uuid}/start", headers=_auth_headers())

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert db.get_status(job_uuid)["status"] == int(JobStatus.PROCESSING)


def test_start_on_terminal_job_returns_409(db, client):
    job_uuid = _pending_job(db)
    db.update_result(job_uuid, _result_payload(), "n", "c", "a")

    resp = client.post(f"/api/jobs/{job_uuid}/start", headers=_auth_headers())

    assert resp.status_code == 409
    assert db.get_status(job_uuid)["status"] == int(JobStatus.DONE)


def test_start_unknown_job_returns_404(db, client):
    resp = client.post(f"/api/jobs/{uuid.uuid4()}/start", headers=_auth_headers())
    assert resp.status_code == 404


# ── /result ──────────────────────────────────────────────────────────────

def test_result_transitions_to_done_and_status_redirects_to_stats(db, client):
    job_uuid = _pending_job(db)
    db.mark_processing(job_uuid)

    resp = client.post(
        f"/api/jobs/{job_uuid}/result", headers=_auth_headers(), json=_result_payload()
    )
    assert resp.status_code == 200

    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.DONE)
    assert job["lambda_plus_eV"] == 0.25
    assert job["xyz_neutral"] == "XYZ:neutral"

    status_resp = client.get(f"/api/jobs/{job_uuid}/status")
    body = status_resp.get_json()
    assert body["status"] == "DONE"
    assert body["redirect"] == f"/stats/{job_uuid}"
    assert body["error"] is None


def test_result_from_pending_skips_processing(db, client):
    """A fast job may skip a visible PROCESSING step (per API contract)."""
    job_uuid = _pending_job(db)

    resp = client.post(
        f"/api/jobs/{job_uuid}/result", headers=_auth_headers(), json=_result_payload()
    )

    assert resp.status_code == 200
    assert db.get_status(job_uuid)["status"] == int(JobStatus.DONE)


def test_result_unknown_job_returns_404(db, client):
    resp = client.post(
        f"/api/jobs/{uuid.uuid4()}/result", headers=_auth_headers(), json=_result_payload()
    )
    assert resp.status_code == 404


# ── /error ───────────────────────────────────────────────────────────────

def test_error_transitions_to_error_and_status_surfaces_message(db, client):
    job_uuid = _pending_job(db)
    db.mark_processing(job_uuid)

    resp = client.post(
        f"/api/jobs/{job_uuid}/error", headers=_auth_headers(), json={"message": "xtb crashed"}
    )
    assert resp.status_code == 200

    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.ERROR)
    assert job["error_message"] == "xtb crashed"

    status_resp = client.get(f"/api/jobs/{job_uuid}/status")
    body = status_resp.get_json()
    assert body["status"] == "ERROR"
    assert body["redirect"] is None
    assert body["error"] == "xtb crashed"


def test_error_unknown_job_returns_404(db, client):
    resp = client.post(
        f"/api/jobs/{uuid.uuid4()}/error", headers=_auth_headers(), json={"message": "boom"}
    )
    assert resp.status_code == 404


# ── Bearer auth ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("endpoint", ["start", "result", "error"])
def test_missing_bearer_returns_401(db, client, endpoint):
    job_uuid = _pending_job(db)

    resp = client.post(f"/api/jobs/{job_uuid}/{endpoint}", json={})

    assert resp.status_code == 401
    # Untouched: still PENDING.
    assert db.get_status(job_uuid)["status"] == int(JobStatus.PENDING)


@pytest.mark.parametrize("endpoint", ["start", "result", "error"])
def test_wrong_bearer_returns_401(db, client, endpoint):
    job_uuid = _pending_job(db)

    resp = client.post(
        f"/api/jobs/{job_uuid}/{endpoint}", headers=_auth_headers("wrong-token"), json={}
    )

    assert resp.status_code == 401
    assert db.get_status(job_uuid)["status"] == int(JobStatus.PENDING)


def test_valid_token_returns_200_on_start(db, client):
    job_uuid = _pending_job(db)
    resp = client.post(f"/api/jobs/{job_uuid}/start", headers=_auth_headers())
    assert resp.status_code == 200


def test_status_endpoint_requires_no_auth(db, client):
    """The poll-facing /status endpoint is capability-based (uuid only), no Bearer."""
    job_uuid = _pending_job(db)
    resp = client.get(f"/api/jobs/{job_uuid}/status")
    assert resp.status_code == 200


# ── idempotency ──────────────────────────────────────────────────────────

def test_repeated_start_after_processing_is_still_200_and_not_terminal(db, client):
    job_uuid = _pending_job(db)

    first = client.post(f"/api/jobs/{job_uuid}/start", headers=_auth_headers())
    second = client.post(f"/api/jobs/{job_uuid}/start", headers=_auth_headers())

    assert first.status_code == 200
    assert second.status_code == 200
    assert db.get_status(job_uuid)["status"] == int(JobStatus.PROCESSING)


def test_repeated_result_does_not_corrupt_done_row(db, client):
    job_uuid = _pending_job(db)
    first = client.post(
        f"/api/jobs/{job_uuid}/result",
        headers=_auth_headers(),
        json=_result_payload(lambda_plus_eV=0.5),
    )
    second = client.post(
        f"/api/jobs/{job_uuid}/result",
        headers=_auth_headers(),
        json=_result_payload(lambda_plus_eV=9.9, xyz_neutral="CLOBBERED"),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.DONE)
    assert job["lambda_plus_eV"] == 0.5
    assert job["xyz_neutral"] == "XYZ:neutral"


def test_error_after_done_does_not_corrupt_row(db, client):
    job_uuid = _pending_job(db)
    client.post(f"/api/jobs/{job_uuid}/result", headers=_auth_headers(), json=_result_payload())

    resp = client.post(
        f"/api/jobs/{job_uuid}/error", headers=_auth_headers(), json={"message": "too late"}
    )

    assert resp.status_code == 200
    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.DONE)
    assert job["error_message"] is None


# ── /status shape per state ─────────────────────────────────────────────

def test_status_shape_for_pending(db, client):
    job_uuid = _pending_job(db)
    resp = client.get(f"/api/jobs/{job_uuid}/status")
    assert resp.get_json() == {"status": "PENDING", "redirect": None, "error": None}


def test_status_shape_for_processing(db, client):
    job_uuid = _pending_job(db)
    db.mark_processing(job_uuid)
    resp = client.get(f"/api/jobs/{job_uuid}/status")
    assert resp.get_json() == {"status": "PROCESSING", "redirect": None, "error": None}


def test_status_unknown_job_returns_404(client, db):
    resp = client.get(f"/api/jobs/{uuid.uuid4()}/status")
    assert resp.status_code == 404


# ── reconciliation hook is injectable (real impl is WP6) ────────────────

def test_status_calls_reconcile_hook_for_non_terminal_job(db, client, monkeypatch):
    job_uuid = _pending_job(db)
    calls = []

    def _fake_reconcile(uuid_arg):
        calls.append(uuid_arg)

    monkeypatch.setattr(app_module, "_reconcile_job", _fake_reconcile)

    client.get(f"/api/jobs/{job_uuid}/status")

    assert calls == [job_uuid]


def test_status_reconcile_hook_can_mark_job_error(db, client, monkeypatch):
    """Simulates WP6 detecting a silently-dead worker during a poll."""
    job_uuid = _pending_job(db)

    def _fake_reconcile(uuid_arg):
        db.update_error(uuid_arg, "compute pod terminated unexpectedly")

    monkeypatch.setattr(app_module, "_reconcile_job", _fake_reconcile)

    resp = client.get(f"/api/jobs/{job_uuid}/status")
    body = resp.get_json()

    assert body["status"] == "ERROR"
    assert body["error"] == "compute pod terminated unexpectedly"


def test_status_does_not_call_reconcile_hook_for_terminal_job(db, client, monkeypatch):
    job_uuid = _pending_job(db)
    db.update_result(job_uuid, _result_payload(), "n", "c", "a")
    calls = []
    monkeypatch.setattr(app_module, "_reconcile_job", lambda uuid_arg: calls.append(uuid_arg))

    client.get(f"/api/jobs/{job_uuid}/status")

    assert calls == []
