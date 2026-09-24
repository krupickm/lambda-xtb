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
image exists if you want one, and stops there.

A release is a tag, and the tag is the only thing that moves prod:

```bash
# 1. Bump both image references to the version you are about to cut.
sed -i 's|lambda-xtb:v1\.1|lambda-xtb:v1.2|g' k8s/base/deployment.yaml
git checkout -b release-v1.2 && git commit -am "Release v1.2" && gh pr create --fill
# 2. Merge it (CI green), then tag that commit:
git checkout main && git pull
git tag v1.2 && git push origin v1.2
```

The tag push builds `cerit.io/krupickm/lambda-xtb:v1.2`, moves `:latest` onto
it, and rolls out prod with the exact tag.

Order matters, and the workflow enforces half of it: a release refuses to build
unless `k8s/base/deployment.yaml` already pins the tag being released, in both
places (the container image and `COMPUTE_IMAGE`). The half it cannot enforce:
**do not `kubectl apply -k k8s/overlays/prod` for a bump whose tag has not been
built yet** — the image does not exist and the pod will sit in
`ImagePullBackOff`.

### Trying something on the test instance

Any branch, no tag, no PR needed:

```
Actions → Build & Push Docker Image → Run workflow → instance: test
```

Builds `:<sha>` and rolls out `lambda-xtb-test`. There is deliberately **no**
manual prod option: prod moves by tag or not at all.

### Rolling back

```bash
kubectl set image deployment/lambda-xtb \
  lambda-xtb=cerit.io/krupickm/lambda-xtb:v1.1 -n krupicka-ns
kubectl set env deployment/lambda-xtb \
  COMPUTE_IMAGE=cerit.io/krupickm/lambda-xtb:v1.1 -n krupicka-ns
```

Then bump `k8s/base/deployment.yaml` back to match, so the manifest stays an
honest record of what is running.

---

## Later, if the pain shows up

Not worth adding until something actually hurts: `ruff format` (a repo-wide
reformat — do it on a quiet day, in its own commit), pre-commit hooks,
Dependabot, a nightly `-m slow` run, auto-generated release notes.
