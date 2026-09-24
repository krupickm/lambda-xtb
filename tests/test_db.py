"""Tests for the PENDING->PROCESSING->DONE/ERROR job lifecycle in db.py (WP1)."""

import uuid

import pytest

from db import JobStatus, SQLiteDatabase


@pytest.fixture
def db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "test.db"))
    database.init_db()
    return database


def _results(**overrides) -> dict:
    base = {
        "lambda_plus_eV": 0.5,
        "lambda_minus_eV": 0.4,
        "partial": {"a": 1},
    }
    base.update(overrides)
    return base


def test_create_pending_job(db):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1", email="a@b.com")

    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.PENDING)
    assert job["smiles_input"] == "c1ccccc1"
    assert job["smiles_canonical"] == "c1ccccc1"
    assert job["email"] == "a@b.com"


def test_mark_processing_from_pending(db):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")

    db.mark_processing(job_uuid)

    assert db.get_job(job_uuid)["status"] == int(JobStatus.PROCESSING)


def test_update_result_stores_lambda_values_and_xyz(db):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")
    db.mark_processing(job_uuid)

    db.update_result(job_uuid, _results(), "xyz-neutral", "xyz-cation", "xyz-anion")

    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.DONE)
    assert job["lambda_plus_eV"] == 0.5
    assert job["lambda_minus_eV"] == 0.4
    assert job["xyz_neutral"] == "xyz-neutral"
    assert job["xyz_cation"] == "xyz-cation"
    assert job["xyz_anion"] == "xyz-anion"


def test_update_result_directly_from_pending(db):
    """A fast job may skip a visible PROCESSING step (per API contract)."""
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")

    db.update_result(job_uuid, _results(), "n", "c", "a")

    assert db.get_job(job_uuid)["status"] == int(JobStatus.DONE)


def test_update_error_from_processing(db):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")
    db.mark_processing(job_uuid)

    db.update_error(job_uuid, "xtb crashed")

    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.ERROR)
    assert job["error_message"] == "xtb crashed"


def test_get_status_returns_status_and_created_at(db):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")

    status = db.get_status(job_uuid)

    assert status["status"] == int(JobStatus.PENDING)
    assert status["created_at"] == db.get_job(job_uuid)["created_at"]


def test_get_status_missing_job_returns_none(db):
    assert db.get_status(str(uuid.uuid4())) is None


# ── idempotency / guarded-transition tests ─────────────────────────────────

def test_late_mark_processing_after_done_is_noop(db):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")
    db.update_result(job_uuid, _results(), "n", "c", "a")

    db.mark_processing(job_uuid)

    assert db.get_job(job_uuid)["status"] == int(JobStatus.DONE)


def test_second_update_result_does_not_overwrite(db):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")
    db.update_result(job_uuid, _results(lambda_plus_eV=0.5), "n", "c", "a")

    db.update_result(job_uuid, _results(lambda_plus_eV=9.9), "N", "C", "A")

    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.DONE)
    assert job["lambda_plus_eV"] == 0.5
    assert job["xyz_neutral"] == "n"


def test_update_error_on_done_row_is_noop(db):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")
    db.update_result(job_uuid, _results(), "n", "c", "a")

    db.update_error(job_uuid, "too late")

    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.DONE)
    assert job["error_message"] is None


def test_update_result_on_error_row_is_noop(db):
    job_uuid = str(uuid.uuid4())
    db.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")
    db.update_error(job_uuid, "boom")

    db.update_result(job_uuid, _results(), "n", "c", "a")

    job = db.get_job(job_uuid)
    assert job["status"] == int(JobStatus.ERROR)
    assert job["lambda_plus_eV"] is None


# ── existing behaviour must be preserved ────────────────────────────────────

def test_find_all_by_canonical_only_returns_done_or_seen(db):
    canonical = "c1ccccc1"

    pending_uuid = str(uuid.uuid4())
    db.create_pending_job(pending_uuid, canonical, canonical)

    processing_uuid = str(uuid.uuid4())
    db.create_pending_job(processing_uuid, canonical, canonical)
    db.mark_processing(processing_uuid)

    error_uuid = str(uuid.uuid4())
    db.create_pending_job(error_uuid, canonical, canonical)
    db.update_error(error_uuid, "failed")

    done_uuid = str(uuid.uuid4())
    db.create_pending_job(done_uuid, canonical, canonical)
    db.update_result(done_uuid, _results(), "n", "c", "a")

    seen_uuid = str(uuid.uuid4())
    db.create_pending_job(seen_uuid, canonical, canonical)
    db.update_result(seen_uuid, _results(), "n", "c", "a")
    db.mark_seen(seen_uuid)

    results = db.find_all_by_canonical(canonical)
    returned_uuids = {r["uuid"] for r in results}

    assert returned_uuids == {done_uuid, seen_uuid}
