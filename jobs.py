"""jobs.py — k8s Job spawning + silent-death reconciliation for compute runs.

The frontend never talks to the worker directly: it spawns one 14-CPU
Kubernetes Job per calculation (`create_compute_job`) and, since the worker
is fire-and-forget, can only detect a worker that dies *silently* (OOMKilled,
evicted, node failure) by polling the Job's own k8s status (`reconcile`).
See SERVICE_SPLIT.md ("API Contract & Job States" / "Silent-death detection")
for the authoritative contract this module implements.

Two state machines, kept separate here too:
  - application state (the SQLite row, via db.py) — the source of truth.
  - k8s Job/Pod state — infra-level, consulted only to infer a dead worker.
"""

import os
from datetime import datetime, timezone

import db as _db
from db import JobStatus

# ── manifest defaults ───────────────────────────────────────────────────────

DEFAULT_BACKOFF_LIMIT = 1
DEFAULT_TTL_SECONDS_AFTER_FINISHED = 3600
DEFAULT_CPU = "14"
DEFAULT_MEMORY_REQUEST = "4Gi"
DEFAULT_MEMORY_LIMIT = "16Gi"

# Belt-and-suspenders timeout for a wedged/garbage-collected job (see
# SERVICE_SPLIT.md "Silent-death detection"): 15 minutes, overridable via env.
DEFAULT_TIMEOUT_SECONDS = 15 * 60

# Statuses for which reconciliation is a no-op (the row is already final).
_TERMINAL_STATUSES = {int(JobStatus.DONE), int(JobStatus.ERROR), int(JobStatus.SEEN)}

_ERROR_DEAD_WORKER = "compute pod terminated unexpectedly"
_ERROR_TIMEOUT = "compute job timed out"


def job_name(job_uuid: str) -> str:
    """Return the k8s Job name for a given job uuid (DNS-1123 label safe)."""
    return f"lambda-xtb-compute-{job_uuid}"


# ── manifest construction (pure) ────────────────────────────────────────────

def build_job_spec(job_uuid: str, smiles: str, image: str, env: dict[str, str]) -> dict:
    """Build the k8s Job manifest for one compute run. Pure function — no I/O.

    `env` must supply `CALLBACK_BASE_URL`, `CALLBACK_TOKEN`, and `XTB_NPROC`;
    `JOB_UUID`/`SMILES` are derived from `job_uuid`/`smiles` and injected
    automatically. Mirrors the restricted securityContext used by the
    frontend Deployment (k8s/deployment.yaml) so CERIT-SC's restricted Pod
    Security Standard admits the pod.
    """
    labels = {"app": "lambda-xtb-compute", "job-uuid": job_uuid}

    container_env = [
        {"name": "JOB_UUID", "value": job_uuid},
        {"name": "SMILES", "value": smiles},
        {"name": "CALLBACK_BASE_URL", "value": env["CALLBACK_BASE_URL"]},
        {"name": "CALLBACK_TOKEN", "value": env["CALLBACK_TOKEN"]},
        {"name": "XTB_NPROC", "value": str(env["XTB_NPROC"])},
    ]

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": job_name(job_uuid),
            "labels": labels,
        },
        "spec": {
            "backoffLimit": DEFAULT_BACKOFF_LIMIT,
            "ttlSecondsAfterFinished": DEFAULT_TTL_SECONDS_AFTER_FINISHED,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "compute",
                            "image": image,
                            "command": ["python", "compute_runner.py"],
                            "securityContext": {
                                "runAsUser": 1000,
                                "allowPrivilegeEscalation": False,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "env": container_env,
                            "resources": {
                                "requests": {
                                    "cpu": DEFAULT_CPU,
                                    "memory": DEFAULT_MEMORY_REQUEST,
                                },
                                "limits": {
                                    "cpu": DEFAULT_CPU,
                                    "memory": DEFAULT_MEMORY_LIMIT,
                                },
                            },
                            "volumeMounts": [
                                {"name": "scratch", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [
                        {"name": "scratch", "emptyDir": {}},
                    ],
                },
            },
        },
    }


# ── k8s client access (the only I/O seam; monkeypatched in tests) ──────────

def _batch_v1_client():
    """Return a `kubernetes.client.BatchV1Api` using in-cluster config.

    Isolated in its own function so tests can monkeypatch it and never touch
    a real cluster or the mounted ServiceAccount.
    """
    from kubernetes import client, config

    config.load_incluster_config()
    return client.BatchV1Api()


# ── job submission ──────────────────────────────────────────────────────────

def create_compute_job(job_uuid: str, smiles: str) -> None:
    """Submit one compute Job via the in-cluster kubernetes client.

    Reads `COMPUTE_IMAGE`, `NAMESPACE`, `CALLBACK_BASE_URL`, `CALLBACK_TOKEN`,
    and `XTB_NPROC` (default `"14"`) from the environment.
    """
    image = os.environ["COMPUTE_IMAGE"]
    namespace = os.environ.get("NAMESPACE", "default")
    env = {
        "CALLBACK_BASE_URL": os.environ["CALLBACK_BASE_URL"],
        "CALLBACK_TOKEN": os.environ["CALLBACK_TOKEN"],
        "XTB_NPROC": os.environ.get("XTB_NPROC", DEFAULT_CPU),
    }

    manifest = build_job_spec(job_uuid, smiles, image, env)
    _batch_v1_client().create_namespaced_job(namespace=namespace, body=manifest)


# ── silent-death reconciliation ─────────────────────────────────────────────

def _read_job(batch_v1, name: str, namespace: str):
    """Return the Job object, or None if it no longer exists (404)."""
    from kubernetes.client.exceptions import ApiException

    try:
        return batch_v1.read_namespaced_job(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _job_failed(job, backoff_limit: int) -> bool:
    """Return True iff the Job's own status shows it exhausted its retries."""
    status = getattr(job, "status", None)
    failed = getattr(status, "failed", None) if status is not None else None
    return failed is not None and failed > backoff_limit


def reconcile(
    job_uuid: str,
    database: "_db.Database | None" = None,
    timeout_seconds: int | None = None,
) -> None:
    """Detect a silently-dead worker for `job_uuid` and mark the row ERROR.

    Consults two channels for a **non-terminal** row only (PENDING/PROCESSING):
      - the k8s Job status: `.status.failed > backoffLimit`, or the Job object
        missing entirely -> ERROR ("compute pod terminated unexpectedly").
      - `created_at` age vs. a timeout cutoff -> ERROR ("compute job timed
        out"), belt-and-suspenders for a TTL-collected or wedged Job.
    A terminal row, or a running-and-young Job, is left unchanged.
    """
    database = database if database is not None else _db.get_db()
    cutoff = timeout_seconds if timeout_seconds is not None else int(
        os.environ.get("JOB_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    )

    status_row = database.get_status(job_uuid)
    if status_row is None:
        return
    if int(status_row["status"]) in _TERMINAL_STATUSES:
        return

    if _dead_worker_detected(job_uuid):
        database.update_error(job_uuid, _ERROR_DEAD_WORKER)
        return

    created_at = datetime.fromisoformat(status_row["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()

    if age_seconds > cutoff:
        database.update_error(job_uuid, _ERROR_TIMEOUT)


def _dead_worker_detected(job_uuid: str) -> bool:
    """Return True iff the k8s Job is missing or has exhausted its retries.

    If the k8s API can't be consulted at all (no in-cluster config outside a
    real cluster, a transient connection error, ...) this returns False
    rather than raising: we simply can't tell from this channel, so
    `reconcile` falls back to the age-based timeout check instead of
    false-positiving a live job as dead on mere connectivity trouble.
    """
    name = job_name(job_uuid)
    namespace = os.environ.get("NAMESPACE", "default")
    try:
        job = _read_job(_batch_v1_client(), name, namespace)
    except Exception:
        return False
    return job is None or _job_failed(job, DEFAULT_BACKOFF_LIMIT)
