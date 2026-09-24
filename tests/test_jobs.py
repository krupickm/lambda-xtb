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


# ── per-instance Job sizing / attribution (WP11) ────────────────────────────
#
# Prod and the parallel test instance run the SAME image, so everything that
# differs between them arrives as env on the frontend Deployment and must be
# honoured here. The defaults above (tested by
# `test_build_job_spec_resources_request_14_cpu`) are prod's values.

INSTANCE_ENV = ENV | {
    "JOB_CPU": "2",
    "JOB_MEMORY_REQUEST": "2Gi",
    "JOB_MEMORY_LIMIT": "4Gi",
    "INSTANCE": "test",
}


def test_build_job_spec_resources_follow_job_env():
    manifest = jobs.build_job_spec(str(uuid.uuid4()), "c1ccccc1", "img", INSTANCE_ENV)
    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert resources["requests"] == {"cpu": "2", "memory": "2Gi"}
    assert resources["limits"] == {"cpu": "2", "memory": "4Gi"}


def test_build_job_spec_resources_fall_back_to_prod_defaults():
    """An instance that sets none of the JOB_* vars must keep prod's size."""
    env = {k: v for k, v in ENV.items() if k != "XTB_NPROC"}
    manifest = jobs.build_job_spec(str(uuid.uuid4()), "c1ccccc1", "img", env)
    resources = manifest["spec"]["template"]["spec"]["containers"][0]["resources"]

    assert resources["requests"] == {"cpu": "14", "memory": "4Gi"}
    assert resources["limits"] == {"cpu": "14", "memory": "16Gi"}


def test_build_job_spec_xtb_nproc_follows_job_cpu():
    """Unset XTB_NPROC must track JOB_CPU — xtb spawning 14 threads inside a
    2-CPU Job would thrash instead of going faster."""
    env = {k: v for k, v in INSTANCE_ENV.items() if k != "XTB_NPROC"}
    manifest = jobs.build_job_spec(str(uuid.uuid4()), "c1ccccc1", "img", env)
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {e["name"]: e["value"] for e in container["env"]}

    assert env_by_name["XTB_NPROC"] == "2"


def test_build_job_spec_xtb_nproc_explicit_wins_over_job_cpu():
    manifest = jobs.build_job_spec(str(uuid.uuid4()), "c1ccccc1", "img", INSTANCE_ENV)
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env_by_name = {e["name"]: e["value"] for e in container["env"]}

    assert env_by_name["XTB_NPROC"] == "14"  # INSTANCE_ENV inherits ENV's value


def test_build_job_spec_instance_label_on_job_and_pod_template():
    manifest = jobs.build_job_spec(str(uuid.uuid4()), "c1ccccc1", "img", INSTANCE_ENV)

    assert manifest["metadata"]["labels"]["instance"] == "test"
    assert manifest["spec"]["template"]["metadata"]["labels"]["instance"] == "test"


def test_build_job_spec_instance_label_defaults_to_prod():
    manifest = jobs.build_job_spec(str(uuid.uuid4()), "c1ccccc1", "img", ENV)

    assert manifest["metadata"]["labels"]["instance"] == "prod"
    assert manifest["spec"]["template"]["metadata"]["labels"]["instance"] == "prod"


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


def test_create_compute_job_passes_instance_env_through(monkeypatch):
    """The JOB_*/INSTANCE vars set on the frontend Deployment must reach the
    spawned Job — this is the whole mechanism the test instance relies on."""
    fake_batch_v1 = MagicMock()
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)
    monkeypatch.setenv("COMPUTE_IMAGE", "img")
    monkeypatch.setenv("NAMESPACE", "krupicka-ns")
    monkeypatch.setenv("CALLBACK_BASE_URL", ENV["CALLBACK_BASE_URL"])
    monkeypatch.setenv("CALLBACK_TOKEN", ENV["CALLBACK_TOKEN"])
    monkeypatch.setenv("JOB_CPU", "2")
    monkeypatch.setenv("JOB_MEMORY_REQUEST", "2Gi")
    monkeypatch.setenv("JOB_MEMORY_LIMIT", "4Gi")
    monkeypatch.setenv("INSTANCE", "test")
    monkeypatch.delenv("XTB_NPROC", raising=False)

    jobs.create_compute_job(str(uuid.uuid4()), "c1ccccc1")

    _, kwargs = fake_batch_v1.create_namespaced_job.call_args
    container = kwargs["body"]["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"] == {"cpu": "2", "memory": "2Gi"}
    assert container["resources"]["limits"] == {"cpu": "2", "memory": "4Gi"}
    assert {e["name"]: e["value"] for e in container["env"]}["XTB_NPROC"] == "2"
    assert kwargs["body"]["metadata"]["labels"]["instance"] == "test"


def test_create_compute_job_unset_instance_env_keeps_prod_size(monkeypatch):
    fake_batch_v1 = MagicMock()
    monkeypatch.setattr(jobs, "_batch_v1_client", lambda: fake_batch_v1)
    monkeypatch.setenv("COMPUTE_IMAGE", "img")
    monkeypatch.setenv("CALLBACK_BASE_URL", ENV["CALLBACK_BASE_URL"])
    monkeypatch.setenv("CALLBACK_TOKEN", ENV["CALLBACK_TOKEN"])
    for var in ("JOB_CPU", "JOB_MEMORY_REQUEST", "JOB_MEMORY_LIMIT", "XTB_NPROC", "INSTANCE"):
        monkeypatch.delenv(var, raising=False)

    jobs.create_compute_job(str(uuid.uuid4()), "c1ccccc1")

    _, kwargs = fake_batch_v1.create_namespaced_job.call_args
    container = kwargs["body"]["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"]["requests"]["cpu"] == "14"
    assert container["resources"]["limits"]["memory"] == "16Gi"
    assert {e["name"]: e["value"] for e in container["env"]}["XTB_NPROC"] == "14"
    assert kwargs["body"]["metadata"]["labels"]["instance"] == "prod"


def test_create_compute_job_no_in_cluster_config_short_circuits():
    """Mirrors SERVICE_SPLIT.md "Verification": outside a cluster (no
    ServiceAccount mounted, e.g. local `flask run`/`docker run`), this must
    not crash even with none of COMPUTE_IMAGE/CALLBACK_* set — it's a no-op
    so a developer can drive the worker by hand instead."""
    job_uuid = str(uuid.uuid4())

    jobs.create_compute_job(job_uuid, "c1ccccc1")  # must not raise


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
