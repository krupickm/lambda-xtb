# λ-xTB Reorganization Energy Calculator

Flask web service that computes intramolecular reorganization energy (λ⁺/λ⁻) using the **Nelsen four-point method** at GFN2-xTB level. Takes a SMILES string, runs a CREST conformer search + 3 optimizations + 4 single-points, and returns λ⁺ (hole) and λ⁻ (electron) in meV. Every run is stored; the stats page shows mean ± std across repeated submissions.

Live at: `https://lambda-xtb.dyn.cloud.e-infra.cz`

## Development

### 1) Create / activate the conda environment

```bash
conda env create -f environment.yml
conda activate xtb-lambda
```

### 2) Run locally

```bash
export FLASK_APP=app.py
flask run --host=0.0.0.0
```

Then open: http://localhost:5000

### 3) Build and run with Docker

```bash
# Build base image once (heavy conda layer, ~3 min)
docker build -f Dockerfile.base -t cerit.io/krupickm/lambda-xtb-base:latest .

# Build app image (fast, ~20s)
docker build -t lambda-xtb-local .

docker run --rm -p 5000:5000 lambda-xtb-local
```

## Kubernetes deployment (CERIT-SC)

### First-time setup

```bash
module add kubectl
export KUBECONFIG=../kuba-cluster.yaml
kubectl apply -k k8s/overlays/prod   # or overlays/test for the test instance
kubectl get pods    -n krupicka-ns
kubectl get ingress -n krupicka-ns
```

To watch a calculation run end to end (frontend → compute Job → callbacks), see
[docs/OBSERVING_A_RUN.md](docs/OBSERVING_A_RUN.md).

### Useful commands

```bash
# Logs
kubectl logs -l app=lambda-xtb -n krupicka-ns --follow

# Manual rollout (if CI failed)
kubectl rollout restart deployment/lambda-xtb -n krupicka-ns
kubectl rollout status  deployment/lambda-xtb -n krupicka-ns

# Copy SQLite DB out for inspection / export
kubectl cp krupicka-ns/<pod-name>:/app/data/lambda.db ./lambda.db
```

## CI/CD (GitHub Actions)

Three workflows in `.github/workflows/`:

| Workflow | Triggers | What it does |
|----------|----------|--------------|
| `ci.yml` | every pull request; called before every build | `ruff check`, unit tests (xtb mocked), one real xtb calculation on ethylene |
| `docker-build.yml` | push to `main`; push of a `v*` tag; manual dispatch | **tag** → builds `:<tag>` + `:latest` and rolls out prod; **main** → builds `:main` + `:<sha>`, deploys nothing; **dispatch** → builds `:<sha>` and rolls out the test instance |
| `base-image.yml` | `environment.yml` or `Dockerfile.base` change, or manual dispatch | rebuilds `lambda-xtb-base:latest` |

Merging to `main` deploys nothing — prod moves only when you push a version
tag, and the manifests pin that exact tag rather than `:latest`. The full
branch → PR → release workflow, including how to cut a release and how to roll
one back, is in [`CONTRIBUTING.md`](CONTRIBUTING.md).

### Required secrets

| Secret | Value |
|--------|-------|
| `HARBOR_USERNAME` | Harbor login name |
| `HARBOR_PASSWORD` | Harbor password |
| `KUBECONFIG_DATA` | Full contents of `kuba-cluster.yaml` |

## See also

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branch/PR/release workflow, test markers, what CI runs.
- `DEVLOG.md` — architecture decisions, science background, known issues, open TODOs.
