"""
Acceptance tests for WP2 — env-driven CREST parallelism in lambda_xtb.py.

Design (per project owner guidance): molecules here are too small to
benefit from OpenMP/-P threading inside a single xtb call — synchronization
overhead outweighs the gain — so every individual xtb worker (opt, SP, and
each CREST conformer) always runs on 1 CPU. Only CREST's own internal
parallelism (how many single-threaded conformer searches it runs
concurrently) scales with XTB_NPROC.

No xtb/CREST binaries are invoked; `easyxtb.calculate.conformers` and the
geometry converters are monkeypatched. Tests assert on config/env state and
the CREST call's n_proc argument only.
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


def test_per_call_xtb_config_always_pinned_to_one_cpu(monkeypatch):
    """
    Regardless of XTB_NPROC, every individual xtb call (opt/SP, via
    easyxtb.configuration.config["n_proc"] / OMP_NUM_THREADS) stays pinned
    to 1 CPU — small molecules don't benefit from OpenMP/-P threading inside
    a single xtb call.
    """
    mod = _reload_with_env(monkeypatch, 8)

    assert mod.XTB_NPROC == 8
    assert easyxtb.configuration.config["n_proc"] == 1
    assert os.environ["OMP_NUM_THREADS"] == "1"


def test_invalid_or_nonpositive_env_falls_back_to_default(monkeypatch):
    assert _reload_with_env(monkeypatch, "not-a-number").XTB_NPROC == 1
    assert _reload_with_env(monkeypatch, 0).XTB_NPROC == 1
    assert _reload_with_env(monkeypatch, -3).XTB_NPROC == 1


def test_pool_worker_counts_are_fixed(monkeypatch):
    """
    calculate_lambda()'s ThreadPoolExecutor sizes (pool-level parallelism
    across independent, single-CPU xtb workers) stay fixed regardless of
    XTB_NPROC — only CREST's internal fan-out scales with the budget.
    """
    mod = _reload_with_env(monkeypatch, 8)
    assert mod.OPT_WORKERS == 3
    assert mod.SP_WORKERS == 4


def test_crest_receives_full_env_budget(monkeypatch):
    """
    get_lowest_conformer() threads XTB_NPROC into CREST's n_proc — the only
    place the budget is used, since CREST is the sole stage that benefits
    from more CPUs (it manages its own process-level parallelism across
    many single-threaded conformer searches).
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


def test_crest_uses_default_nproc_when_env_unset(monkeypatch):
    """With XTB_NPROC unset, CREST still gets n_proc=1 (unchanged behaviour)."""
    mod = _reload_with_env(monkeypatch, None)

    captured = {}

    def fake_conformers(geom, n_proc=None, options=None):
        captured["n_proc"] = n_proc
        return [{"geometry": "lowest-geom"}]

    monkeypatch.setattr(mod, "ase_to_easyxtb", lambda atoms, charge, uhf: "geom-in")
    monkeypatch.setattr(mod, "easyxtb_to_ase", lambda geom: geom)
    monkeypatch.setattr(mod.easyxtb.calculate, "conformers", fake_conformers)

    mod.get_lowest_conformer(atoms=[0, 0, 0], charge=0, uhf=0)

    assert captured["n_proc"] == 1
