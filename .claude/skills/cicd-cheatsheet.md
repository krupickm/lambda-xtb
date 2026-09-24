# CI/CD & Kubernetes Deployment Cheatsheet

Quick reference for deploying to CERIT-SC Kubernetes with GitHub Actions.

---

## One-Command Deployments

### Local Testing
```bash
docker build -f Dockerfile.base -t cerit.io/youruser/app-base:latest .
docker build --build-arg BUILD_VERSION=1.0 -t cerit.io/youruser/app:local .
docker run --rm -p 5000:5000 cerit.io/youruser/app:local
```

### Manual Production Deploy
```bash
docker build -f Dockerfile.base -t cerit.io/youruser/app-base:latest . && \
docker build --build-arg BUILD_VERSION=$(git rev-list --count HEAD) -t cerit.io/youruser/app:$(git rev-parse --short HEAD) . && \
docker push cerit.io/youruser/app-base:latest && \
docker push cerit.io/youruser/app:$(git rev-parse --short HEAD) && \
kubectl set image deployment/app app=cerit.io/youruser/app:$(git rev-parse --short HEAD) -n your-ns && \
kubectl rollout status deployment/app -n your-ns --timeout=300s
```

---

## GitHub Secrets Setup

```bash
# Harbor credentials
HARBOR_USERNAME=your-harbor-username
HARBOR_PASSWORD=your-harbor-password

# Kubeconfig (base64 encoded)
KUBECONFIG_DATA=$(cat kuba-cluster.yaml)
```

Add to GitHub: `Settings → Secrets and variables → Actions → New repository secret`

---

## Kubernetes Manifests (Minimal)

### deployment.yaml
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: app
spec:
  replicas: 1
  selector:
    matchLabels: {app: app}
  template:
    metadata:
      labels: {app: app}
    spec:
      securityContext:
        runAsNonRoot: true
        seccompProfile: {type: RuntimeDefault}
      containers:
      - name: app
        image: cerit.io/youruser/app:latest
        ports: [{containerPort: 5000}]
        securityContext:
          runAsUser: 1000
          allowPrivilegeEscalation: false
          capabilities: {drop: [ALL]}
        resources:
          requests: {cpu: "1", memory: "1Gi"}
          limits: {cpu: "2", memory: "4Gi"}
```

### service.yaml
```yaml
apiVersion: v1
kind: Service
metadata:
  name: app-svc
spec:
  ports: [{port: 80, targetPort: 5000}]
  selector: {app: app}
```

### ingress.yaml
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: app-ingress
  annotations:
    cert-manager.io/cluster-issuer: "letsencrypt-prod"
spec:
  ingressClassName: nginx
  tls:
  - hosts: ["app.dyn.cloud.e-infra.cz"]
    secretName: app-tls
  rules:
  - host: "app.dyn.cloud.e-infra.cz"
    http:
      paths:
      - path: /
        pathType: ImplementationSpecific
        backend:
          service:
            name: app-svc
            port: {number: 80}
```

---

## GitHub Actions Workflow (Minimal)

```yaml
name: CI/CD
on:
  push:
    branches: [main]

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Login to Harbor
        uses: docker/login-action@v3
        with:
          registry: cerit.io
          username: ${{ secrets.HARBOR_USERNAME }}
          password: ${{ secrets.HARBOR_PASSWORD }}

      - name: Build & Push
        uses: docker/build-push-action@v6
        with:
          push: true
          tags: |
            cerit.io/youruser/app:latest
            cerit.io/youruser/app:${{ github.sha }}

      - name: Deploy to K8s
        run: |
          echo "${{ secrets.KUBECONFIG_DATA }}" > ~/.kube/config
          kubectl set image deployment/app \
            app=cerit.io/youruser/app:${{ github.sha }} \
            -n your-namespace
          kubectl rollout status deployment/app -n your-namespace
```

---

## Troubleshooting Commands

```bash
# Pod stuck?
kubectl describe pod -l app=app -n your-namespace

# Crashing?
kubectl logs -l app=app -n your-namespace --previous

# Rollback?
kubectl rollout undo deployment/app -n your-namespace

# Check everything?
kubectl get all,pvc,ingress -n your-namespace
kubectl describe ingress -n your-namespace
```

---

## Common Issues & Fixes

| Issue | Fix |
|-------|-----|
| `ImagePullBackOff` | Verify image exists, check Harbor credentials |
| `CrashLoopBackOff` | Check logs, verify env vars and volume mounts |
| `Pending` | Check resource quotas, PVC binding |
| `OOMKilled` | Increase memory limits in deployment |
| TLS not working | Wait up to 60min for Let's Encrypt, check cert-manager |

---

## Namespace Setup (First Time)

```bash
module add kubectl
export KUBECONFIG=../kuba-cluster.yaml
kubectl create namespace your-namespace
kubectl apply -f k8s/ -n your-namespace
```

---

*See full documentation: `.claude/skills/k8s-deployment.md`*
