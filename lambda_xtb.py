"""
lambda_xtb.py
=============
Minimal four-point intramolecular reorganization energy calculator.
Uses GFN2-xTB via easyxtb (native ANCopt), RDKit for SMILES → 3D starting geometry.

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
    conda install -c conda-forge xtb rdkit ase
    pip install easyxtb

Usage
-----
    python lambda_xtb.py                        # runs built-in naphthalene demo
    python lambda_xtb.py "c1ccc2ccccc2c1"       # naphthalene from SMILES arg
"""

import os
import shutil
import sys
import tempfile
import numpy as np
import pprint
import json
from concurrent.futures import ThreadPoolExecutor
import easyxtb
from easyxtb.calc import XTB as _XTB_PROGRAM

def _xtb_nproc_from_env() -> int:
    """
    CREST conformer-search parallelism budget for this process, read from
    the XTB_NPROC env var (default 1, matching today's local/dev behaviour).
    A dedicated 14-CPU compute Job sets this higher.

    CREST is the only stage here that benefits from more CPUs: it runs many
    independent, single-threaded conformer searches concurrently. A single
    xtb ANCopt/single-point call does NOT benefit from OpenMP/-P threading on
    molecules this small — synchronization overhead outweighs the gain — so
    every individual xtb worker (opt, SP, and each CREST conformer) always
    runs on 1 CPU regardless of this budget; see the module-level config
    below and `get_lowest_conformer`.
    """
    raw = os.environ.get("XTB_NPROC", "1")
    try:
        n = int(raw)
    except ValueError:
        n = 1
    return n if n > 0 else 1


#: CREST conformer-search parallelism budget (see `_xtb_nproc_from_env`).
XTB_NPROC = _xtb_nproc_from_env()

#: Number of concurrent tasks in calculate_lambda()'s opt / single-point
#: phases. Pool-level (process) parallelism across independent, single-CPU
#: xtb workers is fine and unaffected by XTB_NPROC; kept fixed per
#: SERVICE_SPLIT.md.
OPT_WORKERS = 3
SP_WORKERS = 4

# easyxtb auto-detects n_proc from os.cpu_count() // 1.3 at import time.
# On a k8s node this can be 70-100 CPUs, causing xtb to receive -P 98 and
# hang or thrash — and even at modest values, OpenMP threading inside a
# single xtb call on molecules this small tends to lose time to
# synchronization overhead rather than gain from it. Pin every individual
# xtb call to 1 CPU always; only CREST's own internal parallelism (how many
# single-threaded conformer searches it runs concurrently) scales with
# XTB_NPROC.
easyxtb.configuration.config["n_proc"] = 1
os.environ["OMP_NUM_THREADS"] = "1"

# ── constants ─────────────────────────────────────────────────────────────────
BOHR_TO_ANG = 0.529177210903
EH_TO_EV    = 27.211386245988   # Hartree → eV


# ── ASE ↔ easyxtb geometry converters ────────────────────────────────────────

def ase_to_easyxtb(atoms, charge: int, uhf: int):
    """Convert ASE Atoms to easyxtb Geometry with charge and spin."""
    from easyxtb import Geometry as XGeometry
    from easyxtb.geometry import Atom as XAtom

    return XGeometry(
        [XAtom(sym, *pos) for sym, pos in zip(atoms.get_chemical_symbols(), atoms.get_positions())],
        charge=charge, spin=uhf
    )


def easyxtb_to_ase(geom):
    """Convert easyxtb Geometry to ASE Atoms."""
    from ase import Atoms

    return Atoms(
        symbols=[a.element for a in geom.atoms],
        positions=[[a.x, a.y, a.z] for a in geom.atoms]
    )


# ── geometry generation from SMILES ──────────────────────────────────────────

def smiles_to_atoms(smiles: str):
    """
    Convert a SMILES string to an ASE Atoms object with a rough 3D geometry.
    Uses RDKit ETKDG conformer generation + MMFF pre-optimisation.
    Returns (ASE Atoms, RDKit mol-with-Hs) so callers can inspect the molecule.
    Positions in Angstrom, no periodic boundary conditions.
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

    return Atoms(symbols=symbols, positions=pos), mol


def is_flexible(mol) -> bool:
    """True if molecule has rotatable bonds — CREST conformer screening is worthwhile."""
    from rdkit.Chem import rdMolDescriptors
    return rdMolDescriptors.CalcNumRotatableBonds(mol) > 0


# ── GFN-FF pre-optimisation (neutral only, before GFN2 production runs) ──────

def preopt_gfnff(atoms_in, label: str = ""):
    """
    Quick geometry pre-optimisation using GFN-FF force field (neutral, charge=0, uhf=0).
    Used to relax the starting geometry before CREST and the full GFN2-xTB production opts.
    Returns optimized ASE Atoms (energy not used downstream).
    """
    import easyxtb

    geom = ase_to_easyxtb(atoms_in, charge=0, uhf=0)
    tag = label or "neutral (GFN-FF pre-opt)"
    print(f"  Optimizing  [{tag}] (GFN-FF/loose) ...", end=" ", flush=True)

    calc = easyxtb.Calculation.opt(geom, level="loose", options={"gfnff": True})
    calc.run()

    if "FAILED TO CONVERGE" in calc.output:
        raise RuntimeError("GFN-FF pre-optimisation did not converge — check starting geometry.")

    print(f"converged  E = {calc.energy:.8f} Eh")
    return easyxtb_to_ase(calc.output_geometry)


# ── CREST conformer pre-screening ────────────────────────────────────────────

def get_lowest_conformer(atoms, charge: int, uhf: int):
    """
    Run CREST iMTD-GC (--squick --gfnff) and return lowest-energy conformer as ASE Atoms.
    --squick = 1 MTD run; --gfnff = GFN-FF force field (fast, no semiempirical cost).
    Requires crest binary in PATH.
    """
    import easyxtb

    geom = ase_to_easyxtb(atoms, charge=charge, uhf=uhf)
    print(f"  Running CREST --squick --gfnff ({len(atoms)} atoms) ...", flush=True)

    try:
        conformers = easyxtb.calculate.conformers(
            geom,
            n_proc=XTB_NPROC,
            options={"squick": True, "gfnff": True}
        )
    except Exception as e:
        raise RuntimeError(
            f"CREST conformer search failed — is 'crest' in PATH?\n"
            f"Original error: {e}"
        ) from e

    if not conformers:
        raise RuntimeError("CREST returned no conformers — check CREST output for errors.")

    print(f"  CREST found {len(conformers)} conformer(s); using lowest.")
    return easyxtb_to_ase(conformers[0]["geometry"])


# ── geometry optimization via easyxtb native ANCopt ──────────────────────────

def optimize_xtb(atoms_in, charge: int, uhf: int,
                 level: str = "tight", label: str = "") -> tuple:
    """
    Geometry-optimize atoms_in at the given charge/uhf state using native xtb ANCopt.
    Uses a unique calc_dir per call so multiple optimizations can run concurrently.

    Returns
    -------
    (optimized_atoms, energy_hartree)
        optimized_atoms : ASE Atoms at converged geometry
        energy_hartree  : total GFN2-xTB energy in Hartree
    """
    cfg = easyxtb.configuration.config
    geom = ase_to_easyxtb(atoms_in, charge, uhf)
    tag = label or f"charge={charge:+d} uhf={uhf}"
    print(f"  Optimizing  [{tag}] ({level}) ...", end=" ", flush=True)

    calc_dir = tempfile.mkdtemp()
    try:
        calc = easyxtb.Calculation(
            program=_XTB_PROGRAM,
            input_geometry=geom,
            runtype="opt",
            runtype_args=[level],
            options={"gfn": cfg["method"], "alpb": cfg["solvent"], "P": cfg["n_proc"]},
            calc_dir=calc_dir,
        )
        calc.run()
        converged = calc.output_geometry is not None
        output    = calc.output
        energy    = calc.energy
        geom_out  = calc.output_geometry
    finally:
        shutil.rmtree(calc_dir, ignore_errors=True)

    if not converged or "FAILED TO CONVERGE" in output:
        raise RuntimeError(f"xtb ANCopt ({level}) did not converge for [{tag}]")

    print(f"converged  E = {energy:.8f} Eh")
    return easyxtb_to_ase(geom_out), energy


# ── single-point energy via easyxtb ──────────────────────────────────────────

def singlepoint_xtb(atoms_in, charge: int, uhf: int, label: str = "") -> float:
    """
    Single-point GFN2-xTB energy at the geometry of atoms_in.
    Uses a unique calc_dir per call so multiple SPs can run concurrently.
    Returns energy in Hartree.
    """
    cfg = easyxtb.configuration.config
    geom = ase_to_easyxtb(atoms_in, charge, uhf)
    tag = label or f"charge={charge:+d} uhf={uhf}"
    print(f"  Single-point [{tag}] ...", end=" ", flush=True)

    calc_dir = tempfile.mkdtemp()
    try:
        calc = easyxtb.Calculation(
            program=_XTB_PROGRAM,
            input_geometry=geom,
            options={"gfn": cfg["method"], "alpb": cfg["solvent"], "P": cfg["n_proc"]},
            calc_dir=calc_dir,
        )
        calc.run()
        energy = calc.energy
    finally:
        shutil.rmtree(calc_dir, ignore_errors=True)

    print(f"{energy:.8f} Eh")
    return energy


# ── result serialization ──────────────────────────────────────────────────────

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
    print("\n[1/5] Generating 3D starting geometry from SMILES ...")
    atoms0_raw, rdkit_mol = smiles_to_atoms(smiles)
    print(f"      {len(atoms0_raw)} atoms")

    # ── GFN-FF pre-optimisation (both paths) ─────────────────────────
    print("\n[2/5] GFN-FF pre-optimisation ...")
    atoms0_preopt = preopt_gfnff(atoms0_raw)

    # ── conformer search (flexible molecules only) ───────────────────
    if is_flexible(rdkit_mol):
        print("\n[3/5] Flexible molecule — running CREST --squick --gfnff ...")
        atoms0_best = get_lowest_conformer(atoms0_preopt, charge=0, uhf=0)
    else:
        print("\n[3/5] Rigid molecule — skipping CREST.")
        atoms0_best = atoms0_preopt

    # ── three tight GFN2-xTB optimizations (parallel) ────────────────
    print("\n[4/5] Tight GFN2-xTB optimizations (parallel) ...")
    with ThreadPoolExecutor(max_workers=OPT_WORKERS) as pool:
        f0     = pool.submit(optimize_xtb, atoms0_best,  0,  0, "tight", "neutral")
        fplus  = pool.submit(optimize_xtb, atoms0_best, +1,  1, "tight", "cation ")
        fminus = pool.submit(optimize_xtb, atoms0_best, -1,  1, "tight", "anion  ")
        geo0,      E0_geo0          = f0.result()
        geo_plus,  E_plus_geoplus   = fplus.result()
        geo_minus, E_minus_geominus = fminus.result()

    # ── four single-points (parallel) ────────────────────────────────
    print("\n[5/5] Cross single-points (parallel) ...")
    with ThreadPoolExecutor(max_workers=SP_WORKERS) as pool:
        f1 = pool.submit(singlepoint_xtb, geo0,      +1, 1, "cation  @ neutral geo")
        f2 = pool.submit(singlepoint_xtb, geo0,      -1, 1, "anion   @ neutral geo")
        f3 = pool.submit(singlepoint_xtb, geo_plus,   0, 0, "neutral @ cation  geo")
        f4 = pool.submit(singlepoint_xtb, geo_minus,  0, 0, "neutral @ anion   geo")
        E_plus_geo0  = f1.result()
        E_minus_geo0 = f2.result()
        E0_geoplus   = f3.result()
        E0_geominus  = f4.result()

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
