"""End-to-end smoke test: one real xtb calculation, no mocks.

Every other test in this suite stubs `calculate_lambda` out, so the whole
pipeline can be green while xtb itself is broken — a missing binary, an
incompatible easyxtb, a base image built without `tblite`. This test is the
one that would notice.

Ethylene is the smallest molecule the four-point recipe is meaningful for:
6 atoms, no rotatable bonds (so `is_flexible` skips the CREST conformer
search), 3 optimizations + 4 single-points in ~7 s wall clock.

Marked `smoke` so it can be selected or skipped explicitly; it needs the real
conda env (xtb, rdkit, ase, easyxtb) and is the only test here that does.
"""

import pytest

from lambda_xtb import atoms_to_xyz, calculate_lambda

pytestmark = pytest.mark.smoke

ETHYLENE = "C=C"

# Reference values from GFN2-xTB as pinned in environment.yml (xtb 6.7.1):
# λ⁺ ≈ 366 meV, λ⁻ ≈ 898 meV. The assertions below are deliberately loose —
# this test asks "did xtb run and return physically sane numbers?", not
# "does it reproduce to three decimals". Tighten only if you want the suite
# to fail on an xtb version bump.
LAMBDA_PLUS_REF_MEV = 366.0
LAMBDA_MINUS_REF_MEV = 898.0
TOLERANCE = 0.25  # ±25 %


@pytest.fixture(scope="module")
def result():
    """Run the real four-point calculation once for the whole module."""
    return calculate_lambda(ETHYLENE)


def test_returns_the_documented_keys(result):
    assert set(result) >= {
        "lambda_plus_eV",
        "lambda_minus_eV",
        "lambda_plus_meV",
        "lambda_minus_meV",
        "partial",
        "geometries",
    }
    assert set(result["geometries"]) == {"neutral", "cation", "anion"}


@pytest.mark.parametrize(
    ("key", "reference"),
    [
        ("lambda_plus_meV", LAMBDA_PLUS_REF_MEV),
        ("lambda_minus_meV", LAMBDA_MINUS_REF_MEV),
    ],
)
def test_reorganization_energy_is_physically_sane(result, key, reference):
    value = result[key]

    # A negative λ means a geometry or convergence failure — lambda_xtb only
    # warns about it, so assert it here.
    assert value > 0, f"{key} is negative ({value:.1f} meV) — bad geometry or convergence"
    assert abs(value - reference) <= TOLERANCE * reference, (
        f"{key} = {value:.1f} meV is more than {TOLERANCE:.0%} away from the "
        f"reference {reference:.1f} meV — xtb, easyxtb or the recipe changed"
    )


def test_optimized_geometries_are_ethylene(result):
    """The geometries the worker POSTs back must be real, writable XYZ."""
    for state, atoms in result["geometries"].items():
        xyz = atoms_to_xyz(atoms)
        lines = xyz.splitlines()

        assert lines[0].strip() == "6", f"{state}: expected 6 atoms, got {lines[0]!r}"
        assert len(lines) == 8, f"{state}: malformed XYZ ({len(lines)} lines)"
        assert sorted(line.split()[0] for line in lines[2:]) == ["C", "C", "H", "H", "H", "H"]
