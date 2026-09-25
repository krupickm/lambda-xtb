# λ-xTB: Reorganization Energy Calculator — Project Devlog

> **Status**: live at `lambda-xtb.dyn.cloud.e-infra.cz`, split into an always-on frontend
> plus per-calculation 14-CPU Kubernetes Jobs, running as two independent instances
> (prod + test) on CERIT-SC k8s. See [`README.md`](README.md) for the current operational
> state and exact version, which moves faster than this line would stay accurate.
> **Location**: VS Code Remote → MetaCentrum (brno2)
> **Repo**: `WORK/2026-tobrman-polovodic/lambda-xtb/`

This file is the project's running engineering journal: why things are built the way they
are, the science behind the numbers, and the gotchas that cost real time to figure out. For
"how do I run/deploy this," see [`README.md`](README.md); for the authoritative current
architecture and API contract, see [`SERVICE_SPLIT.md`](SERVICE_SPLIT.md) — this file is the
narrative, those are the reference.

---

## What this project does

Calculates **intramolecular reorganization energy (λ)** for organic π-conjugated molecules
(OLED/OSC materials) using the **Nelsen four-point method** at GFN2-xTB level.

Takes a SMILES string → returns λ⁺ (hole transport) and λ⁻ (electron transport) in eV/meV.

**Use case**: Colleague uploads a structure, gets λ⁺ and λ⁻ back without touching the CLI.

---

## Science background

### Four-point recipe (Nelsen/Gruhn)

Seven calculations: 3 geometry optimizations + 4 single-points.

```
geo0   = optimized neutral      (charge=0,  uhf=0)
geo+   = optimized cation       (charge=+1, uhf=1)
geo-   = optimized anion        (charge=-1, uhf=1)

E+(geo0)  = SP: cation  @ neutral geometry     ← cross-evaluations
E-(geo0)  = SP: anion   @ neutral geometry
E0(geo+)  = SP: neutral @ cation  geometry
E0(geo-)  = SP: neutral @ anion   geometry
```

```
λ⁺ = [E+(geo0) - E+(geo+)] + [E0(geo+) - E0(geo0)]    hole transport
λ⁻ = [E-(geo0) - E-(geo-)] + [E0(geo-) - E0(geo0)]    electron transport
```

All partials (λ₁, λ₂) must be **positive**. Negative values = geometry/convergence problem.

### Calculation pipeline

For each submitted SMILES:

1. **MMFF geometry** — RDKit ETKDG + MMFF force field, fast 3D embed
2. **GFN-FF pre-opt** — easyxtb loose optimisation, prepares geometry for CREST
3. **CREST conformer search** — `--squick --gfnff` (3 MTD runs), flexible molecules only (gates on rotatable bonds). Returns lowest-energy conformer.
4. **Tight GFN2-xTB optimisations** — neutral, cation, anion run **in parallel** via `ThreadPoolExecutor(max_workers=3)`
5. **Four cross single-points** — GFN2-xTB, all four run **in parallel** via `ThreadPoolExecutor(max_workers=4)`

Thread safety: each `optimize_xtb` / `singlepoint_xtb` call gets its own `tempfile.mkdtemp()` as `calc_dir`, passed to the `easyxtb.Calculation()` constructor. Cleaned up in `finally`. No shared state between concurrent calls.

### Method: GFN2-xTB
- Semiempirical tight-binding, ~seconds per molecule
- Accuracy: good for screening / relative comparisons
- Not publication-quality for absolute λ (use B3LYP/6-31G* for that)
- Charged species: doublets (uhf=1) for both cation and anion

### Key references
- Nelsen, Blackstock & Kim, JACS **1987**, 109, 677 — original four-point method
- Gruhn et al., JACS **2002**, 124, 7918 (doi:10.1021/ja0175892) — DFT/xTB validation, pentacene
- Coropceanu et al., Chem. Rev. **2007**, 107, 926 — canonical OSC review

---

## Environment setup

### Why conda (not pip/uv)

`xtb-python` PyPI wheels are frozen at v22.1 (Python 3.7–3.11 only, manylinux2010).
The upstream project recommends **conda-forge** as the only supported install path.

### Create the environment

```bash
conda create -n xtb-lambda python=3.11
conda activate xtb-lambda
conda install -c conda-forge xtb rdkit ase crest easyxtb flask
python -m ipykernel install --user --name xtb-lambda --display-name "Python (xtb-lambda)"
```

### Export for reproducibility

```bash
conda env export -n xtb-lambda > environment.yml
```

Use `conda env create -f environment.yml` to recreate on MetaCentrum JupyterHub.

---

## Known issues / gotchas

### easyxtb: n_proc auto-detection oversubscribes on k8s nodes

**Symptom**: `xtb` launched with `-P 98`, using 400% CPU, calculation never finishes or is much slower than serial.

**Cause**: easyxtb reads `os.cpu_count() // 1.3` at import time. On a k8s node with 98 logical CPUs (even if only 4 are allocated to the pod), this passes `-P 75` or similar to every xtb call.

**Fix**: Override at module level before any calculation:
```python
import easyxtb
easyxtb.configuration.config["n_proc"] = 1
```
CREST passes `n_proc=4` explicitly where desired.

### CREST reproducibility

**Symptom**: Same molecule submitted twice returns slightly different λ values.

**Cause**: `--mquick` (1 MTD run) had high stochastic variance. Switched to `--squick` (3 MTD runs) — better conformer sampling, ~30s overhead for small molecules. The multi-run statistics page lets users judge spread empirically.

---

## Architecture decisions

### Deployment path

```
Phase 1 [done]     CLI script — lambda_xtb.py, SMILES via argv
Phase 2 [done]     Flask web service — SMILES in, HTML out
Phase 3 [done]     Docker image → push to cerit.io
Phase 4 [done]     Kubernetes Deployment + Service + Ingress on CERIT-SC
Phase 5 [done]     SQLite multi-run storage + shareable /result/<uuid> URLs + stats page
Phase 6 [done]     Parallel xtb execution — ThreadPoolExecutor + isolated calc_dir per call
Phase 7 [done]     Service split — async job queue via per-calculation k8s Jobs (below)
Phase 8 [done]     Two independent instances (prod + test) from one image, one manifest tree
Phase 9 [BETA]     e-infra AAI single sign-on gating both instances
```

### The service split, and building it with agents

#### The problem it solved

Through Phase 6, the Flask route ran the full ~60–120 s calculation **synchronously inside
the HTTP request**, on a single 4-CPU pod that requested that CPU **24/7** even though it
sat idle almost all day. Consequences:
- **One calculation at a time** — concurrent requests queued behind each other.
- **No progress feedback** — the browser just waited.
- **Timeout risk** — slow molecules or a busy server could hit proxy timeouts.
- **Wasted quota** — 4 CPU reserved permanently for a workload that runs a few minutes a day.

#### The fix: two roles instead of one pod

The service is now split into a **tiny always-on frontend** (UI + internal API + SQLite on
a PVC, ~1 CPU) and a **compute worker that exists only as a Kubernetes Job, one per
calculation, requesting 14 CPU**. The worker is fire-and-forget: no server, no listener,
just `POST /start` → run `calculate_lambda()` → `POST /result` (or `/error`) → exit. The
frontend tracks each calculation through `PENDING → PROCESSING → DONE/ERROR` in SQLite (the
existing `JobStatus` enum, `db.py`), and separately consults the *Kubernetes* Job/Pod state
to detect a worker that died silently (OOMKilled, evicted, node failure) without ever being
able to report it. Full design, the exact API contract, and the state machine are in
[`SERVICE_SPLIT.md`](SERVICE_SPLIT.md).

Net effect: 14-CPU burst capacity exists only for the ~1–2 minutes a calculation actually
runs; steady-state footprint dropped to roughly 250m CPU. The email column that had sat
unused in the schema since Phase 5 (`db.py`) is now wired up as optional async notification.
A later pass (Phase 8) turned "one instance" into **two independent instances from the same
image** (prod + test), covered in [`README.md`](README.md#kubernetes-deployment-cerit-sc)
and `SERVICE_SPLIT.md`'s "Instances" section — driven by the very practical constraint that
the production instance had to stay untouched ahead of a conference presentation while the
split itself was still being proven out on real hardware.

#### Built as nine reviewable work packages, by AI coding agents

The split was scoped as a design doc first ([`SERVICE_SPLIT.md`](SERVICE_SPLIT.md), written
with Claude before any code changed) and then broken into **nine independent work packages**
(WP1–WP9 in that doc, one GitHub issue each), each small enough that one coding agent could
implement it end to end without touching the others. The rules those agents worked under are
in [`AGENTS.md`](AGENTS.md): one agent per work package, mocked infrastructure only (no real
cluster, no real network in tests), every acceptance-test checkbox in the issue backed by a
passing test, work left on an **unpushed local branch** for a human to review — no agent
merges, deploys, or touches the cluster itself. Each branch's tests were re-run at the tip of
a shared integration branch before the next dependent package started (see the dependency
graph in `SERVICE_SPLIT.md`).

It did not eliminate manual testing. Docker builds and on-cluster behavior aren't things the
sandbox those agents ran in could exercise at all (no Docker daemon, no `kubectl`), so real
`docker build`/`docker run` and real `kubectl apply` runs against the cluster caught three
production bugs the mocked unit tests structurally could not: a `data/` directory baked into
the image with the wrong ownership for a non-root container; a missing dev-mode short-circuit
that turned a normal local run into a raw `KeyError` on `POST /calculate`; and, once live,
that a k8s Service selects pods by label, not by name — so the test instance's pods needed
their own `app=` label or the two instances would have silently served each other's traffic
against the wrong SQLite database. Each is a small, instructive lesson about the boundary
between "the unit tests pass" and "it survives the wrong pod scheduling to the wrong node," and each was fixed as its own commit once found.

### Why Flask (not Jupyter/Voilà for the web service)

- Flask is a minimal Python web framework: one file, no JS build step
- Colleague gets a real URL, not a notebook interface
- Clean separation: `lambda_xtb.py` is pure science, `app.py` is pure web plumbing

### Docker: base image split

The conda environment takes 2–3 min to build. Split into two images to keep CI fast:

| Image | Dockerfile | Triggers | Build time |
|-------|-----------|----------|------------|
| `lambda-xtb-base:latest` | `Dockerfile.base` | `environment.yml` or `Dockerfile.base` change | ~3 min |
| `lambda-xtb:latest` | `Dockerfile` | every push to `main` | ~20 s |

`Dockerfile.base` contains: OS patches + CVE mitigations + conda env + PyJWT fix + `XDG_DATA_HOME` config.
`Dockerfile` is just `FROM base`, `COPY . .`, `USER 1000`, `CMD`.

### CERIT-SC Kubernetes security requirements

CERIT-SC enforces the **restricted PodSecurity standard** — pod must run as non-root.

```yaml
securityContext:
  runAsNonRoot: true
  fsGroupChangePolicy: OnRootMismatch
  seccompProfile:
    type: RuntimeDefault
containers:
  - securityContext:
      runAsUser: 1000
      allowPrivilegeEscalation: false
      capabilities:
        drop: [ALL]
```

`/app` stays root-owned (read-only for user 1000). Only `/tmp` is writable at runtime.
easyxtb temp files go to `/tmp` via `XDG_DATA_HOME=/tmp` (set in `Dockerfile.base`).

---

## Web service

### Request flow

Since the [service split](#the-service-split-and-building-it-with-agents), `/calculate`
returns immediately and the calculation happens in a separate Kubernetes Job; see
[`SERVICE_SPLIT.md`](SERVICE_SPLIT.md) for the authoritative state machine and API
contract. Browser-facing routes (`app.py`):

```
POST /calculate
  └─ canonicalize SMILES (RDKit)
  └─ create_pending_job() — row status=PENDING
  └─ create_compute_job() — spawns the 14-CPU k8s Job (no-op off-cluster, see README)
  └─ redirect to /pending/<uuid>

GET /pending/<uuid>
  └─ renders a polling page that hits GET /api/jobs/<uuid>/status

GET /stats/<uuid>
  └─ load all DONE/SEEN rows for same canonical SMILES
  └─ compute mean ± std if >1 run
  └─ render stats.html (table of all runs, current highlighted)

GET /result/<uuid>
  └─ load row from DB → render result.html (energies, 3D viewer, XYZ download)
```

The compute worker (`compute_runner.py`) never talks to the browser — it POSTs to
`/api/jobs/<uuid>/{start,result,error}` and exits.

### Database schema (`jobs` table)

| column | type | notes |
|--------|------|-------|
| `uuid` | TEXT PK | UUID4, used in URL |
| `smiles_input` | TEXT | as typed by user |
| `smiles_canonical` | TEXT | RDKit canonical — non-unique (multiple runs allowed) |
| `created_at` | TEXT | ISO 8601 UTC |
| `status` | INTEGER | -1=ERROR, 0=PENDING, 1=PROCESSING, 2=DONE, 3=SEEN |
| `lambda_plus_eV` | REAL | |
| `lambda_minus_eV` | REAL | |
| `partial_json` | TEXT | JSON blob of all 11 partial energies |
| `xyz_neutral/cation/anion` | TEXT | XYZ strings |
| `error_message` | TEXT | NULL if DONE |
| `email` | TEXT | reserved for async notification |

Every submission creates a new row. The stats page shows all DONE/SEEN rows for the same canonical SMILES with mean ± std.

### Exporting results for ML use

```bash
kubectl cp krupicka-ns/<pod-name>:/app/data/lambda.db ./lambda.db

sqlite3 -csv -header lambda.db \
  "SELECT uuid, smiles_canonical, lambda_plus_eV, lambda_minus_eV, partial_json \
   FROM jobs WHERE status >= 2;" \
  > lambda_results.csv
```

---

## Kubernetes deployment

[`README.md`](README.md#kubernetes-deployment-cerit-sc) is the step-by-step operational
reference (bringing an instance up, the required env vars, releasing, rolling back, drift
traps); [`SERVICE_SPLIT.md`](SERVICE_SPLIT.md) is the architecture and manifest-layout
reference. The short version: `k8s/base/` holds the six instance-independent manifests
(Deployment, Service, Ingress, PVC, RBAC, callback Secret), `k8s/overlays/{prod,test}` apply
via `kubectl apply -k` (kustomize, built into `kubectl`), and three GitHub Actions workflows
(`ci.yml`, `docker-build.yml`, `base-image.yml`) build and — for a tag push or manual
dispatch only — partially roll out the image; a manifest change always needs a manual
`kubectl apply -k`. See README for why that split between "image rollout" and "manifest
apply" exists and what breaks if you forget it.

---

## File structure

```
lambda-xtb/
├── lambda_xtb.py              core science: geometry pipeline, calculate_lambda
├── app.py                     Flask frontend: UI routes + internal /api/jobs/* callbacks
├── compute_runner.py          worker entrypoint — no server; POST start/result/error, exit
├── jobs.py                    builds & submits the compute Job spec; dead-worker reconcile
├── db.py                      SQLite database abstraction layer (JobStatus state machine)
├── templates/
│   ├── index.html             SMILES (+ optional email) input form + method details
│   ├── pending.html           polling page while a Job runs
│   ├── stats.html             all runs for a molecule: table + mean±std summary
│   └── result.html            results display + 3D viewer (py3Dmol via CDN)
├── tests/                     pytest — mocked infra; test_smoke_xtb.py is the one real run
├── environment.yml            conda env — the deployment artifact
├── Dockerfile                 app image: FROM base + COPY source (~20s build)
├── Dockerfile.base            base image: OS patches + conda env (~3min build)
├── k8s/
│   ├── base/                  instance-independent manifests (deployment, service,
│   │                          ingress + oauth ingress/service, pvc, rbac, secret)
│   └── overlays/
│       ├── prod/               names identical to what is live — an apply adopts it
│       └── test/               nameSuffix -test, own host/PVC/Job-size overrides
├── .github/workflows/
│   ├── ci.yml                 lint + mocked tests + one real xtb calc; gates every build
│   ├── docker-build.yml       app image: tag → release+rollout; main → build only;
│   │                          dispatch → build + roll out the test instance
│   └── base-image.yml         base image on env/Dockerfile.base change or dispatch
├── SERVICE_SPLIT.md           architecture + authoritative API contract & job states
├── AGENTS.md                  rules coding agents implemented the split under
└── DEVLOG.md                  this file
```

---

## Demo molecules (for testing)

| Name | SMILES | Expected λ⁺ (approx) |
|------|--------|----------------------|
| Naphthalene | `c1ccc2ccccc2c1` | ~190 meV |
| Anthracene | `c1ccc2cc3ccccc3cc2c1` | ~130 meV |
| Pentacene | `c1ccc2cc3cc4cc5ccccc5cc4cc3cc2c1` | ~95 meV |
| TPD | `CN(c1ccc(-c2ccc(N(C)c3ccccc3)cc2)cc1)c1ccccc1` | ~290 meV |

Expected trend: λ decreases as acene length increases (charge more delocalized).
If your numbers violate this trend, something is wrong.

---

## Docker image CVE status (as of 2026-03-14)

`continuumio/miniconda3:latest` is based on **Debian 13 (Trixie)**.

| Package | CVE | Status | Action |
|---------|-----|--------|--------|
| `python3.13`, `libpython3.13-*` | CVE-2025-13836, -15366, -15367, -8194, CVE-2026-1299 | **Eliminated** | Purged (unused — conda Python 3.11) |
| `openssh-client` | CVE-2026-3497 | **Eliminated** | Purged (not needed at runtime) |
| `PyJWT` | CVE-2026-32597 | **Eliminated** | Upgraded to ≥2.12.0 via pip |
| `libc6`, `libc-bin` | CVE-2026-0861, CVE-2026-0915 | No upstream fix | Wait for Debian patch |
| `libexpat1` | CVE-2026-25210 | No upstream fix | Wait for Debian patch |
| `libtasn1-6` | CVE-2025-13151 | No upstream fix | Wait for Debian patch |
| `libsqlite3-0` | CVE-2025-7709 | No upstream fix | Wait for Debian patch |

**Medium-priority**: evaluate `mambaorg/micromamba` as base — leaner image, smaller CVE surface.

---

## Open questions / next steps

- [ ] **Verify e-infra SSO on the live cluster** *(most urgent — see README)* — the ingress
  gating was merged intending to admit any authenticated e-infra identity, but that
  specific behavior was never confirmed against the real oauth2-proxy, and the release
  that would ship it (`v1.3`) was never tagged. Confirm both before relying on it for an
  outside audience.
- [ ] **Prod's rolling-update deadlock risk** — 1 replica + a ReadWriteOnce PVC + the
  default `RollingUpdate` strategy can hang if the new pod schedules onto a different node
  than the old one (the new pod waits for a volume the old one still holds). The test
  overlay already patches in `strategy: Recreate`; prod hasn't been given the same fix yet.
- [ ] **CREST `--squick` validation** — compare λ results for naphthalene/anthracene/TPD across 3+ runs; confirm reproducibility vs `--mquick`
- [ ] **CVE table refresh** — the table below is a point-in-time snapshot from 2026-03-14; re-check before treating it as current.
- [ ] **`mambaorg/micromamba` base image** — evaluate for smaller CVE surface
- [ ] Test `environment.yml` reproducibility on MetaCentrum JupyterHub
- [ ] **SQLite → Postgres** — `db.py`'s `Database` ABC already anticipates this; only needed if the frontend ever goes multi-replica.
- [ ] **True scale-to-zero** — CERIT-SC has no Knative/KEDA today, which is why the frontend is "minimal always-on" rather than actually zero; revisit if idle cost ever matters.
