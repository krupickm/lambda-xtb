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

## CI / Docker build (GitHub Actions)

This repo includes a GitHub Actions workflow that builds the Docker image and pushes it to **cerit.io**.

### Required secrets (set these in your repository settings)

- `HARBOR_USERNAME` — your Harbor login name
- `HARBOR_PASSWORD` — your Harbor password

The image is pushed as:

- `cerit.io/krupickm/lambda-xtb:latest`
- `cerit.io/krupickm/lambda-xtb:<commit_sha>`
