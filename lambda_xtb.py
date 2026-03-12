"""
lambda_xtb.py
=============
Minimal four-point intramolecular reorganization energy calculator.
Uses GFN2-xTB via xtb-python, ASE for geometry optimization, RDKit for
SMILES → 3D starting geometry.

Nelsen four-point recipe
------------------------
Seven calculations total (3 opts + 4 single-points):

    geo0   = optimized neutral     (charge=0,  uhf=0)
    geo+   = optimized cation      (charge=+1, uhf=1)
    geo-   = optimized anion       (charge=-1, uhf=1)

    E0(geo0)   opt result (free)
    E+(geo+)   opt result (free)
    E-(geo-)   opt result (free)
    E+(geo0)   SP: cation   @ neutral geometry
    E-(geo0)   SP: anion    @ neutral geometry
    E0(geo+)   SP: neutral  @ cation geometry
    E0(geo-)   SP: neutral  @ anion geometry

    lambda+ = [E+(geo0) - E+(geo+)] + [E0(geo+) - E0(geo0)]   hole transport
    lambda- = [E-(geo0) - E-(geo-)] + [E0(geo-) - E0(geo0)]   electron transport

All energies in Hartree internally; results reported in eV and meV.

Install
-------
    pip install xtb-python ase rdkit numpy

Usage
-----
    python lambda_xtb.py                        # runs built-in pentacene demo
    python lambda_xtb.py "c1ccc2ccccc2c1"       # naphthalene from SMILES arg
"""

import sys
import numpy as np
import pprint
import json

# ── constants ─────────────────────────────────────────────────────────────────
BOHR_TO_ANG = 0.529177210903
ANG_TO_BOHR = 1.0 / BOHR_TO_ANG
EH_TO_EV    = 27.211386245988   # Hartree → eV


# ── xTB / ASE calculator factory ─────────────────────────────────────────────

def make_xtb_calc(atoms, charge: int, uhf: int):
    """
    Attach a correctly charged GFN2-xTB calculator to atoms in-place.
    Charge and uhf must be set on the Atoms object, NOT just the calculator.
    """
    from xtb.ase.calculator import XTB
    import numpy as np

    n = len(atoms)

    # distribute charge and unpaired electrons uniformly across atoms
    # xTB reads the SUM, so distribution doesn't matter — just needs to sum correctly
    atoms.set_initial_charges(np.full(n, charge / n))
    atoms.set_initial_magnetic_moments(np.full(n, uhf / n))

    atoms.calc = XTB(method="GFN2-xTB")


# ── geometry generation from SMILES ──────────────────────────────────────────

def smiles_to_atoms(smiles: str):
    """
    Convert a SMILES string to an ASE Atoms object with a rough 3D geometry.
    Uses RDKit ETKDG conformer generation.
    Returns ASE Atoms (positions in Angstrom, no periodic boundary conditions).
    """
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from ase import Atoms

    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        raise ValueError(f"RDKit could not parse SMILES: {smiles!r}")

    mol = Chem.AddHs(mol)
    result = AllChem.EmbedMolecule(mol, AllChem.ETKDGv3())
    if result != 0:
        raise RuntimeError("RDKit ETKDG embedding failed — try a different SMILES.")
    AllChem.MMFFOptimizeMolecule(mol)   # quick MMFF pre-optimisation

    conf    = mol.GetConformer()
    symbols = [atom.GetSymbol() for atom in mol.GetAtoms()]
    pos     = conf.GetPositions()       # Angstrom

    return Atoms(symbols=symbols, positions=pos)


# ── single geometry optimization ──────────────────────────────────────────────

def optimize(atoms_in, charge: int, uhf: int,
             fmax: float = 0.05, max_steps: int = 500,
             label: str = "") -> tuple:
    """
    Geometry-optimize a copy of atoms_in at the given charge/uhf state.

    Returns
    -------
    (optimized_atoms, energy_hartree)
        optimized_atoms : ASE Atoms at converged geometry
        energy_hartree  : total GFN2-xTB energy in Hartree
    """
    from ase.optimize import LBFGS
    import copy

    # CORRECT — copy only the geometry, attach a fresh calculator
    atoms = atoms_in.copy()          # ASE Atoms.copy() clones positions/numbers/cell only
    make_xtb_calc(atoms, charge, uhf)

    tag = label or f"charge={charge:+d} uhf={uhf}"
    print(f"  Optimizing  [{tag}] ...", end=" ", flush=True)

    opt = LBFGS(atoms, logfile="-")
    converged = opt.run(fmax=fmax, steps=max_steps)

    if not converged:
        print(f"WARNING: not converged after {max_steps} steps!")
    else:
        print(f"converged ({opt.get_number_of_steps()} steps)")

    # xTB energy is in eV from the ASE calculator interface;
    # convert to Hartree for the four-point arithmetic to stay clean.
    energy_ev = atoms.get_potential_energy()
    energy_eh = energy_ev / EH_TO_EV

    return atoms, energy_eh


# ── single-point energy ───────────────────────────────────────────────────────

def singlepoint(atoms_in, charge: int, uhf: int, label: str = "") -> float:
    """
    Single-point GFN2-xTB energy at the geometry of atoms_in.
    Returns energy in Hartree.
    """
    import copy

    # CORRECT — copy only the geometry, attach a fresh calculator
    atoms = atoms_in.copy()          # ASE Atoms.copy() clones positions/numbers/cell only
    make_xtb_calc(atoms, charge, uhf)

    tag = label or f"charge={charge:+d} uhf={uhf}"
    print(f"  Single-point [{tag}] ...", end=" ", flush=True)

    energy_ev = atoms.get_potential_energy()
    energy_eh = energy_ev / EH_TO_EV
    print(f"{energy_eh:.8f} Eh")

    return energy_eh

# add this function anywhere before calculate_lambda()
def save_results(results: dict, smiles: str, path: str = "lambda_results.json"):
    """Save results dict to JSON, converting non-serializable parts."""
    output = {
        "smiles": smiles,
        "lambda_plus_eV":   results["lambda_plus_eV"],
        "lambda_minus_eV":  results["lambda_minus_eV"],
        "lambda_plus_meV":  results["lambda_plus_meV"],
        "lambda_minus_meV": results["lambda_minus_meV"],
        "partial":          results["partial"],   # already all floats
        "geometries": {
            name: {
                "symbols":   atoms.get_chemical_symbols(),
                "positions": atoms.get_positions().tolist(),   # numpy → list
            }
            for name, atoms in results["geometries"].items()
        }
    }
    with open(path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results saved to {path}")


def atoms_to_xyz(atoms):
    """Return an XYZ-formatted string for an ASE Atoms object."""
    from ase.io import write
    import io

    buf = io.StringIO()
    write(buf, atoms, format="xyz")
    return buf.getvalue()

# ── main four-point calculator ────────────────────────────────────────────────

def calculate_lambda(smiles: str) -> dict:
    """
    Full four-point reorganization energy calculation.

    Parameters
    ----------
    smiles : str
        SMILES string of the neutral molecule.

    Returns
    -------
    dict with keys:
        lambda_plus_eV   : hole reorganization energy (eV)
        lambda_minus_eV  : electron reorganization energy (eV)
        lambda_plus_meV  : same in meV
        lambda_minus_meV : same in meV
        partial          : dict of all eight intermediate energies (Eh)
        geometries       : dict with ASE Atoms for neutral, cation, anion
    """
    print(f"\n{'='*60}")
    print(f"  Molecule : {smiles}")
    print(f"{'='*60}")

    # ── starting geometry ────────────────────────────────────────────
    print("\n[1/3] Generating 3D starting geometry from SMILES ...")
    atoms0_raw = smiles_to_atoms(smiles)
    print(f"      {len(atoms0_raw)} atoms")

    # ── three optimizations ──────────────────────────────────────────
    print("\n[2/3] Geometry optimizations ...")
    geo0,    E0_geo0    = optimize(atoms0_raw, charge= 0, uhf=0, label="neutral")
    geo_plus, E_plus_geoplus = optimize(atoms0_raw, charge=+1, uhf=1, label="cation ")
    geo_minus,E_minus_geominus = optimize(atoms0_raw, charge=-1, uhf=1, label="anion  ")

    # ── four single-points ───────────────────────────────────────────
    print("\n[3/3] Cross single-points ...")
    E_plus_geo0    = singlepoint(geo0,     charge=+1, uhf=1, label="cation  @ neutral geo")
    E_minus_geo0   = singlepoint(geo0,     charge=-1, uhf=1, label="anion   @ neutral geo")
    E0_geoplus     = singlepoint(geo_plus, charge= 0, uhf=0, label="neutral @ cation  geo")
    E0_geominus    = singlepoint(geo_minus,charge= 0, uhf=0, label="neutral @ anion   geo")

    # ── four-point formula (all in Hartree) ──────────────────────────
    lam1_plus  = E_plus_geo0   - E_plus_geoplus     # λ₁⁺
    lam2_plus  = E0_geoplus    - E0_geo0             # λ₂⁺
    lam_plus   = lam1_plus + lam2_plus

    lam1_minus = E_minus_geo0  - E_minus_geominus    # λ₁⁻
    lam2_minus = E0_geominus   - E0_geo0             # λ₂⁻
    lam_minus  = lam1_minus + lam2_minus

    lam_plus_ev  = lam_plus  * EH_TO_EV
    lam_minus_ev = lam_minus * EH_TO_EV

    # ── report ───────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  Results")
    print(f"{'='*60}")
    print(f"  λ⁺  (hole)     = {lam_plus_ev*1000:8.1f} meV  ({lam_plus_ev:.4f} eV)")
    print(f"    λ₁⁺           = {lam1_plus*EH_TO_EV*1000:8.1f} meV  (cation relaxation)")
    print(f"    λ₂⁺           = {lam2_plus*EH_TO_EV*1000:8.1f} meV  (neutral relaxation from cation geo)")
    print(f"  λ⁻  (electron) = {lam_minus_ev*1000:8.1f} meV  ({lam_minus_ev:.4f} eV)")
    print(f"    λ₁⁻           = {lam1_minus*EH_TO_EV*1000:8.1f} meV  (anion relaxation)")
    print(f"    λ₂⁻           = {lam2_minus*EH_TO_EV*1000:8.1f} meV  (neutral relaxation from anion geo)")

    # sanity check: both partials should be positive
    for name, val in [("λ₁⁺", lam1_plus), ("λ₂⁺", lam2_plus),
                      ("λ₁⁻", lam1_minus), ("λ₂⁻", lam2_minus)]:
        if val < 0:
            print(f"  WARNING: {name} is negative ({val*EH_TO_EV*1000:.1f} meV) "
                  f"— possible geometry or convergence issue!")

    return {
        "lambda_plus_eV":   lam_plus_ev,
        "lambda_minus_eV":  lam_minus_ev,
        "lambda_plus_meV":  lam_plus_ev  * 1000,
        "lambda_minus_meV": lam_minus_ev * 1000,
        "partial": {
            "E0_geo0":          E0_geo0,
            "E_plus_geoplus":   E_plus_geoplus,
            "E_minus_geominus": E_minus_geominus,
            "E_plus_geo0":      E_plus_geo0,
            "E_minus_geo0":     E_minus_geo0,
            "E0_geoplus":       E0_geoplus,
            "E0_geominus":      E0_geominus,
            "lam1_plus_eV":     lam1_plus  * EH_TO_EV,
            "lam2_plus_eV":     lam2_plus  * EH_TO_EV,
            "lam1_minus_eV":    lam1_minus * EH_TO_EV,
            "lam2_minus_eV":    lam2_minus * EH_TO_EV,
        },
        "geometries": {
            "neutral": geo0,
            "cation":  geo_plus,
            "anion":   geo_minus,
        }
    }



# ── CLI entry point ───────────────────────────────────────────────────────────

DEMO_MOLECULES = {
    "pentacene":   "c1ccc2cc3cc4cc5ccccc5cc4cc3cc2c1",
    "naphthalene": "c1ccc2ccccc2c1",
    "anthracene":  "c1ccc2cc3ccccc3cc2c1",
    "TPD":         "CN(c1ccc(-c2ccc(N(C)c3ccccc3)cc2)cc1)c1ccccc1",
}

if __name__ == "__main__":
    if len(sys.argv) > 1:
        smiles = sys.argv[1]
    else:
        # default demo: naphthalene (fast, ~10s total)
        smiles = DEMO_MOLECULES["naphthalene"]
        print(f"No SMILES given — running demo: naphthalene ({smiles})")

    results = calculate_lambda(smiles)
    save_results(results, smiles)

    pprint.pp(results)
