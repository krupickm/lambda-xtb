"""Acceptance tests for WP6 — k8s Job spawning + silent-death reconciliation.

All infrastructure is mocked: `build_job_spec` is exercised as a pure
function (no client at all), and `reconcile` is exercised against a fake
`kubernetes.client.BatchV1Api` (monkeypatched via `jobs._batch_v1_client`)
with fabricated Job statuses/timestamps. No real cluster or network is ever
touched.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

import jobs
from db import JobStatus, SQLiteDatabase

ENV = {
    "CALLBACK_BASE_URL": "http://lambda-xtb-svc.krupicka-ns.svc.cluster.local",
    "CALLBACK_TOKEN": "secret-token",
    "XTB_NPROC": "14",
}


# ── build_job_spec (pure function) ──────────────────────────────────────────

def test_build_job_spec_basic_shape():
    job_uuid = str(uuid.uuid4())
    manifest = jobs.build_job_spec(job_uuid, "c1ccccc1", "cerit.io/krupickm/lambda-xtb:abc123", ENV)

    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["metadata"]["name"] == f"lambda-xtb-compute-{job_uuid}"

    spec = manifest["spec"]
    assert spec["backoffLimit"] == 1
    assert spec["ttlSecondsAfterFinished"] == 3600

    pod_spec = spec["template"]["spec"]
    assert pod_spec["restartPolicy"] == "Never"

    container = pod_spec["containers"][0]
    assert container["image"] == "cerit.io/krupickm/lambda-xtb:abc123"
    assert container["command"] == ["python", "compute_runner.py"]


def test_build_job_spec_resources_request_14_cpu():
    manifest = jobs.build_job_spec(str(uuid.uuid4()), "c1ccccc1", "img", ENV)
    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert resources["requests"]["cpu"] == "14"
    assert resources["limits"]["cpu"] == "14"


def test_build_job_spec_security_context_restricted():
    manifest = jobs.build_job_spec(str(uuid.uuid4()), "c1ccccc1", "img", ENV)
    pod_spec = manifest["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert pod_spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"

    assert container["securityContext"]["runAsUser"] == 1000
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_build_job_spec_env_vars():
    job_uuid = str(uuid.uuid4())
    manifest = jobs.build_job_spec(job_uuid, "CCO", "img", ENV)
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {e["name"]: e["value"] for e in container["env"]}

    assert env_by_name["JOB_UUID"] == job_uuid
    assert env_by_name["SMILES"] == "CCO"
    assert env_by_name["CALLBACK_BASE_URL"] == ENV["CALLBACK_BASE_URL"]
    assert env_by_name["CALLBACK_TOKEN"] == ENV["CALLBACK_TOKEN"]
    assert env_by_name["XTB_NPROC"] == "14"


def test_build_job_spec_empty_dir_mounted_at_tmp():
    manifest = jobs.build_job_spec(str(uuid.uuid4()), "c1ccccc1", "img", ENV)
    pod_spec = manifest["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]

    mounts = container["volumeMounts"]
    assert any(m["mountPath"] == "/tmp" for m in mounts)

    scratch_name = next(m["name"] for m in mounts if m["mountPath"] == "/tmp")
    volume = next(v for v in pod_spec["volumes"] if v["name"] == scratch_name)
    assert volume["emptyDir"] == {}


# ── create_compute_job ───────────────────────────────────────────────────────

def test_create_compute_job_submits_via_batch_v1(monkeypatch):
    fake_batch_v1 = MagicMock()
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)
    monkeypatch.setenv("COMPUTE_IMAGE", "cerit.io/krupickm/lambda-xtb:abc123")
    monkeypatch.setenv("NAMESPACE", "krupicka-ns")
    monkeypatch.setenv("CALLBACK_BASE_URL", ENV["CALLBACK_BASE_URL"])
    monkeypatch.setenv("CALLBACK_TOKEN", ENV["CALLBACK_TOKEN"])

    job_uuid = str(uuid.uuid4())
    jobs.create_compute_job(job_uuid, "c1ccccc1")

    fake_batch_v1.create_namespaced_job.assert_called_once()
    _, kwargs = fake_batch_v1.create_namespaced_job.call_args
    assert kwargs["namespace"] == "krupicka-ns"
    assert kwargs["body"]["metadata"]["name"] == jobs.job_name(job_uuid)


# ── reconcile ─────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    database = SQLiteDatabase(str(tmp_path / "test.db"))
    database.init_db()
    return database


def _pending_job(database, created_at=None):
    job_uuid = str(uuid.uuid4())
    database.create_pending_job(job_uuid, "c1ccccc1", "c1ccccc1")
    if created_at is not None:
        conn = database._connect()
        conn.execute(
            "UPDATE jobs SET created_at = ? WHERE uuid = ?",
            (created_at.isoformat(), job_uuid),
        )
        conn.commit()
        conn.close()
    return job_uuid


def _fake_job(failed=None):
    return SimpleNamespace(status=SimpleNamespace(failed=failed))


def test_reconcile_failed_job_marks_error(monkeypatch, db):
    job_uuid = _pending_job(db)
    fake_batch_v1 = MagicMock()
    fake_batch_v1.read_namespaced_job.return_value = _fake_job(failed=2)  # > backoffLimit=1
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)

    jobs.reconcile(job_uuid, database=db)

    status = db.get_status(job_uuid)
    assert status["status"] == int(JobStatus.ERROR)
    assert db.get_job(job_uuid)["error_message"] == jobs._ERROR_DEAD_WORKER


def test_reconcile_running_and_young_job_unchanged(monkeypatch, db):
    job_uuid = _pending_job(db)
    fake_batch_v1 = MagicMock()
    fake_batch_v1.read_namespaced_job.return_value = _fake_job(failed=None)
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)

    jobs.reconcile(job_uuid, database=db)

    assert db.get_status(job_uuid)["status"] == int(JobStatus.PENDING)


def test_reconcile_missing_job_marks_error(monkeypatch, db):
    job_uuid = _pending_job(db)
    fake_batch_v1 = MagicMock()
    fake_batch_v1.read_namespaced_job.side_effect = ApiException(status=404)
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)

    jobs.reconcile(job_uuid, database=db)

    status = db.get_status(job_uuid)
    assert status["status"] == int(JobStatus.ERROR)
    assert db.get_job(job_uuid)["error_message"] == jobs._ERROR_DEAD_WORKER


def test_reconcile_k8s_unreachable_falls_back_to_age_check(monkeypatch, db):
    """A non-404 API error (or no in-cluster config, e.g. local dev/tests) must
    not crash the poll endpoint or false-positive a live job as dead — it
    just can't confirm via k8s, so a young job is left unchanged."""
    job_uuid = _pending_job(db)
    fake_batch_v1 = MagicMock()
    fake_batch_v1.read_namespaced_job.side_effect = ApiException(status=500)
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)

    jobs.reconcile(job_uuid, database=db)

    assert db.get_status(job_uuid)["status"] == int(JobStatus.PENDING)


def test_reconcile_no_in_cluster_config_does_not_crash(db):
    """Mirrors what actually happens in local dev/tests: no ServiceAccount
    mounted, so the real `_batch_v1_client()` itself raises a
    ConfigException. Must not propagate out of `reconcile`."""
    job_uuid = _pending_job(db)

    jobs.reconcile(job_uuid, database=db)  # must not raise

    assert db.get_status(job_uuid)["status"] == int(JobStatus.PENDING)


def test_reconcile_age_over_cutoff_marks_error(monkeypatch, db):
    old_created_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    job_uuid = _pending_job(db, created_at=old_created_at)
    fake_batch_v1 = MagicMock()
    fake_batch_v1.read_namespaced_job.return_value = _fake_job(failed=None)
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)

    jobs.reconcile(job_uuid, database=db, timeout_seconds=15 * 60)

    status = db.get_status(job_uuid)
    assert status["status"] == int(JobStatus.ERROR)
    assert db.get_job(job_uuid)["error_message"] == jobs._ERROR_TIMEOUT


def test_reconcile_age_under_cutoff_unchanged(monkeypatch, db):
    recent_created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    job_uuid = _pending_job(db, created_at=recent_created_at)
    fake_batch_v1 = MagicMock()
    fake_batch_v1.read_namespaced_job.return_value = _fake_job(failed=None)
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)

    jobs.reconcile(job_uuid, database=db, timeout_seconds=15 * 60)

    assert db.get_status(job_uuid)["status"] == int(JobStatus.PENDING)


def test_reconcile_processing_job_also_checked(monkeypatch, db):
    """PROCESSING (not just PENDING) is a non-terminal state that gets reconciled."""
    job_uuid = _pending_job(db)
    db.mark_processing(job_uuid)
    fake_batch_v1 = MagicMock()
    fake_batch_v1.read_namespaced_job.side_effect = ApiException(status=404)
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)

    jobs.reconcile(job_uuid, database=db)

    assert db.get_status(job_uuid)["status"] == int(JobStatus.ERROR)


@pytest.mark.parametrize("terminal_setup", ["done", "error"])
def test_reconcile_terminal_job_never_calls_k8s(monkeypatch, db, terminal_setup):
    job_uuid = _pending_job(db)
    if terminal_setup == "done":
        db.update_result(
            job_uuid,
            {"lambda_plus_eV": 0.1, "lambda_minus_eV": 0.2, "partial": {}},
            "n", "c", "a",
        )
    else:
        db.update_error(job_uuid, "already failed")

    fake_batch_v1 = MagicMock()
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)

    jobs.reconcile(job_uuid, database=db)

    fake_batch_v1.read_namespaced_job.assert_not_called()


def test_reconcile_unknown_job_is_noop(monkeypatch, db):
    fake_batch_v1 = MagicMock()
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)

    jobs.reconcile(str(uuid.uuid4()), database=db)

    fake_batch_v1.read_namespaced_job.assert_not_called()
