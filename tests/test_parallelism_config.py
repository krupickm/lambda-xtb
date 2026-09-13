"""
Acceptance tests for WP2 — env-driven xtb/CREST parallelism in lambda_xtb.py.

No xtb/CREST binaries are invoked; easyxtb's `calculate.conformers` and the
geometry converters are monkeypatched where a call path would otherwise
reach out to them. Tests assert on config/env state and the pure sizing
helpers only.
"""
import importlib
import os

import easyxtb

import lambda_xtb


def _reload_with_env(monkeypatch, value):
    """Reload lambda_xtb with XTB_NPROC set to `value` (None = unset)."""
    if value is None:
        monkeypatch.delenv("XTB_NPROC", raising=False)
    else:
        monkeypatch.setenv("XTB_NPROC", str(value))
    return importlib.reload(lambda_xtb)


def test_default_nproc_resolves_to_one(monkeypatch):
    """XTB_NPROC unset -> config resolves to 1 (unchanged local behaviour)."""
    mod = _reload_with_env(monkeypatch, None)

    assert mod.XTB_NPROC == 1
    assert easyxtb.configuration.config["n_proc"] == 1
    assert os.environ["OMP_NUM_THREADS"] == "1"


def test_env_nproc_is_applied_to_config(monkeypatch):
    """XTB_NPROC=8 -> easyxtb config / OMP_NUM_THREADS reflect it."""
    mod = _reload_with_env(monkeypatch, 8)

    assert mod.XTB_NPROC == 8
    assert easyxtb.configuration.config["n_proc"] == 8
    assert os.environ["OMP_NUM_THREADS"] == "8"


def test_invalid_or_nonpositive_env_falls_back_to_default(monkeypatch):
    assert _reload_with_env(monkeypatch, "not-a-number").XTB_NPROC == 1
    assert _reload_with_env(monkeypatch, 0).XTB_NPROC == 1
    assert _reload_with_env(monkeypatch, -3).XTB_NPROC == 1


def test_executor_sizing_reflects_env_value(monkeypatch):
    """
    Per-call n_proc for each ThreadPoolExecutor phase in calculate_lambda()
    scales with the XTB_NPROC budget, split across the (fixed) number of
    concurrent workers in that phase.
    """
    mod = _reload_with_env(monkeypatch, None)
    assert mod.OPT_WORKERS == 3
    assert mod.SP_WORKERS == 4

    # default budget (1) -> 1 thread per call regardless of worker count
    assert mod._per_call_nproc(1, mod.OPT_WORKERS) == 1
    assert mod._per_call_nproc(1, mod.SP_WORKERS) == 1

    # XTB_NPROC=8 -> split across the fixed worker counts, never below 1
    assert mod._per_call_nproc(8, mod.OPT_WORKERS) == 2   # 8 // 3
    assert mod._per_call_nproc(8, mod.SP_WORKERS) == 2    # 8 // 4


def test_crest_receives_full_env_budget(monkeypatch):
    """
    get_lowest_conformer() threads XTB_NPROC into CREST's n_proc. CREST runs
    as a single task (no concurrent phase), so it gets the full budget
    rather than a division of it.
    """
    mod = _reload_with_env(monkeypatch, 8)

    captured = {}

    def fake_conformers(geom, n_proc=None, options=None):
        captured["n_proc"] = n_proc
        captured["options"] = options
        return [{"geometry": "lowest-geom"}]

    monkeypatch.setattr(mod, "ase_to_easyxtb", lambda atoms, charge, uhf: "geom-in")
    monkeypatch.setattr(mod, "easyxtb_to_ase", lambda geom: geom)
    monkeypatch.setattr(mod.easyxtb.calculate, "conformers", fake_conformers)

    result = mod.get_lowest_conformer(atoms=[0, 0, 0], charge=0, uhf=0)

    assert captured["n_proc"] == 8
    assert captured["options"] == {"squick": True, "gfnff": True}
    assert result == "lowest-geom"
