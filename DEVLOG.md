# λ-xTB: Reorganization Energy Calculator — Project Devlog

> **Status**: Working CLI script → next: Flask web service → Kubernetes on CERIT-SC  
> **Location**: VS Code Remote → MetaCentrum (brno2)  
> **Repo**: `WORK/2026-tobrman-polovodic/lambda-xtb/`

---

## What this project does

Calculates **intramolecular reorganization energy (λ)** for organic π-conjugated molecules
(OLED/OSC materials) using the **Nelsen four-point method** at GFN2-xTB level.

Takes a SMILES string → returns λ⁺ (hole transport) and λ⁻ (electron transport) in eV/meV.

**Use case**: Colleague uploads a structure, gets λ⁺ and λ₋ back without touching the CLI.

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
conda install -c conda-forge xtb-python ase rdkit numpy
conda install -c conda-forge jupyterlab ipykernel flask
python -m ipykernel install --user --name xtb-lambda --display-name "Python (xtb-lambda)"
```

### Export for reproducibility

```bash
conda env export -n xtb-lambda > environment.yml
```

Use `conda env create -f environment.yml` to recreate on MetaCentrum JupyterHub.

### Verify install

```bash
python -c "from xtb.interface import Calculator; print('xtb OK')"
python -c "from xtb.ase.calculator import XTB; print('ASE bridge OK')"
python -c "from rdkit import Chem; print('RDKit OK')"
```

---

## Bugs fixed during development

### Bug 1 — ASE `LBFGS.converged()` signature change (ASE ≥ 3.23)

**Symptom**: `TypeError: Optimizer.converged() missing 1 required positional argument: 'gradient'`

**Cause**: ASE refactored `converged()` to require explicit forces argument.

**Fix**: Use the return value of `opt.run()` instead of calling `opt.converged()`:
```python
# BROKEN
opt.run(fmax=fmax, steps=max_steps)
if not opt.converged(): ...

# FIXED
converged = opt.run(fmax=fmax, steps=max_steps)
if not converged: ...
```

### Bug 2 — `deepcopy` fails on CFFI handle

**Symptom**: `TypeError: cannot pickle '_cffi_backend.__CDataGCP' object`

**Cause**: After optimization, the Atoms object holds a live CFFI pointer to the Fortran
xTB library. `copy.deepcopy()` cannot serialize it.

**Fix**: Use `ASE Atoms.copy()` instead — clones geometry only, drops calculator:
```python
# BROKEN
atoms = copy.deepcopy(atoms_in)

# FIXED
atoms = atoms_in.copy()   # geometry only, no calculator
```
Remove `import copy` entirely.

### Bug 3 — Charge/uhf silently ignored by `xtb.ase.calculator.XTB`

**Symptom**: All three optimizations (neutral, cation, anion) produce identical energies
and geometries. Single-points all return the same value. λ = 0.

**Cause**: Known bug in `xtb.ase.calculator.XTB` — the `charge=` and `uhf=` constructor
arguments are silently dropped. The calculator reads charge from
`atoms.get_initial_charges().sum()` which is 0.0 on any fresh Atoms object.
Reference: https://github.com/grimme-lab/xtb-python/issues/58

**Fix**: Set charge and uhf on the Atoms object before attaching the calculator:
```python
def make_xtb_calc(atoms, charge: int, uhf: int):
    import numpy as np
    from xtb.ase.calculator import XTB
    n = len(atoms)
    atoms.set_initial_charges(np.full(n, charge / n))
    atoms.set_initial_magnetic_moments(np.full(n, uhf / n))
    atoms.calc = XTB(method="GFN2-xTB")

# usage: modifies atoms in-place
atoms = atoms_in.copy()
make_xtb_calc(atoms, charge=+1, uhf=1)
```

**Alternative** (more robust): bypass the ASE wrapper entirely and use
`xtb.interface.Calculator` directly. The native interface takes `charge` and `uhf`
as proper constructor arguments that actually work.

---

## Architecture decisions

### Deployment path (phased)

```
Phase 1 [done]     CLI script — lambda.py, SMILES via argv
Phase 2 [next]     Flask web service — SMILES in, JSON/HTML out
Phase 3            Docker image → push to hub.cerit.io
Phase 4            Kubernetes Deployment + Service + Ingress on CERIT-SC
```

### Why Flask (not Jupyter/Voilà for the web service)

- Flask is a minimal Python web framework: one file, no JS build step
- Colleague gets a real URL, not a notebook interface
- Clean separation: `lambda_xtb.py` is pure science, `app.py` is pure web plumbing
- Easy to containerize: `FROM continuumio/miniconda3` + conda env + `CMD flask run`

### Why not Galaxy / JupyterHub for the web service

- Galaxy: massive overkill, wrong abstraction for a single-function tool
- JupyterHub: requires MetaCentrum account for each user, exposes notebook UI
- Flask: zero friction for the end user, just a browser

---

## Web service design (Phase 2)

### Stack

```
browser  ←→  Flask (app.py)  ←→  lambda_xtb.py  ←→  xTB/ASE
```

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | HTML form page |
| POST | `/calculate` | Accepts SMILES, runs calculation, returns result page |
| GET | `/result/<job_id>` | (future) async result polling |

### Simplest synchronous version (Phase 2a)

- POST /calculate with SMILES string
- Block until calculation finishes (~10–40s)
- Return rendered HTML with λ⁺, λ⁻, partial energies table, 3D geometry viewer
- No queue, no database, no auth — single user at a time is fine for now

### Async upgrade path (Phase 2b, if needed)

- POST submits job → returns job_id immediately
- GET /result/<job_id> polls for completion
- Use Python `threading` or `subprocess` — no Celery/Redis needed at this scale

---

## Kubernetes deployment (Phase 3/4)

### Concepts needed

| K8s object | Purpose |
|------------|---------|
| **Image** | Docker image with conda env + app — pushed to `hub.cerit.io` (CERIT-SC Harbor) |
| **Deployment** | Runs 1 replica of the container, auto-restarts on crash |
| **Service** | Stable internal network address for the container |
| **Ingress** | Exposes Service at `lambda-calc.dyn.cloud.e-infra.cz` |
| **PVC** | Not needed for stateless calculator |

### MetaCentrum infrastructure

- **JupyterHub**: `hub.cloud.e-infra.cz` — for interactive development only
- **Harbor registry**: `hub.cerit.io` — push Docker images here
- **Kubernetes**: CERIT-SC managed — deploy via Rancher or `kubectl`
- **Ingress domain**: `*.dyn.cloud.e-infra.cz` — auto-provisioned hostnames
- **Auth**: MetaCentrum credentials work for JupyterHub; K8s apps can be public

### CERIT-SC Kubernetes security requirements (must follow)

CERIT-SC enforces the **restricted PodSecurity standard**, which means the pod must run as a non-root user.
If the container attempts to start as root, it will fail with `CreateContainerConfigError` or `runAsNonRoot` errors.

- **Docker image must switch to a non-root UID** (recommended: `USER 1000`)
- **Deployment must include `securityContext`** (pod + container) to enforce non-root and drop capabilities

Required security block (copy into Deployment manifest):

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
        drop:
          - ALL
```

### Docker image strategy

We build a small, reproducible image from `environment.yml` and pinned dependencies.

```dockerfile
FROM continuumio/miniconda3

WORKDIR /app

# Install the environment
COPY environment.yml ./
RUN conda env create -f environment.yml && \
    conda clean -afy

# Copy source
COPY . .

# Ensure conda env is on PATH (so python/flask just work)
ENV PATH=/opt/conda/envs/xtb-lambda/bin:${PATH}
ENV CONDA_DEFAULT_ENV=xtb-lambda

# Run as non-root (required by CERIT-SC)
RUN chown -R 1000 /opt/conda /app
USER 1000

EXPOSE 5000
CMD ["flask", "run", "--host=0.0.0.0"]
```

### Kubernetes manifests (recommended)

Create a `k8s/` directory and add these three files.

**`k8s/deployment.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: lambda-xtb
spec:
  replicas: 1
  selector:
    matchLabels:
      app: lambda-xtb
  template:
    metadata:
      labels:
        app: lambda-xtb
    spec:
      securityContext:
        runAsNonRoot: true
        fsGroupChangePolicy: OnRootMismatch
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: lambda-xtb
          image: cerit.io/krupickm/lambda-xtb:latest
          imagePullPolicy: Always
          ports:
            - containerPort: 5000
          securityContext:
            runAsUser: 1000
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
          resources:
            requests:
              cpu: "1"
              memory: "2Gi"
            limits:
              cpu: "4"
              memory: "8Gi"
```

**`k8s/service.yaml`**

```yaml
apiVersion: v1
kind: Service
metadata:
  name: lambda-xtb-svc
spec:
  type: ClusterIP
  ports:
    - name: http
      port: 80
      targetPort: 5000
  selector:
    app: lambda-xtb
```

**`k8s/ingress.yaml`**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: lambda-xtb-ingress
  annotations:
    kubernetes.io/tls-acme: "true"
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
spec:
  ingressClassName: nginx
  tls:
    - hosts:
        - "lambda-xtb.dyn.cloud.e-infra.cz"
      secretName: lambda-xtb-dyn-cloud-e-infra-cz-tls
  rules:
    - host: "lambda-xtb.dyn.cloud.e-infra.cz"
      http:
        paths:
          - path: /
            pathType: ImplementationSpecific
            backend:
              service:
                name: lambda-xtb-svc
                port:
                  number: 80
```

> 🔎 Use a unique hostname (e.g., `lambda-xtb-<user>.dyn.cloud.e-infra.cz`) to avoid collisions.

### Build / push workflow

```bash
# Build locally
docker build -t cerit.io/krupickm/lambda-xtb:latest .

# Push to CERIT Harbor
docker push cerit.io/krupickm/lambda-xtb:latest
```

### Deploy / update (kubectl)

From MetaCentrum login node (`perian`):

```bash
module add kubectl
export KUBECONFIG=../kuba-cluster.yaml

# First-time apply of all manifests
kubectl apply -f k8s/ -n krupicka-ns
kubectl get pods    -n krupicka-ns
kubectl get ingress -n krupicka-ns
kubectl logs -l app=lambda-xtb -n krupicka-ns --follow

# Redeploy after image update (rolling restart)
kubectl rollout restart deployment/lambda-xtb -n krupicka-ns
kubectl rollout status  deployment/lambda-xtb -n krupicka-ns
```

### CI auto-rollout

The GitHub Actions workflow (`.github/workflows/docker-build.yml`) runs the rollout automatically on every push to `main` — after the image is built and pushed. This requires the `KUBECONFIG_DATA` secret to be set in the repository settings (paste the contents of `kuba-cluster.yaml`). If the secret is absent the step is skipped without failing the build.

### Local testing (fast iteration)

```bash
docker build -t lambda-xtb-local .
docker run --rm -p 5000:5000 --user 1000 lambda-xtb-local
```

---

## File structure (target)

```
lambda-xtb/
├── lambda_xtb.py          core science: smiles_to_atoms, optimize, singlepoint,
│                          calculate_lambda, save_results
├── app.py                 Flask web service
├── templates/
│   ├── index.html         SMILES input form
│   └── result.html        results display + 3D viewer (py3Dmol via CDN)
├── environment.yml        conda env — the deployment artifact
├── Dockerfile             for K8s deployment
├── k8s/                   Kubernetes manifests
└── DEVLOG.md              this file
```

---

## Running locally

```bash
conda activate xtb-lambda

# CLI mode
python lambda_xtb.py "c1ccc2ccccc2c1"          # naphthalene
python lambda_xtb.py "c1ccc2cc3ccccc3cc2c1"    # anthracene

# Web service (once app.py exists)
flask --app app run --debug
# open http://localhost:5000
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

## Open questions / next steps

- [ ] Write `app.py` — synchronous Flask service
- [ ] Add 3D geometry visualization (py3Dmol via CDN, no install needed)
- [ ] Test environment.yml reproducibility on MetaCentrum JupyterHub
- [ ] Write Dockerfile
- [ ] Get CERIT-SC Harbor access and push first image
- [ ] Write minimal K8s manifest (Deployment + Service + Ingress)
- [ ] Decide: public URL or MetaCentrum-login-required?
