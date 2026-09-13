# AGENTS.md — Implementer guide for the λ-xTB service split

You are an implementer agent. You will be assigned **exactly one work package (WP)** —
one GitHub issue (`#1`–`#9`) under the **service split** milestone. Do that WP well and
nothing else. A human reviews your branch, runs the tests, and handles all deployment.

> Read this file fully before touching code. It overrides your default habits.

---

## 1. What we are building (one paragraph)

The monolithic Flask pod is being split into a **tiny always-on frontend** (UI + internal
API + SQLite on a PVC) that **spawns one 14-CPU Kubernetes Job per calculation**. The
worker is **fire-and-forget**: it POSTs `start`/`result`/`error` back to the frontend API
and exits — it exposes no API of its own. Full design, the **authoritative API contract**,
and the job **state machine** are in [`SERVICE_SPLIT.md`](SERVICE_SPLIT.md). Read the
relevant parts; do not invent alternative endpoints, JSON keys, or states.

---

## 2. Before you start (per WP)

1. `gh issue view <N>` — your scope, acceptance-test checklist, and `Depends on` line.
2. Read [`SERVICE_SPLIT.md`](SERVICE_SPLIT.md) — especially **“API Contract & Job States”**.
3. Read the files your WP names, plus the code you must reuse (do **not** rewrite these):
   - `calculate_lambda()`, `atoms_to_xyz()` — `lambda_xtb.py`
   - `JobStatus`, the guarded-`UPDATE` pattern (see `mark_seen`) — `db.py`
   - `_canonical_smiles()`, existing routes — `app.py`
4. Assume every WP in your `Depends on` line is already merged into your working tree. If it
   is **not** present, **stop and report** — do not reimplement a dependency.

---

## 3. Environment

- Conda env **`xtb-lambda`** (Python 3.11). Activate it before running anything.
- Run the app: `FLASK_APP=app.py flask run --host=0.0.0.0`
- Tests: **pytest**. If missing, `pip install pytest` into the active env (dev-only; only
  add it to `environment.yml` if your WP already edits deps, since that triggers a
  base-image rebuild).
- If there is no `tests/` directory yet, create it with an empty `tests/__init__.py` and a
  `tests/conftest.py` as needed.

---

## 3a. Dev data is disposable

`data/lambda.db` (and anything else under `data/`) is a **local dev/scratch database with
no real data** — running the app, importing `app.py`, or running tests may write to it.
That is expected and fine; it is not a scope violation and does not need to be reverted or
called out in your handoff. (Real data only ever lives on the cluster PVC, which you never
touch — see §4.)

## 4. Guardrails — do NOT

- ❌ Deploy, `kubectl apply`/`set image`, or touch the `krupicka-ns` cluster.
- ❌ Build or push Docker images to `cerit.io`.
- ❌ Push to `main`, open/merge PRs, or force-push.
- ❌ Edit GitHub issues, the milestone, or close your issue.
- ❌ Hit real infrastructure or the network in tests — **mock it** (see §6).
- ❌ Hardcode secrets/tokens — always read from env vars.
- ❌ Touch files outside your WP’s scope, or refactor unrelated code “while you’re there”.
- ❌ Add heavy dependencies unless the WP explicitly calls for them (prefer stdlib —
  e.g. `urllib.request` over `requests`).

## 5. Guardrails — do

- ✅ Match the existing code style: stdlib-first, type hints, module/function docstrings,
  small pure functions, the guarded-transition SQL pattern already in `db.py`.
- ✅ Keep the diff **minimal and reviewable**.
- ✅ Work on a feature branch: `git checkout -b wp<N>-<short-slug>` (off `main`, or off the
  dependency branch if the human tells you which). Commit locally with clear messages
  ending in:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- ✅ Leave the branch **committed but unpushed** for human review, unless told otherwise.

---

## 6. Testing rules

- Every acceptance-criteria checkbox in the issue must be covered by a passing test.
- Mock all infrastructure — no real cluster, no real network:
  - **WP3** (worker): stub HTTP server capturing POSTs; monkeypatch `calculate_lambda`.
  - **WP4** (API): Flask test client; seed DB rows directly; stub the reconciliation hook.
  - **WP5** (flow): Flask test client; mock `create_compute_job`.
  - **WP6** (jobs): mock the `kubernetes` client; unit-test `build_job_spec` as a pure
    function and `reconcile` with fabricated Job statuses/timestamps.
- The API contract is **exact**: endpoint paths, JSON keys, status codes, Bearer auth, and
  the PENDING→PROCESSING→DONE/ERROR transitions must match `SERVICE_SPLIT.md`.
- Run `pytest -q` and make sure **pre-existing tests still pass** and the app still imports.

---

## 7. Definition of Done — checklist

Copy this into your final message with each box marked:

- [ ] Every acceptance-criteria item from the issue is implemented **and** covered by a test.
- [ ] `pytest -q` passes (paste the command + output tail).
- [ ] No files changed outside the WP’s stated scope.
- [ ] Reused existing helpers instead of duplicating (`calculate_lambda`, `atoms_to_xyz`,
      `JobStatus`, `_canonical_smiles`, guarded-`UPDATE` pattern).
- [ ] API/state behavior matches `SERVICE_SPLIT.md` exactly (if applicable).
- [ ] App still imports/boots where relevant; no debug prints or dead code left behind.
- [ ] Feature branch `wp<N>-<slug>` committed locally (not pushed).
- [ ] Final report written (see §8).

---

## 8. What to hand back (final message)

Your last message is the handoff to the human reviewer. It must contain:

1. **Branch name** and list of **files changed** (with one-line rationale each).
2. **Test command and its output** (the `pytest -q` tail).
3. The **Definition-of-Done checklist** from §7, filled in.
4. **Assumptions / deviations** from the issue or design, and why.
5. **What the human must do next**: anything to wire up, review carefully, or deploy
   (e.g. “WP8 must add the RBAC before this runs on-cluster”, “base image rebuild needed”).

Keep it factual. If tests fail or something is incomplete, **say so plainly** — do not
claim done.

---

## 9. Suggested execution order (for the human running the agents)

Dependency-respecting order (see each issue’s `Depends on`):

```
Phase A (parallel, no deps):   #1  #2  #3
Phase B:                       #4 (needs #1)      #6
Phase C:                       #5 (needs #4, #6)  #7
Phase D:                       #8 (needs #6, #7)
Phase E:                       #9 (needs #7, #8)
```
