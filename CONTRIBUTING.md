# Working on λ-xTB

The rule this repo is organised around: **`main` is always green, and prod only
ever runs a tagged release.** Everything below exists to make that true.

---

## One-time setup

```bash
git config core.hooksPath .githooks     # blocks accidental pushes to main
conda env create -f environment.yml     # if you don't have it yet
conda activate xtb-lambda
pip install pytest ruff                 # dev-only, deliberately not in environment.yml
```

`pytest` and `ruff` stay out of `environment.yml` because adding them there
rebuilds the base image (`.github/workflows/base-image.yml`) for everyone.

---

## Day to day

```bash
git checkout main && git pull
git checkout -b <short-slug>
# ... work ...
ruff check .                            # what CI's lint job runs
pytest -q                               # everything, including the xtb smoke test
git push -u origin <short-slug>
gh pr create --fill
```

Merge only once CI is green, and **squash-merge** so `main` keeps one commit per
change.

### Enforcement, and the gap in it

`lambda-xtb` is private on a GitHub Free plan, where branch protection and
rulesets are simply not offered:

```
$ gh api repos/krupickm/lambda-xtb/rulesets
Upgrade to GitHub Pro or make this repository public to enable this feature.
```

So nothing server-side stops a merge over a red build. The `.githooks/pre-push`
hook is the whole enforcement story for now — client-side, and bypassable with
`--no-verify`, which is the point: it makes pushing to `main` a decision rather
than a reflex.

**When the repo goes public or onto Pro**, turn on the real thing —
Settings → Branches → Add rule for `main`:

- Require a pull request before merging
- Require status checks to pass → select `ruff` and `pytest`
- Require branches to be up to date before merging
- Do not allow bypassing the above settings

At that point the hook becomes a redundant convenience, not the safety net.

---

## What CI runs

`.github/workflows/ci.yml`, on every pull request and before every build:

| Job      | What it does                                             |
| -------- | -------------------------------------------------------- |
| `ruff`   | `ruff check` with the config in `pyproject.toml`          |
| `pytest` | unit tests (xtb mocked), then one real xtb calculation    |

The `pytest` job runs **inside the production base image**
(`cerit.io/krupickm/lambda-xtb-base:latest`) instead of rebuilding the conda
env: `environment.yml` is fully pinned and slow to solve, and the smoke test is
only meaningful against the xtb build that actually ships.

### Test markers

Declared in `pyproject.toml`:

- `smoke` — a real xtb run on ethylene (~8 s). The only test that is not
  mocked; it is what notices a broken xtb, easyxtb or base image while every
  mocked test stays green.
- `slow` — realistic-size calculations (minutes). **Never run per commit.**
  CI selects `-m "not smoke and not slow"` for the unit pass.

```bash
pytest -q -m "not smoke and not slow"   # fast, what CI runs first
pytest -q -m smoke                      # the real calculation
```

Keep the smoke test on a molecule that finishes in seconds. Ethylene is 6
atoms with no rotatable bonds, so it skips the CREST conformer search; anything
flexible is an order of magnitude slower.

---

## Releasing

Merging to `main` **deploys nothing**. It builds `:main` and `:<sha>` so an
image exists if you want one, and stops there. A release is a **tag**, and the
tag is the only thing that moves prod.

The code-side half, which is all that happens in this repo:

```bash
# Bump BOTH image references in k8s/base/deployment.yaml (the container image
# and COMPUTE_IMAGE) to the version you are about to cut, in one PR.
git checkout -b release-v1.3 && git commit -am "Release v1.3" && gh pr create --fill
# Merge it with CI green, then tag that commit:
git checkout main && git pull && git tag v1.3 && git push origin v1.3
```

A release **refuses to build** unless the manifest already pins the tag being
released, in both places — so the bump PR always lands first.

The cluster-side half — what the tag rollout does and does not carry, when you
additionally have to `kubectl apply -k`, how to put a branch on the test
instance, and how to roll back — lives with the rest of the operational
documentation in [`README.md`](README.md#releasing-a-new-version).

---

## Later, if the pain shows up

Not worth adding until something actually hurts: `ruff format` (a repo-wide
reformat — do it on a quiet day, in its own commit), pre-commit hooks,
Dependabot, a nightly `-m slow` run, auto-generated release notes.
