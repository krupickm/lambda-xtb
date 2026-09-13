"""Flask web frontend for the λ‑xTB reorganization energy calculator.

Usage:
    export FLASK_APP=app.py
    flask run --host=0.0.0.0

Then visit http://localhost:5000/
"""

import hmac
import json
import os
import statistics
import traceback
import uuid

from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from rdkit import Chem

import db as _db
import jobs as _jobs
from db import JobStatus
from lambda_xtb import calculate_lambda, atoms_to_xyz


app = Flask(__name__)
app.secret_key = "replace-me-with-a-random-secret"  # only needed for flash messages

_BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")

_database = _db.get_db()
_database.init_db()

# Statuses for which the worker-facing API refuses further transitions.
_TERMINAL_STATUSES = {int(JobStatus.DONE), int(JobStatus.ERROR), int(JobStatus.SEEN)}


def _reconcile_job(job_uuid: str) -> None:
    """Dead-worker reconciliation hook.

    Called by `GET /api/jobs/<uuid>/status` for non-terminal jobs so a
    silently-dead worker (OOMKilled, evicted, ...) can be detected and the
    row marked ERROR — see SERVICE_SPLIT.md, "Silent-death detection".
    Delegates to `jobs.reconcile` (WP6), which consults the k8s Job status
    and the row's age. Kept as a module-level function (rather than inlined
    in the endpoint) so tests can monkeypatch `app._reconcile_job`.
    """
    _jobs.reconcile(job_uuid, database=_database)


@app.context_processor
def inject_version():
    return {"build_version": _BUILD_VERSION}


def _canonical_smiles(smiles: str) -> str | None:
    """Return RDKit canonical SMILES, or None if the input cannot be parsed."""
    mol = Chem.MolFromSmiles(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else None


def _require_token() -> bool:
    """Check `Authorization: Bearer <CALLBACK_TOKEN>` on a worker callback.

    Returns True only if CALLBACK_TOKEN is configured (non-empty) and the
    request's bearer token matches it via a constant-time comparison.
    """
    expected = os.environ.get("CALLBACK_TOKEN", "")
    if not expected:
        return False

    auth = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return False

    provided = auth[len(prefix):]
    return hmac.compare_digest(provided, expected)


# ── worker-facing internal API (Bearer-token auth) ─────────────────────────

@app.route("/api/jobs/<job_uuid>/start", methods=["POST"])
def api_job_start(job_uuid):
    """PENDING -> PROCESSING. 200 {ok}; 401 if unauthenticated; 409 if terminal."""
    if not _require_token():
        return jsonify({"error": "unauthorized"}), 401

    status = _database.get_status(job_uuid)
    if status is None:
        return jsonify({"error": "not found"}), 404
    if int(status["status"]) in _TERMINAL_STATUSES:
        return jsonify({"error": "job already terminal"}), 409

    _database.mark_processing(job_uuid)
    return jsonify({"ok": True}), 200


@app.route("/api/jobs/<job_uuid>/result", methods=["POST"])
def api_job_result(job_uuid):
    """PENDING/PROCESSING -> DONE, storing results + geometries. 200 always
    (idempotent/guarded at the DB layer; a terminal row is left untouched)."""
    if not _require_token():
        return jsonify({"error": "unauthorized"}), 401

    status = _database.get_status(job_uuid)
    if status is None:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    results = {
        "lambda_plus_eV": payload.get("lambda_plus_eV"),
        "lambda_minus_eV": payload.get("lambda_minus_eV"),
        "partial": payload.get("partial", {}),
    }
    _database.update_result(
        job_uuid,
        results,
        xyz_neutral=payload.get("xyz_neutral", ""),
        xyz_cation=payload.get("xyz_cation", ""),
        xyz_anion=payload.get("xyz_anion", ""),
    )
    return jsonify({"ok": True}), 200


@app.route("/api/jobs/<job_uuid>/error", methods=["POST"])
def api_job_error(job_uuid):
    """PENDING/PROCESSING -> ERROR, storing the error message. 200 always
    (idempotent/guarded at the DB layer; a terminal row is left untouched)."""
    if not _require_token():
        return jsonify({"error": "unauthorized"}), 401

    status = _database.get_status(job_uuid)
    if status is None:
        return jsonify({"error": "not found"}), 404

    payload = request.get_json(silent=True) or {}
    _database.update_error(job_uuid, payload.get("message", ""))
    return jsonify({"ok": True}), 200


# ── poll-facing internal API (uuid is the capability; no auth) ─────────────

@app.route("/api/jobs/<job_uuid>/status", methods=["GET"])
def api_job_status(job_uuid):
    """Return `{status, redirect, error}` for the polling page.

    For a non-terminal job, runs the (injectable) dead-worker reconciliation
    hook first so a silently-dead worker is surfaced as ERROR on this poll.
    """
    status_row = _database.get_status(job_uuid)
    if status_row is None:
        return jsonify({"error": "not found"}), 404

    if int(status_row["status"]) not in _TERMINAL_STATUSES:
        _reconcile_job(job_uuid)
        status_row = _database.get_status(job_uuid)

    status_value = int(status_row["status"])
    status_name = JobStatus(status_value).name

    redirect_to = None
    error_message = None
    if status_value in (int(JobStatus.DONE), int(JobStatus.SEEN)):
        redirect_to = url_for("stats", job_uuid=job_uuid)
    elif status_value == int(JobStatus.ERROR):
        job = _database.get_job(job_uuid)
        error_message = job["error_message"] if job else None

    return jsonify({"status": status_name, "redirect": redirect_to, "error": error_message}), 200


@app.route("/", methods=["GET"])
def index():
    """Render the SMILES input form."""
    return render_template("index.html")


@app.route("/calculate", methods=["POST"])
def calculate():
    """Run a new xTB calculation and redirect to the statistics page."""
    smiles = request.form.get("smiles", "").strip()

    if not smiles:
        flash("Please provide a SMILES string.")
        return redirect(url_for("index"))

    canonical = _canonical_smiles(smiles)
    if canonical is None:
        flash("Invalid SMILES — could not parse the structure.")
        return redirect(url_for("index"))

    job_uuid = str(uuid.uuid4())
    try:
        results = calculate_lambda(smiles)
        _database.store_job(
            job_uuid, smiles, canonical, results,
            xyz_neutral=atoms_to_xyz(results["geometries"]["neutral"]),
            xyz_cation=atoms_to_xyz(results["geometries"]["cation"]),
            xyz_anion=atoms_to_xyz(results["geometries"]["anion"]),
        )
    except Exception as exc:
        traceback.print_exc()
        _database.store_error(job_uuid, smiles, canonical, str(exc))
        flash(f"Calculation failed: {exc}")
        return redirect(url_for("index"))

    return redirect(url_for("stats", job_uuid=job_uuid))


@app.route("/stats/<job_uuid>")
def stats(job_uuid):
    """Show all runs for the same canonical SMILES, with summary statistics."""
    job = _database.get_job(job_uuid)
    if job is None:
        flash("Result not found.")
        return redirect(url_for("index"))

    all_jobs = _database.find_all_by_canonical(job["smiles_canonical"])

    summary = None
    if len(all_jobs) > 1:
        lp = [j["lambda_plus_eV"]  * 1000 for j in all_jobs]
        lm = [j["lambda_minus_eV"] * 1000 for j in all_jobs]
        summary = {
            "n":               len(all_jobs),
            "lp_mean":         statistics.mean(lp),
            "lp_stdev":        statistics.stdev(lp),
            "lm_mean":         statistics.mean(lm),
            "lm_stdev":        statistics.stdev(lm),
        }

    return render_template(
        "stats.html",
        canonical=job["smiles_canonical"],
        current_uuid=job_uuid,
        jobs=all_jobs,
        summary=summary,
    )


@app.route("/result/<job_uuid>")
def result(job_uuid):
    """Load a result from the database and render it."""
    job = _database.get_job(job_uuid)
    if job is None:
        flash("Result not found.")
        return redirect(url_for("index"))

    if job["status"] == int(_db.JobStatus.ERROR):
        flash(f"Calculation failed: {job['error_message']}")
        return redirect(url_for("index"))

    partial = json.loads(job["partial_json"]) if job["partial_json"] else {}
    from_cache = request.args.get("from_cache", False)

    _database.mark_seen(job_uuid)

    return render_template(
        "result.html",
        job=job,
        partial=partial,
        from_cache=from_cache,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
