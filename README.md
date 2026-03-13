# λ-xTB Reorganization Energy Calculator

This repository contains a small Flask web service that computes intramolecular reorganization energy (λ) using the Nelsen four-point method with **GFN2-xTB**.

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

## Kubernetes deployment (CERIT-SC)

### Manual rollout (from MetaCentrum login node)

```bash
module add kubectl
export KUBECONFIG=../kuba-cluster.yaml   # path to your cluster credentials
kubectl rollout restart deployment/lambda-xtb -n krupicka-ns
kubectl rollout status deployment/lambda-xtb -n krupicka-ns
```

First-time apply of all manifests:

```bash
kubectl apply -f k8s/ -n krupicka-ns
kubectl get pods    -n krupicka-ns
kubectl get ingress -n krupicka-ns
kubectl logs -l app=lambda-xtb -n krupicka-ns --follow
```

## CI / Docker build + auto-deploy (GitHub Actions)

This repo includes a GitHub Actions workflow that builds the Docker image, pushes it to **cerit.io**, and then triggers a rolling restart of the Kubernetes deployment automatically.

### Required secrets (set these in your repository settings)

| Secret | Value |
|--------|-------|
| `HARBOR_USERNAME` | Your Harbor login name |
| `HARBOR_PASSWORD` | Your Harbor password |
| `KUBECONFIG_DATA` | Full contents of `kuba-cluster.yaml` (base64 or raw YAML) |

To add the kubeconfig secret:

```bash
# Copy the file contents and paste into the GitHub secret field
cat ../kuba-cluster.yaml
```

The workflow will skip the rollout step gracefully if `KUBECONFIG_DATA` is not set.

The image is pushed as:

- `cerit.io/krupickm/lambda-xtb:latest`
- `cerit.io/krupickm/lambda-xtb:<commit_sha>`
