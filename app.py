"""Flask web frontend for the λ‑xTB reorganization energy calculator.

This service is intentionally tiny: it takes a SMILES string, runs the
four-point reorganization energy calculation (GFN2-xTB via xtb-python), and
renders the results.

Usage:
    export FLASK_APP=app.py
    flask run --host=0.0.0.0

Then visit http://localhost:5000/
"""

import os
import traceback

from flask import Flask, render_template, request, redirect, url_for, flash

from lambda_xtb import calculate_lambda, atoms_to_xyz


app = Flask(__name__)
app.secret_key = "replace-me-with-a-random-secret"  # only needed for flash messages

_BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")


@app.context_processor
def inject_version():
    return {"build_version": _BUILD_VERSION}


@app.route("/", methods=["GET"])
def index():
    """Render the SMILES input form."""
    return render_template("index.html")


@app.route("/calculate", methods=["POST"])
def calculate():
    """Run a calculation and show the results."""
    smiles = request.form.get("smiles", "").strip()

    if not smiles:
        flash("Please provide a SMILES string.")
        return redirect(url_for("index"))

    try:
        results = calculate_lambda(smiles)

        # Prepare serialized geometry for visualization (optional)
        xyz_neutral = atoms_to_xyz(results["geometries"]["neutral"])

        return render_template(
            "result.html",
            smiles=smiles,
            results=results,
            xyz_neutral=xyz_neutral,
        )
    except Exception as exc:
        traceback.print_exc()
        flash(f"Calculation failed: {exc}")
        return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
