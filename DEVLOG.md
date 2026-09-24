# λ-xTB: Reorganization Energy Calculator — Project Devlog

> **Status**: v1.1 live at `lambda-xtb.dyn.cloud.e-infra.cz` — parallel xtb execution, multi-run statistics, shareable URLs, CI/CD on CERIT-SC k8s.
> **Location**: VS Code Remote → MetaCentrum (brno2)
> **Repo**: `WORK/2026-tobrman-polovodic/lambda-xtb/`

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
Phase 7 [TODO]     Async job queue — decouple HTTP request from calculation; email result link
```

### Synchronous execution — current limitation

The Flask route blocks for the full calculation (~30–90s). This means:
- **One calculation at a time** — concurrent requests queue behind each other
- **No progress feedback** — browser just waits
- **Timeout risk** — slow molecules or a busy server may hit proxy timeouts

The right fix is an async job queue (Celery + Redis, or a simple thread pool with a DB-polled status endpoint). Until then, the UI warns users not to resubmit, and results are always stored by UUID so reloading is safe.

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

```
POST /calculate
  └─ canonicalize SMILES (RDKit)
  └─ always run calculate_lambda() — every submission is stored
  └─ store_job() / store_error()
  └─ redirect to /stats/<uuid>

GET /stats/<uuid>
  └─ load all DONE/SEEN rows for same canonical SMILES
  └─ compute mean ± std if >1 run
  └─ render stats.html (table of all runs, current highlighted)

GET /result/<uuid>
  └─ load row from DB → render result.html (energies, 3D viewer, XYZ download)
```

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

### Manifests (`k8s/`)

`k8s/base/` + `k8s/overlays/{prod,test}`, applied with `kubectl apply -k` — two
instances (prod and test) share one image and differ only by overlay. See
SERVICE_SPLIT.md, "Instances".

| File (`k8s/base/`) | Purpose |
|------|---------|
| `deployment.yaml` | 1 replica, PVC mount, env vars |
| `service.yaml` | ClusterIP on port 80 → 5000 |
| `ingress.yaml` | TLS via cert-manager, `lambda-xtb.dyn.cloud.e-infra.cz` |
| `pvc.yaml` | 1Gi ReadWriteOnce for SQLite DB at `/app/data` |
| `rbac.yaml` | SA + Role/RoleBinding letting the frontend manage compute Jobs |
| `secret.yaml` | callback-token Secret — name only, value created out-of-band |

Key env vars in the pod:

| Var | Value | Why |
|-----|-------|-----|
| `OMP_NUM_THREADS` | `4` | CREST uses this; xtb overrides to 1 per thread in Python |
| `XDG_DATA_HOME` | `/tmp` | easyxtb writes temp files here |
| `DATABASE_URL` | `sqlite:///data/lambda.db` | Points to PVC mount |

### CI/CD (`.github/workflows/`)

| Workflow | Triggers | What it does |
|----------|----------|--------------|
| `docker-build.yml` | every push to `main` | builds app image from base, pushes `:latest` + `:<sha>`, pins deployment to `:<sha>` via `kubectl set image` |
| `base-image.yml` | `environment.yml` or `Dockerfile.base` change, or manual dispatch | rebuilds and pushes `lambda-xtb-base:latest` |

Rollout uses `kubectl set image ... :<sha>` (not `rollout restart`) to avoid the Harbor propagation race where `:latest` may not yet be available when k8s pulls.

### Useful commands

```bash
# Logs
kubectl logs -l app=lambda-xtb -n krupicka-ns --follow

# Copy DB out for inspection
kubectl cp krupicka-ns/<pod-name>:/app/data/lambda.db ./lambda.db

# Manual rollout (if CI failed)
kubectl rollout restart deployment/lambda-xtb -n krupicka-ns
kubectl rollout status  deployment/lambda-xtb -n krupicka-ns
```

### Local testing

```bash
# Must build base first (once)
docker build -f Dockerfile.base -t cerit.io/krupickm/lambda-xtb-base:latest .
docker build -t lambda-xtb-local .
docker run --rm -p 5000:5000 lambda-xtb-local
```

---

## File structure

```
lambda-xtb/
├── lambda_xtb.py              core science: geometry pipeline, calculate_lambda
├── app.py                     Flask web service
├── db.py                      SQLite database abstraction layer
├── templates/
│   ├── index.html             SMILES input form + method details
│   ├── stats.html             all runs for a molecule: table + mean±std summary
│   └── result.html            results display + 3D viewer (py3Dmol via CDN)
├── environment.yml            conda env — the deployment artifact
├── Dockerfile                 app image: FROM base + COPY source (~20s build)
├── Dockerfile.base            base image: OS patches + conda env (~3min build)
├── k8s/
│   ├── base/                  instance-independent manifests
│   └── overlays/
│       ├── prod/              names identical to what is live
│       └── test/              nameSuffix -test, own host/PVC/Job size
├── .github/workflows/
│   ├── docker-build.yml       CI: app image on every push
│   └── base-image.yml         CI: base image on env/Dockerfile.base change
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

- [ ] **Async job queue** *(biggest open item)* — decouple the HTTP request from the ~30–90s calculation. Options: Celery + Redis sidecar; or a simple in-process thread pool with DB-polled `/status/<uuid>` endpoint. Goal: browser returns immediately with a "pending" page, user gets a shareable link by email when done. The `email` column in the DB is already reserved for this.
- [ ] **CREST `--squick` validation** — compare λ results for naphthalene/anthracene/TPD across 3+ runs; confirm reproducibility vs `--mquick`
- [ ] **`mambaorg/micromamba` base image** — evaluate for smaller CVE surface
- [ ] Test `environment.yml` reproducibility on MetaCentrum JupyterHub
- [ ] Decide: public URL or MetaCentrum-login-required?
