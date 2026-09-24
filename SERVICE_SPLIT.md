# Split λ-xTB into Frontend + Per-Request Compute Jobs

## Context

Today λ-xTB is a **single monolithic Flask pod** on CERIT-SC Kubernetes that requests
**4 CPU / 2Gi 24/7** ([k8s/base/deployment.yaml](k8s/base/deployment.yaml)) even though it is idle
almost all the time. Worse, `POST /calculate` runs the 60–120 s `calculate_lambda()`
**synchronously inside the HTTP request** ([app.py:65](app.py#L65)), so the single pod
blocks on one calculation at a time and can time out behind the ingress.

The goal is **better infrastructure utilization** by splitting into two roles:

- **Frontend (UI + API + SQLite)** — tiny, always-on, ~1 CPU. Serves the UI, owns the
  SQLite DB on the existing PVC, exposes an internal API, and **spawns one compute
  container per calculation**.
- **Compute worker** — a Kubernetes **Job created per API call**, requesting **14 CPU**,
  runs `calculate_lambda()`, POSTs the result back to the frontend API, then exits.

Net effect: 14-CPU burst capacity exists only during the ~1–2 min a calculation runs;
the steady-state footprint drops to a 1-CPU/512Mi frontend. Functionality (UI, 3D viewer,
stats, reproducibility runs) is unchanged.

### Infrastructure findings (CERIT-SC)
- Rancher-managed Kubernetes; registry Harbor `cerit.io`; namespace `krupicka-ns`;
  ingress nginx + cert-manager; restricted Pod Security Standard (must run non-root,
  drop ALL caps, seccomp RuntimeDefault). Batch **Jobs are supported**
  (https://docs.cerit-sc.cz/en/docs/kubernetes/job).
- **No Knative / KEDA** is documented → literal scale-to-zero isn't natively available.
  **Decision: minimal always-on frontend** (1 replica, 1 CPU / 512Mi) instead of true 0.
- Creating Jobs from inside the frontend pod is done via the Kubernetes API using a
  **ServiceAccount + Role/RoleBinding** scoped to `krupicka-ns` (we own the namespace).

### Decisions (confirmed with user)
1. **Frontend:** minimal always-on (not serverless install).
2. **UX:** async — **polling status page now + optional email notification** (DB already
   has an `email` column, [db.py:94](db.py#L94)).
3. **Packaging:** **one image, two entrypoints** — frontend runs `flask run`; the compute
   Job overrides the command to `python compute_runner.py`. Reuses the existing two-layer
   Docker base, so CI barely changes.

---

## Architecture & Flow

```
Browser ──POST /calculate (SMILES [+email])──▶ Frontend (Flask, 1 CPU, owns SQLite)
                                                │  insert row status=PENDING
                                                │  create K8s Job (14 CPU)  ──────┐
          ◀──302 redirect /pending/<uuid>───────┘                                 │
Browser ──GET /pending/<uuid> (auto-poll)──▶ Frontend ──GET status──▶ SQLite      ▼
                                                              Compute Job (per call)
Frontend ◀─POST /api/jobs/<uuid>/result (Bearer token)── calculate_lambda() runs, 14 CPU
          write DONE+geometries to SQLite; (optional) send email           exits
Browser  (poll sees DONE) ──▶ redirect to existing /stats/<uuid>
```

- Compute reaches the frontend via the in-cluster Service DNS
  `http://lambda-xtb-svc.krupicka-ns.svc.cluster.local` (existing [k8s/base/service.yaml](k8s/base/service.yaml)).
- Callbacks are authenticated with a shared **Bearer token** (k8s Secret), injected into
  both the frontend and each Job. This is the "DB reached via API from computing" the
  user asked for — **no dedicated DB container**; SQLite stays in the frontend on the PVC.

---

## API Contract & Job States (authoritative)

**The worker exposes no API and listens on nothing.** It is a fire-and-forget batch
process: POST `/start`, compute, POST `/result` or `/error`, exit. Nothing ever calls
*into* the worker. Its state is observable only through two channels:

1. **What it POSTed** to the frontend (start / result / error) — recorded in the SQLite row.
2. **The k8s Job/Pod state + start timestamp** — queried by the frontend from the k8s API.

So a worker has exactly **two possible outcomes**:
- **Graceful:** it catches its own exception and POSTs `/error` → row becomes ERROR with a
  message. (Also the normal success path: POSTs `/result` → DONE.)
- **Silent death:** OOMKilled, evicted, node failure, SIGKILL — it never POSTs anything.
  The frontend detects this from the k8s Job status + elapsed time since start (below).

Two **separate** state machines; do not conflate them:

1. **Application state** — the SQLite row, single source of truth, uses the existing
   `JobStatus` enum ([db.py:23](db.py#L23)). This is what the UI shows.
2. **Kubernetes Job/Pod state** — infra-level (`Pending`/`Running`/`Failed`). Consulted by
   the frontend **only** to detect a silently-dead worker (outcome 2 above).

### State machine (application)
```
 submit ─create Job─▶ PENDING ──/start──▶ PROCESSING ──/result──▶ DONE ──view──▶ SEEN
                         │                    │
                 /error, k8s-fail,     /error, k8s pod Failed/OOM,
                 or timeout            or timeout
                         ▼                    ▼
                            ──────▶ ERROR ◀──────
```
- **PENDING** = Job submitted, pod may be waiting for a 14-CPU slot (the "queued" state).
- **PROCESSING** = worker container actually started `calculate_lambda()`.
- **DONE / ERROR** terminal (plus DONE→SEEN on first view via existing `mark_seen`).
- **Progress granularity: coarse only** (user-confirmed). No per-stage reporting.

### Transition rules
- All callbacks **idempotent + guarded** with `WHERE status = <expected>` (same pattern as
  `mark_seen`, [db.py:227](db.py#L227)), so retries / a late `/start` after `/result` can't
  clobber a terminal row.
- `/result` and `/error` accept from **PENDING or PROCESSING** (a fast job may skip a
  visible PROCESSING step).
- Terminal states ignore all further worker callbacks.

### Endpoints — worker → frontend (Bearer-token auth; token from k8s Secret)
| Endpoint | Body | Effect | Response |
|---|---|---|---|
| `POST /api/jobs/<uuid>/start`  | — | PENDING → PROCESSING | 200 `{ok}`; 409 if terminal |
| `POST /api/jobs/<uuid>/result` | `{lambda_plus_eV, lambda_minus_eV, partial{}, xyz_neutral, xyz_cation, xyz_anion}` | → DONE | 200 |
| `POST /api/jobs/<uuid>/error`  | `{message}` | → ERROR | 200 |

### Endpoint — browser → frontend (the `uuid` is the capability, like today's share links)
| Endpoint | Returns |
|---|---|
| `GET /api/jobs/<uuid>/status` | `{status, redirect: "/stats/<uuid>"\|null, error\|null}` |

### Silent-death detection (handles worker outcome 2 — the only thing the worker can't tell us)
The worker can't report its own SIGKILL/OOM/eviction, so the frontend infers it. On each
`GET /status` for a **non-terminal** job (PENDING/PROCESSING), the frontend consults the
two channels — never the worker itself:
- **k8s Job status** (via the client it already uses to create Jobs): if `.status.failed >
  backoffLimit`, or the Job object no longer exists while the row is still non-terminal →
  mark the row **ERROR** ("compute pod terminated unexpectedly").
- **Start timestamp:** if `now − created_at` exceeds a cutoff (e.g. 15 min) and the row is
  still non-terminal → mark **ERROR** (timeout). Belt-and-suspenders for cases where the
  k8s Job was already garbage-collected (`ttlSecondsAfterFinished`) or the pod is wedged.

This only fires for non-terminal jobs on poll, so it costs nothing in the normal path. It
is the sole mechanism for the silent-death outcome; the graceful outcome needs no
reconciliation because the worker already POSTed `/error`.

---

## Changes

### 1. Compute entrypoint — NEW `compute_runner.py`
Thin wrapper (no Flask). Reads env `JOB_UUID`, `SMILES`, `CALLBACK_BASE_URL`,
`CALLBACK_TOKEN`. Sequence: POST `/start` (→ PROCESSING), run `calculate_lambda(smiles)`
from [lambda_xtb.py](lambda_xtb.py), serialize geometries with the existing
`atoms_to_xyz()` ([lambda_xtb.py:277](lambda_xtb.py#L277)), then POST JSON to
`{CALLBACK_BASE_URL}/api/jobs/{uuid}/result` on success or `/error` on exception
(stdlib `urllib.request`, `Authorization: Bearer …`). Callbacks retry a few times on
transient network errors. Exit 0/1 (a non-zero exit with no `/error` POST is caught by the
frontend's dead-worker reconciliation).

### 2. Parallelism tuning — `lambda_xtb.py`
Today [lambda_xtb.py:53-54](lambda_xtb.py#L53) hard-codes `n_proc=1` / `OMP_NUM_THREADS=1`
to avoid oversubscription on huge shared nodes. Make these **env-driven**
(`XTB_NPROC`, default 1) so a dedicated 14-CPU Job can raise per-xtb threads and CREST
`n_proc` (`get_lowest_conformer()`, [lambda_xtb.py:146](lambda_xtb.py#L146)). Keep the
existing `ThreadPoolExecutor(3)` opts / `(4)` SPs structure; tune thread counts to fit 14
CPU. Conservative defaults preserved for local runs.

### 3. Frontend job-spawning — NEW `jobs.py`
Helper `create_compute_job(job_uuid, smiles)` using the official **`kubernetes`** Python
client (in-cluster config via the mounted ServiceAccount). Builds a Job with:
`restartPolicy: Never`, `backoffLimit: 1`, `ttlSecondsAfterFinished: 3600` (auto-clean),
`command: ["python","compute_runner.py"]`, image `COMPUTE_IMAGE` (same SHA as frontend),
CERIT-SC **restricted securityContext** (mirror [k8s/base/deployment.yaml:175-191](k8s/base/deployment.yaml#L175)),
`emptyDir` mounted at `/tmp` for xtb/CREST scratch, resources
`requests/limits cpu: "14"`, memory (e.g. `4Gi`/`16Gi`), and env
`JOB_UUID/SMILES/CALLBACK_BASE_URL/CALLBACK_TOKEN/XTB_NPROC`.

### 4. Frontend API + async flow — `app.py`
- Rewrite `POST /calculate` ([app.py:49](app.py#L49)): validate SMILES (keep
  `_canonical_smiles`, [app.py:37](app.py#L37)), read optional `email`, insert a
  **PENDING** row, call `create_compute_job(...)`, redirect to `/pending/<uuid>`.
  No more blocking calculation in the request.
- NEW `GET /pending/<uuid>` → renders polling page.
- NEW internal API per the **API Contract** section above:
  - `POST /api/jobs/<uuid>/start` (Bearer) → PENDING → PROCESSING.
  - `POST /api/jobs/<uuid>/result` (Bearer) → body = results + xyz → DONE; optional email.
  - `POST /api/jobs/<uuid>/error` (Bearer) → ERROR; optional email.
  - `GET /api/jobs/<uuid>/status` (uuid = capability) → `{status, redirect, error}`;
    runs **dead-worker reconciliation** for non-terminal jobs (query k8s Job, apply age
    cutoff).
- A small `_require_token()` helper checks `Authorization: Bearer <CALLBACK_TOKEN>` on the
  three worker endpoints; 401 otherwise.
- `/stats/<uuid>` and `/result/<uuid>` unchanged.

### 5. DB layer — `db.py`
Add to the `Database` interface + `SQLiteDatabase`, all **guarded** (update only when the
current status matches the allowed source state, mirroring `mark_seen`, [db.py:227](db.py#L227)):
- `create_pending_job(uuid, smiles_input, canonical, email)` — INSERT status=PENDING.
- `mark_processing(uuid)` — PENDING → PROCESSING (no-op if already terminal).
- `update_result(uuid, results, xyz_*)` / `update_error(uuid, msg)` — **UPDATE** the
  existing row from PENDING/PROCESSING → DONE/ERROR (current `store_job`/`store_error`
  INSERT; switch callbacks to guarded UPDATE so the row becomes DONE/ERROR idempotently).
- `get_status(uuid)` — lightweight `{status, created_at}` fetch for polling + age cutoff.
- Add `PRAGMA busy_timeout=5000` in `_connect()` ([db.py:122](db.py#L122)) to tolerate
  concurrent read (poll) + write (callback). SQLite stays single-writer → frontend keeps
  **1 replica**.

### 6. Templates
- NEW `templates/pending.html` — "Computing…" page, `meta refresh`/small JS poll of
  `/api/jobs/<uuid>/status`, redirects to `/stats/<uuid>` when DONE (to `/` with flash on
  ERROR).
- `templates/index.html` — add optional **email** input.

### 7. Dependencies — `environment.yml`
Add pip `kubernetes` (frontend Job creation). Compute callback uses stdlib `urllib` (no
new dep). `requests` optional. Triggers a base-image rebuild via
[.github/workflows/base-image.yml](.github/workflows/base-image.yml).

### 8. Kubernetes manifests — `k8s/`
- NEW `k8s/rbac.yaml`: `ServiceAccount lambda-xtb-sa` + `Role` (verbs
  create/get/list/delete on `batch/jobs`, get/list on `pods`) + `RoleBinding`.
- NEW `k8s/secret.yaml` **or** `kubectl create secret generic lambda-xtb-callback
  --from-literal=token=…`: the callback Bearer token.
- Update `k8s/deployment.yaml`: add `serviceAccountName: lambda-xtb-sa`; **shrink
  resources** to `requests cpu 250m/256Mi`, `limits cpu "1"/512Mi`; add env
  `NAMESPACE`, `COMPUTE_IMAGE`, `CALLBACK_BASE_URL`
  (`http://lambda-xtb-svc.krupicka-ns.svc.cluster.local`), `CALLBACK_TOKEN` (from Secret),
  and optional SMTP vars. Keep the PVC mount (SQLite only) and restricted securityContext.
- `service.yaml` / `ingress.yaml` / `pvc.yaml` unchanged.

### 9. CI/CD — `.github/workflows/docker-build.yml`
Still builds **one** SHA-tagged image. After `kubectl set image`, also set the Job image
env so it matches: `kubectl set env deployment/lambda-xtb COMPUTE_IMAGE=cerit.io/krupickm/lambda-xtb:${sha} -n krupicka-ns`.

---

## Key reuse (don't rewrite)
- `calculate_lambda()` / `atoms_to_xyz()` — [lambda_xtb.py:289](lambda_xtb.py#L289),
  [lambda_xtb.py:277](lambda_xtb.py#L277): used as-is by `compute_runner.py`.
- `JobStatus` enum incl. unused `PENDING`/`PROCESSING` — [db.py:23](db.py#L23): now wired in.
- `find_all_by_canonical()` / `get_job()` / stats math — [app.py:89-101](app.py#L89),
  [db.py:144](db.py#L144): unchanged.
- Restricted securityContext block — copy from [k8s/base/deployment.yaml:175](k8s/base/deployment.yaml#L175)
  into the Job spec so CERIT-SC PSS admits the compute pod.

---

## Instances (prod + test)

Two independent copies of the whole stack run side by side in `krupicka-ns`, from the
**same image**. Nothing in the source knows which one it is: every difference is a
manifest/env value, so a change can be exercised end-to-end on `test` without touching
the instance being demoed.

| | prod | test |
|---|---|---|
| URL | `lambda-xtb.dyn.cloud.e-infra.cz` | `lambda-xtb-test.dyn.cloud.e-infra.cz` |
| Deployment / Service | `lambda-xtb` / `lambda-xtb-svc` | `lambda-xtb-test` / `lambda-xtb-svc-test` |
| PVC (its own SQLite) | `lambda-xtb-data` | `lambda-xtb-data-test` |
| Callback token Secret | `lambda-xtb-callback` | `lambda-xtb-callback-test` |
| Compute Job size | 14 CPU / 4–16Gi | 14 CPU / 4–16Gi — same as prod |
| Pod label / selector | `app=lambda-xtb` | `app=lambda-xtb-test`, `instance=test` |
| Spawned Jobs labelled | `instance=prod` | `instance=test` |
| Rollout strategy | RollingUpdate (legacy) | `Recreate` |
| Gets `:latest` | yes | no — `:<sha>` only |

The test instance is deliberately **full-power**: its Jobs are the same 14 CPU as prod's, so a
run there reproduces prod's timings and thread behaviour instead of a scaled-down
approximation. The cost is that two concurrent calculations, one per instance, need 28 CPU of
namespace quota — set `JOB_CPU`/`JOB_MEMORY_*` on the test overlay if that ever gets tight.

They cannot interfere. Compute Jobs mount only an `emptyDir` scratch and never a PVC
(results travel back by HTTP callback), Job names carry a uuid4, and `reconcile()` reads
Jobs **by exact name**, never by label selector — the `instance` label on Jobs is for humans
and quota accounting only.

The frontends need one more thing, though, because **`nameSuffix` renames objects but not
label values, and Service selectors match on labels**: left alone, `lambda-xtb-svc-test`
would select `app=lambda-xtb` and so pick up prod's pod, while prod's Service picked up the
test pod — each instance serving a share of the other's traffic against the wrong SQLite.
The test overlay therefore re-labels its pods `app=lambda-xtb-test`, selectors included.
Prod needs no change for this: once test's pods no longer carry `app=lambda-xtb`, prod's
existing selector matches prod's pods only.

### Manifests — `k8s/`

`k8s/base/` holds the six instance-independent manifests; `k8s/overlays/{prod,test}/`
add what differs, via kustomize (built into kubectl — no extra tooling).

- **prod** changes nothing but the namespace: every name is byte-identical to what is
  live, so an apply **adopts** the running objects instead of creating a parallel set.
- **test** sets `nameSuffix: -test` and patches the public host, the in-cluster
  `CALLBACK_BASE_URL`, `INSTANCE`, the Job size, and `strategy: Recreate`. Kustomize
  rewrites the cross-references (PVC `claimName`, `secretKeyRef.name`,
  `serviceAccountName`, RoleBinding subject, Ingress backend service).

```bash
kubectl kustomize k8s/overlays/prod          # inspect — expect today's exact names
kubectl apply -k k8s/overlays/prod --dry-run=server -n krupicka-ns
kubectl apply -k k8s/overlays/test
```

Applying an overlay is always a **deliberate manual step** — CI never applies manifests,
so no pipeline run can rewrite prod's spec.

### Bringing the test instance up

```bash
kubectl create secret generic lambda-xtb-callback-test \
  --from-literal=token=$(openssl rand -hex 32) -n krupicka-ns
kubectl apply -k k8s/overlays/test
gh workflow run "Build & Push Docker Image" --ref <branch> -f instance=test
```

The Secret carries no value in git (base declares the name only, so re-applying an
overlay can never overwrite a live token), and the overlay must exist before the workflow
runs, since the rollout step does `kubectl set image` on an existing Deployment.

Watching a calculation travel through the stack — and the login-node thread hazard that
comes with `kubectl logs -f` — is covered in
[docs/OBSERVING_A_RUN.md](docs/OBSERVING_A_RUN.md).

**Drift warning:** CI pins the Deployment to `:<sha>` with `kubectl set image` / `set env`.
Re-applying an overlay afterwards resets the image back to the manifest's `:latest` —
re-run the workflow for that instance after any apply.

---

## Verification

**Local (no cluster):**
1. `flask run` the frontend with `CALLBACK_TOKEN=dev COMPUTE_IMAGE=… NAMESPACE=…`; for dev,
   short-circuit `create_compute_job` (skip real Job when no in-cluster config).
2. Submit a SMILES (e.g. `c1ccccc1`) → confirm a PENDING row + redirect to `/pending`.
3. Run the worker by hand:
   `JOB_UUID=<uuid> SMILES=c1ccccc1 CALLBACK_BASE_URL=http://localhost:5000 CALLBACK_TOKEN=dev python compute_runner.py`
   → confirm the poll page flips to `/stats/<uuid>` with λ⁺/λ⁻ and 3D viewer.
4. Test `/error` path with a bad SMILES handed to the worker.

**Cluster (krupicka-ns):**
1. `kubectl apply -k k8s/overlays/<instance>` (rbac, secret, deployment, svc, ingress, pvc)
   — see "Instances" above; create that instance's callback-token Secret first.
2. `kubectl describe quota -n krupicka-ns` — confirm 14-CPU Jobs fit the namespace quota.
3. Submit via `https://lambda-xtb.dyn.cloud.e-infra.cz`; watch `kubectl get jobs,pods -w`.
4. Confirm the compute pod is scheduled with **14 CPU** (`kubectl describe pod <job-pod>`),
   the result lands in SQLite, the Job auto-deletes after `ttlSecondsAfterFinished`, and
   and the frontend pod idles well under its 1-CPU request.
5. `kubectl logs job/<name>` on failure paths; verify Bearer-token rejection of
   unauthenticated callbacks.

---

## Implementation Work Packages

Each package below is scoped to become **one GitHub issue**. They are ordered by
dependency but Phase A items are independent and can be picked up in parallel. Every
package lists its own acceptance tests so an implementer agent has self-contained scope.

**GitHub filing:** create a milestone (e.g. `service split`) in this repo's origin remote
(confirm slug with `gh repo view` first), then one issue per WP (WP1–WP9) assigned to that
milestone. Each issue body = the package's Goal/Scope/Depends-on/Acceptance-tests, with the
acceptance tests as a checklist and a "Depends on #N" line for ordering.

### Dependency graph
```
Phase A (no infra, parallel):  WP1        WP2        WP3 (mocked endpoint)
Phase B:                        WP4 (needs WP1)       WP6
Phase C:                        WP5 (needs WP4, WP6)
Phase D:                        WP7
Phase E:                        WP8 (needs WP6, WP7)  WP9 (needs WP7, WP8)
```

---

### WP1 — DB: pending lifecycle + guarded transitions
**Files:** `db.py` (+ `tests/test_db.py`)
**Depends on:** none
**Goal:** Extend the DB layer to support the PENDING→PROCESSING→DONE/ERROR lifecycle with
idempotent, guarded updates.
**Scope:**
- Add to `Database` interface + `SQLiteDatabase`: `create_pending_job`, `mark_processing`,
  `update_result`, `update_error`, `get_status`.
- `update_result`/`update_error`/`mark_processing` = guarded `UPDATE ... WHERE status=<src>`
  (source states per the API Contract); `store_job`/`store_error` become these UPDATEs.
- Add `PRAGMA busy_timeout=5000` in `_connect()`.
**Acceptance tests (pytest, temp sqlite file):**
- create_pending → row is PENDING; mark_processing → PROCESSING; update_result → DONE with
  λ values + xyz stored; `get_status` returns correct `{status, created_at}`.
- Idempotency: a late `mark_processing` after DONE is a no-op; second `update_result` doesn't
  error or overwrite; `update_error` on a DONE row is a no-op.
- `find_all_by_canonical` still returns only DONE/SEEN.
**Out of scope:** any Flask/k8s code.

---

### WP2 — Compute: env-driven parallelism
**Files:** `lambda_xtb.py` (+ `tests/test_parallelism_config.py`)
**Depends on:** none
**Goal:** Let a dedicated 14-CPU worker use more threads without changing local defaults.
**Scope:**
- Replace hard-coded `easyxtb ... n_proc=1` / `OMP_NUM_THREADS=1`
  ([lambda_xtb.py:53-54](lambda_xtb.py#L53)) with reads of `XTB_NPROC` (default `1`).
- Thread `n_proc` into CREST `get_lowest_conformer()` ([lambda_xtb.py:146](lambda_xtb.py#L146))
  and the `ThreadPoolExecutor` worker counts, sized to fit the env value.
**Acceptance tests:**
- With `XTB_NPROC` unset, config resolves to 1 (unchanged behaviour).
- With `XTB_NPROC=8`, `easyxtb.configuration.config["n_proc"]` and the executor sizing
  reflect it. (No full xtb run required; assert on config.)
**Out of scope:** choosing the production thread count (set in WP8 env).

---

### WP3 — Worker entrypoint `compute_runner.py`
**Files:** `compute_runner.py` (+ `tests/test_compute_runner.py`)
**Depends on:** API Contract (mock), `lambda_xtb` (exists), ideally WP2
**Goal:** Fire-and-forget batch runner: POST `/start`, compute, POST `/result` or `/error`,
exit. No server, no listener.
**Scope:**
- Read env `JOB_UUID`, `SMILES`, `CALLBACK_BASE_URL`, `CALLBACK_TOKEN`.
- POST `/start`; run `calculate_lambda`; serialize geometries via `atoms_to_xyz`; POST
  `/result` (or `/error` on exception). stdlib `urllib.request`, `Authorization: Bearer`,
  small retry on transient network errors. Exit 0/1.
**Acceptance tests (stub HTTP server capturing POSTs; `calculate_lambda` monkeypatched):**
- Happy path: receives `/start` then `/result` with the expected JSON keys + Bearer header;
  process exits 0.
- Failure path: patched `calculate_lambda` raises → `/error` POST with message; exit 1.
- Retry: first callback attempt returns 503 → runner retries and succeeds.
**Out of scope:** k8s Job creation (WP6).

---

### WP4 — Frontend internal API + auth
**Files:** `app.py` (+ `tests/test_api.py`)
**Depends on:** WP1
**Goal:** Implement the worker-facing and poll-facing endpoints from the API Contract.
**Scope:**
- `_require_token()` Bearer check (401 on mismatch) for the three worker endpoints.
- `POST /api/jobs/<uuid>/start|result|error`; `GET /api/jobs/<uuid>/status`
  (`{status, redirect, error}`). Reconciliation hook is injectable (stubbed here; real impl
  in WP6).
**Acceptance tests (Flask test client; seed rows via WP1 DB):**
- `/start` flips PENDING→PROCESSING; `/result` → DONE returns redirect `/stats/<uuid>`;
  `/error` → ERROR surfaces message.
- Missing/wrong Bearer → 401; valid token → 200.
- Idempotent repeats don't corrupt state; `/status` returns the right shape per state.
**Out of scope:** the `/calculate` rewrite (WP5), real reconciliation (WP6).

---

### WP5 — Frontend async flow + templates
**Files:** `app.py`, `templates/pending.html` (new), `templates/index.html`
(+ `tests/test_flow.py`)
**Depends on:** WP4 (and WP6's `create_compute_job`, injected/mocked here)
**Goal:** Turn `/calculate` async and add the polling UX.
**Scope:**
- Rewrite `POST /calculate` ([app.py:49](app.py#L49)): validate SMILES, read optional
  `email`, `create_pending_job`, call `create_compute_job(...)` (injected dependency),
  redirect to `/pending/<uuid>`.
- `GET /pending/<uuid>` → polling page that hits `/status` and redirects to `/stats/<uuid>`
  on DONE, `/` + flash on ERROR.
- Add optional email input to `index.html`.
**Acceptance tests (Flask test client, `create_compute_job` mocked):**
- Valid submit → PENDING row created, job-creation hook called once, 302 to `/pending`.
- Invalid SMILES → flash + no row, no hook call.
- `/pending` renders; simulated DONE status drives the client-side redirect target.
**Out of scope:** real Job creation (WP6).

---

### WP6 — k8s Job spawning + silent-death reconciliation
**Files:** `jobs.py` (+ `tests/test_jobs.py`), wire into `app.py` `/status`
**Depends on:** `kubernetes` client (WP7 adds the dep; unit tests mock it)
**Goal:** Create one 14-CPU Job per call and detect silently-dead workers.
**Scope:**
- `build_job_spec(uuid, smiles, image, env)` → pure function returning the Job manifest:
  `restartPolicy: Never`, `backoffLimit: 1`, `ttlSecondsAfterFinished: 3600`,
  `command:["python","compute_runner.py"]`, restricted securityContext (copy
  [k8s/base/deployment.yaml:175](k8s/base/deployment.yaml#L175)), `emptyDir` at `/tmp`, resources
  `cpu:"14"`, env (`JOB_UUID/SMILES/CALLBACK_BASE_URL/CALLBACK_TOKEN/XTB_NPROC`).
- `create_compute_job(...)` submits via in-cluster client.
- `reconcile(uuid)`: query Job status + compare `created_at` to cutoff → mark ERROR on
  `.status.failed > backoffLimit`, missing Job, or timeout.
**Acceptance tests (k8s client mocked):**
- `build_job_spec` asserts resources=14 CPU, command, securityContext fields, env, ttl,
  backoffLimit, emptyDir mount.
- `reconcile`: failed Job → ERROR; running-and-young → unchanged; missing Job + non-terminal
  row → ERROR; age > cutoff → ERROR.
**Out of scope:** RBAC manifests (WP8).

---

### WP7 — Dependencies + single dual-entrypoint image
**Files:** `environment.yml`, `Dockerfile` (verify), docs
**Depends on:** none (merge after WP3/WP6 exist so both entrypoints are present)
**Goal:** One image runs both roles.
**Scope:** Add pip `kubernetes` to `environment.yml` (triggers base-image rebuild). Confirm
`COPY . .` ships `compute_runner.py`; frontend `CMD ["flask","run"]` unchanged; Job
overrides `command`.
**Acceptance tests:**
- `docker build` succeeds; `import kubernetes` works in the image.
- Container with default CMD serves the UI; same image with
  `command: python compute_runner.py` runs the worker against a stub endpoint.
**Out of scope:** manifest wiring (WP8).

---

### WP8 — Kubernetes manifests (RBAC, Secret, deployment)
**Files:** `k8s/rbac.yaml` (new), `k8s/secret.yaml` (or `kubectl create secret`),
`k8s/deployment.yaml`
**Depends on:** WP6, WP7
**Goal:** Give the frontend permission to create Jobs and the config to do so; shrink its
footprint.
**Scope:**
- `ServiceAccount lambda-xtb-sa` + `Role` (create/get/list/delete `batch/jobs`, get/list
  `pods`) + `RoleBinding`.
- Callback-token Secret.
- `deployment.yaml`: add `serviceAccountName`, shrink to `requests cpu 250m/256Mi` /
  `limits 1/512Mi`, add env (`NAMESPACE`, `COMPUTE_IMAGE`, `CALLBACK_BASE_URL`,
  `CALLBACK_TOKEN` from Secret). Keep PVC mount + restricted securityContext.
**Acceptance tests (krupicka-ns):**
- `kubectl apply --dry-run=server -f k8s/` passes; `kubectl auth can-i create jobs
  --as=system:serviceaccount:krupicka-ns:lambda-xtb-sa` → yes.
- End-to-end: submit via UI → Job scheduled with **14 CPU** (`kubectl describe pod`),
  result lands in SQLite, Job auto-deletes after TTL, idle frontend ~250m CPU.
- `kubectl describe quota` confirms a 14-CPU Job fits.
**Out of scope:** CI changes (WP9).

---

### WP9 — CI/CD: keep COMPUTE_IMAGE in lockstep
**Files:** `.github/workflows/docker-build.yml`
**Depends on:** WP7, WP8
**Goal:** Ensure the Job uses the same SHA image as the freshly-deployed frontend.
**Scope:** After `kubectl set image`, add `kubectl set env deployment/lambda-xtb
COMPUTE_IMAGE=cerit.io/krupickm/lambda-xtb:${sha} -n krupicka-ns`; keep the rollout wait.
**Acceptance tests:**
- Push to main → workflow sets both the container image and `COMPUTE_IMAGE` to the same
  SHA; `kubectl rollout status` succeeds; a post-deploy calculation spawns a Job on that SHA.
**Out of scope:** manifest content (WP8).

---

### WP10 — Cleanup chores (living list)
**Files:** whatever each item names (see below)
**Depends on:** the WP that introduced the item (noted per item)
**Goal:** Catch tech debt that earlier WPs deliberately deferred to stay in scope, so it
doesn't get lost. This is **not a one-shot WP** — it's a running checklist. Every
implementer agent, on finishing its own WP, **must check this list for an item unblocked by
its work and, if found and small, do it as part of a *separate* commit on its own branch**
(never silently folded into the WP's main commit). If an agent's WP creates *new*,
deliberately-deferred debt, it should **add a line here** (edit this file only — do not
touch other WPs' sections) rather than expanding its own scope.

**Format per item:** `- [ ] <what> — introduced by WP<N>, unblocked by WP<M>. <why deferred>`

**Chores:**
- [x] Remove `store_job`/`store_error` from `db.py` (`Database` ABC + `SQLiteDatabase`) once
  `app.py` no longer calls them — introduced by WP1 (kept them as the old INSERT-based
  methods so the pre-existing synchronous `/calculate` flow kept working), unblocked by
  WP5 (rewrites `/calculate` to use `create_pending_job`/`update_result`/`update_error`
  instead). Also drop the now-redundant `store_job`/`store_error` acceptance tests in
  `tests/test_db.py` if any were added for them. Done in a separate commit on
  `wp5-frontend-flow`; `tests/test_db.py` had no tests for them to drop.

**Out of scope:** anything that isn't a small, mechanical follow-up to an already-merged
WP — file a proper new WP/issue instead of growing this list unboundedly.

---

## Out of scope / follow-ups
- True scale-to-zero (Knative/KEDA HTTP add-on) — needs CERIT-SC admin; revisit if idle
  frontend cost matters.
- Concurrency cap on simultaneous Jobs (rely on namespace quota queuing for now).
- Migrating SQLite → Postgres (the `db.py` abstraction already anticipates it) if multi-
  replica frontend is ever needed.
