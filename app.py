"""Flask web frontend for the λ‑xTB reorganization energy calculator.

Usage:
    export FLASK_APP=app.py
    flask run --host=0.0.0.0

Then visit http://localhost:5000/
"""

import json
import os
import traceback
import uuid

from flask import Flask, render_template, request, redirect, url_for, flash
from rdkit import Chem

import db as _db
from lambda_xtb import calculate_lambda, atoms_to_xyz


app = Flask(__name__)
app.secret_key = "replace-me-with-a-random-secret"  # only needed for flash messages

_BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")

_database = _db.get_db()
_database.init_db()


@app.context_processor
def inject_version():
    return {"build_version": _BUILD_VERSION}


def _canonical_smiles(smiles: str) -> str | None:
    """Return RDKit canonical SMILES, or None if the input cannot be parsed."""
    mol = Chem.MolFromSmiles(smiles)
    return Chem.MolToSmiles(mol) if mol is not None else None


@app.route("/", methods=["GET"])
def index():
    """Render the SMILES input form."""
    return render_template("index.html")


@app.route("/calculate", methods=["POST"])
def calculate():
    """Canonicalize SMILES, serve from cache if available, else run xTB."""
    smiles = request.form.get("smiles", "").strip()
    force_recalc = request.form.get("force_recalc") == "1"

    if not smiles:
        flash("Please provide a SMILES string.")
        return redirect(url_for("index"))

    canonical = _canonical_smiles(smiles)
    if canonical is None:
        flash("Invalid SMILES — could not parse the structure.")
        return redirect(url_for("index"))

    # Cache hit — skip the calculation entirely (unless force_recalc is set)
    if not force_recalc:
        cached = _database.find_by_canonical(canonical)
        if cached:
            return redirect(url_for("result", job_uuid=cached["uuid"], from_cache=1))

    # Cache miss — run the calculation
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

    return redirect(url_for("result", job_uuid=job_uuid))


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
