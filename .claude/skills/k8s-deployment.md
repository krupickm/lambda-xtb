# Kubernetes Deployment & CI/CD Skill for λ-xTB Project

This skill provides comprehensive guidance for deploying Python/Flask applications to CERIT-SC Kubernetes clusters using GitHub Actions CI/CD pipelines.

---

## Table of Contents

1. [Project Overview](#project-overview)
2. [Prerequisites](#prerequisites)
3. [Docker Image Strategy](#docker-image-strategy)
4. [Kubernetes Manifests](#kubernetes-manifests)
5. [CI/CD with GitHub Actions](#cicd-with-github-actions)
6. [Deployment Workflow](#deployment-workflow)
7. [Troubleshooting](#troubleshooting)
8. [e-INFRA CZ / CERIT-SC Specifics](#e-infra-cz--cerit-sc-specifics)

---

## Project Overview

The λ-xTB project demonstrates a production-ready deployment pattern for compute-intensive scientific web applications:

- **Application**: Flask web service (Python 3.11)
- **Dependencies**: xtb, CREST, RDKit via conda-forge
- **Container Registry**: Harbor (cerit.io)
- **Orchestration**: Kubernetes on CERIT-SC cloud
- **CI/CD**: GitHub Actions
- **TLS**: cert-manager with Let's Encrypt

**Live deployment**: `https://lambda-xtb.dyn.cloud.e-infra.cz`

---

## Prerequisites

### Required Tools

```bash
# Docker (for local building/testing)
docker --version

# kubectl (for cluster interaction)
module add kubectl  # On MetaCentrum systems
kubectl version --client

# Git (for version control)
git --version
```

### Access Requirements

1. **CERIT-SC Cloud Account**: Register at https://cloud.e-infra.cz
2. **Harbor Registry Credentials**: For pushing images to cerit.io
3. **Kubeconfig**: Cluster access configuration file
4. **GitHub Repository**: With Actions enabled and secrets configured

### Namespace Setup

```bash
# Create namespace if not exists
kubectl create namespace krupicka-ns

# Verify access
kubectl get pods -n krupicka-ns
```

---

## Docker Image Strategy

### Two-Layer Build Pattern

For projects with heavy dependencies (conda environments), split the Dockerfile:

#### Dockerfile.base (Base Image)

```dockerfile
FROM continuumio/miniconda3:latest

# Security: Apply patches and remove unused packages with CVEs
RUN apt-get update && \
    apt-get upgrade -y && \
    apt-get purge -y --auto-remove \
        python3.13 python3.13-minimal \
        libpython3.13-minimal libpython3.13-stdlib \
        openssh-client && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install conda environment (slow step ~2-3 min)
COPY environment.yml ./
RUN conda env create -f environment.yml && \
    conda clean -afy && \
    /opt/conda/envs/<env-name>/bin/pip install --no-cache-dir "PyJWT>=2.12.0"

# Set conda as default Python
ENV PATH=/opt/conda/envs/<env-name>/bin:${PATH}
ENV CONDA_DEFAULT_ENV=<env-name>

# Config for temp files in k8s (tmpfs mount)
ENV XDG_DATA_HOME=/tmp
```

#### Dockerfile (App Image)

```dockerfile
FROM cerit.io/youruser/app-base:latest

WORKDIR /app
COPY . .

# Version baked at build time
ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}

ENV FLASK_APP=app.py
ENV FLASK_RUN_HOST=0.0.0.0

# Run as non-root (matches k8s runAsUser: 1000)
USER 1000

EXPOSE 5000
CMD ["flask", "run"]
```

### Build Commands

```bash
# Build base image (only when environment.yml changes)
docker build -f Dockerfile.base -t cerit.io/youruser/app-base:latest .

# Build app image (fast, ~20s)
docker build --build-arg BUILD_VERSION=1.0 -t cerit.io/youruser/app:latest .
```

### Multi-Architecture Support

```bash
# Setup Buildx
docker buildx create --use

# Build for multiple platforms
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t cerit.io/youruser/app:latest \
  --push \
  .
```

---

## Kubernetes Manifests

### Deployment (`k8s/deployment.yaml`)

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
      # Pod-level security context (CERIT-SC restricted PSS)
      securityContext:
        runAsNonRoot: true
        fsGroupChangePolicy: OnRootMismatch
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: lambda-xtb
          image: cerit.io/youruser/app:latest
          imagePullPolicy: Always
          ports:
            - containerPort: 5000
          securityContext:
            runAsUser: 1000
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
          env:
            - name: DATABASE_URL
              value: "sqlite:///data/lambda.db"
            - name: OMP_NUM_THREADS
              value: "4"
            - name: XDG_DATA_HOME
              value: "/tmp"
          volumeMounts:
            - name: data
              mountPath: /app/data
          resources:
            requests:
              cpu: "4"
              memory: "2Gi"
            limits:
              cpu: "4"
              memory: "8Gi"
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: lambda-xtb-data
```

**Key Configuration:**

| Setting | Value | Reason |
|---------|-------|--------|
| `runAsNonRoot` | `true` | CERIT-SC security requirement |
| `runAsUser` | `1000` | Non-root user ID |
| `fsGroupChangePolicy` | `OnRootMismatch` | Only fix permissions if needed |
| `seccompProfile` | `RuntimeDefault` | Container syscall filtering |
| `allowPrivilegeEscalation` | `false` | Security hardening |
| `capabilities.drop` | `[ALL]` | Minimal privilege |

### Service (`k8s/service.yaml`)

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

### Ingress (`k8s/ingress.yaml`)

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
        - "your-app.dyn.cloud.e-infra.cz"
      secretName: your-app-dyn-cloud-e-infra-cz-tls
  rules:
    - host: "your-app.dyn.cloud.e-infra.cz"
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

### PersistentVolumeClaim (`k8s/pvc.yaml`)

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: lambda-xtb-data
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi
```

---

## CI/CD with GitHub Actions

### Workflow Structure

Create `.github/workflows/` directory with separate workflows for base and app images.

#### App Image Workflow (`.github/workflows/docker-build.yml`)

```yaml
name: Build & Push Docker Image

on:
  push:
    branches:
      - main
  workflow_dispatch: {}

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

jobs:
  build-and-push:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to Cerit Harbor
        uses: docker/login-action@v3
        with:
          registry: cerit.io
          username: ${{ secrets.HARBOR_USERNAME }}
          password: ${{ secrets.HARBOR_PASSWORD }}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            cerit.io/youruser/app:latest
            cerit.io/youruser/app:${{ github.sha }}
          platforms: linux/amd64
          build-args: |
            BUILD_VERSION=${{ github.run_number }}

      - name: Rollout restart on Kubernetes
        env:
          KUBECONFIG_DATA: ${{ secrets.KUBECONFIG_DATA }}
        run: |
          if [ -z "$KUBECONFIG_DATA" ]; then
            echo "KUBECONFIG_DATA secret not set — skipping rollout restart"
            exit 0
          fi
          mkdir -p ~/.kube
          echo "$KUBECONFIG_DATA" > ~/.kube/config
          chmod 600 ~/.kube/config
          kubectl set image deployment/lambda-xtb \
            lambda-xtb=cerit.io/youruser/app:${{ github.sha }} \
            -n your-namespace
          kubectl rollout status deployment/lambda-xtb -n your-namespace --timeout=300s
```

#### Base Image Workflow (`.github/workflows/base-image.yml`)

```yaml
name: Build & Push Base Image

on:
  push:
    branches:
      - main
    paths:
      - environment.yml
      - Dockerfile.base
  workflow_dispatch: {}

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

jobs:
  build-base:
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - name: Set up QEMU
        uses: docker/setup-qemu-action@v3

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to Cerit Harbor
        uses: docker/login-action@v3
        with:
          registry: cerit.io
          username: ${{ secrets.HARBOR_USERNAME }}
          password: ${{ secrets.HARBOR_PASSWORD }}

      - name: Build and push base image
        uses: docker/build-push-action@v6
        with:
          context: .
          file: Dockerfile.base
          push: true
          tags: cerit.io/youruser/app-base:latest
          platforms: linux/amd64
```

### Required GitHub Secrets

Configure these in **Repository Settings → Secrets and variables → Actions**:

| Secret Name | Value | Description |
|-------------|-------|-------------|
| `HARBOR_USERNAME` | Your Harbor login | Username for cerit.io registry |
| `HARBOR_PASSWORD` | Your Harbor password | Password/token for registry auth |
| `KUBECONFIG_DATA` | Full kubeconfig content | Content of your cluster config file |

### Getting KUBECONFIG_DATA

```bash
# On your local machine
cat ../kuba-cluster.yaml | pbcopy  # macOS
# or
cat ../kuba-cluster.yaml | xclip -selection clipboard  # Linux

# Then paste into GitHub Secrets
```

---

## Deployment Workflow

### First-Time Setup

```bash
# 1. Clone repository
git clone <repo-url>
cd lambda-xtb

# 2. Add kubectl module (MetaCentrum)
module add kubectl

# 3. Set KUBECONFIG
export KUBECONFIG=../kuba-cluster.yaml

# 4. Create namespace
kubectl create namespace krupicka-ns

# 5. Apply manifests
kubectl apply -f k8s/ -n krupicka-ns

# 6. Verify deployment
kubectl get pods -n krupicka-ns
kubectl get svc -n krupicka-ns
kubectl get ingress -n krupicka-ns
```

### Manual Deployment

```bash
# Build and push manually
docker build -f Dockerfile.base -t cerit.io/youruser/app-base:latest .
docker build --build-arg BUILD_VERSION=1.0 -t cerit.io/youruser/app:latest .
docker push cerit.io/youruser/app:latest

# Update deployment
kubectl set image deployment/lambda-xtb \
  lambda-xtb=cerit.io/youruser/app:latest \
  -n krupicka-ns

# Monitor rollout
kubectl rollout status deployment/lambda-xtb -n krupicka-ns
```

### Rolling Back

```bash
# Rollback to previous version
kubectl rollout undo deployment/lambda-xtb -n krupicka-ns

# Rollback to specific revision
kubectl rollout undo deployment/lambda-xtb --to-revision=2 -n krupicka-ns

# Check rollout history
kubectl rollout history deployment/lambda-xtb -n krupicka-ns
```

### Useful Commands

```bash
# View logs
kubectl logs -l app=lambda-xtb -n krupicka-ns --follow

# View logs from specific pod
kubectl logs pod/lambda-xtb-xxxxx -n krupicka-ns --follow

# Describe pod (for events/errors)
kubectl describe pod -l app=lambda-xtb -n krupicka-ns

# Exec into running container
kubectl exec -it -l app=lambda-xtb -n krupicka-ns -- /bin/bash

# Copy database out
kubectl cp krupicka-ns/<pod-name>:/app/data/lambda.db ./lambda.db

# Port-forward for debugging
kubectl port-forward -n krupicka-ns svc/lambda-xtb-svc 5000:80
```

---

## Troubleshooting

### Pod Stuck in Pending

```bash
# Check why pod is pending
kubectl describe pod -l app=lambda-xtb -n krupicka-ns

# Common causes:
# - Insufficient resources (reduce CPU/memory requests)
# - PVC not bound (check PVC status)
# - Node selector mismatch (verify node labels)
```

### Image Pull Errors

```bash
# Check image pull secret
kubectl get secret -n krupicka-ns

# Verify image exists
curl -u "username:password" https://cerit.io/api/v2/repository/youruser/app/tags

# Test locally
docker pull cerit.io/youruser/app:latest
```

### CrashLoopBackOff

```bash
# Check logs
kubectl logs -l app=lambda-xtb -n krupicka-ns --previous

# Check events
kubectl describe pod -l app=lambda-xtb -n krupicka-ns

# Common causes:
# - Missing environment variables
# - Database connection errors
# - Permission issues on mounted volumes
```

### Resource Issues

```bash
# Check resource usage
kubectl top pods -n krupicka-ns
kubectl top nodes

# If OOMKilled, increase memory limits
# Edit deployment:
kubectl edit deployment/lambda-xtb -n krupicka-ns
```

### TLS/Certificate Issues

```bash
# Check certificate status
kubectl get certificaterequest -n krupicka-ns
kubectl get certificate -n krupicka-ns

# Check ingress
kubectl describe ingress -n krupicka-ns

# Verify TLS secret
kubectl get secret lambda-xtb-dyn-cloud-e-infra-cz-tls -n krupicka-ns -o yaml
```

---

## e-INFRA CZ / CERIT-SC Specifics

### Dynamic DNS

CERIT-SC provides dynamic DNS at `dyn.cloud.e-infra.cz`:

- Format: `<app-name>.dyn.cloud.e-infra.cz`
- Automatic SSL via cert-manager
- No manual DNS configuration required

### Container Registry (Harbor)

- URL: `cerit.io`
- Namespace: `<your-username>`
- Authentication: Harbor credentials (separate from cloud UI)

### Namespace Quotas

Check namespace resource quotas:

```bash
kubectl get quota -n your-namespace
kubectl describe quota -n your-namespace
```

### Storage Classes

Available storage classes on CERIT-SC:

```bash
kubectl get sc
```

Common options:
- `standard` - Default SSD storage
- `fast` - High-performance NVMe
- `archive` - Cold storage for backups

### Pod Security Standards

CERIT-SC enforces **restricted** PSS by default:

- Must run as non-root
- No privilege escalation
- All capabilities dropped
- Seccomp profile required

If you need elevated privileges, request a dedicated namespace with relaxed policies.

### Network Policies

By default, pods can communicate within the cluster. For external access:

```bash
# Allow outbound HTTPS
kubectl apply -f - <<EOF
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-https
  namespace: your-namespace
spec:
  podSelector: {}
  policyTypes:
  - Egress
  egress:
  - to:
    - ipBlock:
        cidr: 0.0.0.0/0
    ports:
    - protocol: TCP
      port: 443
EOF
```

---

## Best Practices

### Security

1. **Never commit secrets** - Use GitHub Secrets or external secret managers
2. **Use immutable tags** - Pin deployments to SHA tags in production
3. **Scan images** - Run Trivy or Grype before pushing
4. **Least privilege** - Drop all capabilities, add only what's needed
5. **Read-only filesystems** - Where possible, use `readOnlyRootFilesystem: true`

### Reliability

1. **Health checks** - Add liveness/readiness probes
2. **Resource limits** - Always set requests and limits
3. **Pod disruption budgets** - For multi-replica deployments
4. **Horizontal Pod Autoscaler** - For variable workloads

### Observability

1. **Structured logging** - JSON format for log aggregation
2. **Metrics endpoint** - Prometheus-compatible metrics
3. **Distributed tracing** - OpenTelemetry integration

### CI/CD

1. **Separate base/app images** - Keep heavy dependencies stable
2. **SHA pinning** - Deploy specific image digests
3. **Rollback automation** - Auto-rollback on health check failures
4. **Canary deployments** - Gradual rollout for critical apps

---

## Quick Reference

### Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `DATABASE_URL` | Database connection | `sqlite:///data/lambda.db` |
| `OMP_NUM_THREADS` | OpenMP thread count | `4` |
| `XDG_DATA_HOME` | Temp file location | `/tmp` |
| `FLASK_APP` | Flask entry point | `app.py` |
| `FLASK_RUN_HOST` | Bind address | `0.0.0.0` |
| `FLASK_RUN_PORT` | Bind port | `5000` |

### Common kubectl Commands

```bash
# Get all resources
kubectl get all -n your-namespace

# Watch resources
kubectl get pods -n your-namespace -w

# Delete and recreate
kubectl delete -f k8s/ -n your-namespace
kubectl apply -f k8s/ -n your-namespace

# Scale deployment
kubectl scale deployment/lambda-xtb --replicas=3 -n your-namespace
```

### Docker Commands

```bash
# List images
docker images

# Remove old images
docker image prune -a

# Save/load image
docker save app:latest | gzip > app.tar.gz
gunzip app.tar.gz | docker load
```

---

## Related Documentation

- [CERIT-SC Cloud Documentation](https://docs.cerit-sc.cz/)
- [E-INFRA CZ Knowledge Base](https://kb.einfra.cz/)
- [Kubernetes Official Docs](https://kubernetes.io/docs/)
- [GitHub Actions Docs](https://docs.github.com/en/actions)

---

*This skill was generated based on the λ-xTB project deployment patterns and CERIT-SC best practices.*
