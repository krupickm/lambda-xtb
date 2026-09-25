# λ-xTB Reorganization Energy Calculator

Give it a molecule as a [SMILES](https://en.wikipedia.org/wiki/Simplified_Molecular_Input_Line_Entry_System)
string, and it hands back **λ⁺** and **λ⁻**: the *intramolecular reorganization energy*
that governs how well that molecule conducts holes and electrons — a key design number for
OLED and organic-semiconductor (OSC) materials. Under the hood it runs the **Nelsen
four-point method** (three geometry optimizations, four single-point energies) at the
**GFN2-xTB** semiempirical level — a full run finishes in few minutes, where the
same seven-calculation recipe at full DFT would take hours. Every submission is stored, so
repeated runs on the same molecule build up a mean ± standard deviation instead of a
single, unverifiable number.

**Live:**
https://lambda-xtb.dyn.cloud.e-infra.cz


> Gated behind **e-infra AAI single sign-on** — you need an academic identity to log in. 

Try naphthalene (`c1ccc2ccccc2c1`, λ⁺ ≈ 190 meV) or see more
[demo molecules](DEVLOG.md#demo-molecules-for-testing).

## The engineering, in one paragraph

λ-xTB started as a single Flask pod that ran every ~60–120 s calculation **synchronously
inside the HTTP request**: one calculation at a time for everyone, on a 4-CPU pod that sat
idle almost all day for the rare moment someone actually submitted a molecule. It is now
**split into two roles that scale independently**: a tiny **always-on frontend** (UI +
internal API + SQLite, ~1 CPU) that, for every submission, **spawns a dedicated 14-CPU
Kubernetes Job** to do the actual xTB/CREST work and then disappears. The worker exposes no
API of its own — it's fire-and-forget: it POSTs its own state (`start` / `result` /
`error`) back to the frontend and exits.

```
Browser ──POST /calculate──▶ Frontend (1 CPU, always on)
                                │  writes a PENDING row to SQLite
                                │  spawns a Kubernetes Job ─────────┐
         ◀──redirect /pending/<uuid>──┘                             │
                                                                     ▼
                                                    Compute Job (14 CPU, one per calculation)
Frontend ◀──POST /api/jobs/<uuid>/result── runs calculate_lambda(), POSTs its result, exits
   (poll sees DONE) ──▶ redirect to /stats/<uuid>
```

The full design — and the production bugs it took to get right — is in
[`SERVICE_SPLIT.md`](SERVICE_SPLIT.md). The "how we got here" story, including using AI
coding agents under a strict human-review gate to implement the split one reviewable piece
at a time, is in [`DEVLOG.md`](DEVLOG.md#the-service-split-and-building-it-with-agents).

Two independent instances run side by side in `krupicka-ns`, from the **same image** —
so a change can be tried end-to-end on `test` without ever touching the instance in front
of real users:

| | prod | test |
|---|---|---|
| URL | `https://lambda-xtb.dyn.cloud.e-infra.cz` | `https://lambda-xtb-test.dyn.cloud.e-infra.cz` |
| Overlay | `k8s/overlays/prod` | `k8s/overlays/test` |
| Deployment / Service | `lambda-xtb` / `lambda-xtb-svc` | `lambda-xtb-test` / `lambda-xtb-svc-test` |
| PVC (its own SQLite) | `lambda-xtb-data` | `lambda-xtb-data-test` |
| Callback-token Secret | `lambda-xtb-callback` | `lambda-xtb-callback-test` |
| Moves when | a `v*` tag is pushed | the workflow is dispatched manually |

---

Everything below this line is **operational documentation** — for developers and for
whoever is running the cluster deployment. Curious what the code actually does, or the
chemistry behind it? [`DEVLOG.md`](DEVLOG.md) is the better next stop.

## Development

### 1) Create / activate the conda environment

```bash
conda env create -f environment.yml
conda activate xtb-lambda
pip install pytest ruff          # dev-only, deliberately not in environment.yml
```

### 2) Run locally

```bash
export FLASK_APP=app.py
export CALLBACK_TOKEN=dev        # the internal API rejects callbacks without it
flask run --host=0.0.0.0
```

Then open http://localhost:5000.

Off-cluster there is no in-cluster ServiceAccount config, so `create_compute_job()`
short-circuits: submitting a SMILES writes a PENDING row and redirects to `/pending`, but
**no Job is created**. Drive the worker by hand, with the uuid from the `/pending` URL:

```bash
JOB_UUID=<uuid> SMILES=c1ccccc1 \
CALLBACK_BASE_URL=http://localhost:5000 CALLBACK_TOKEN=dev \
python compute_runner.py
```

The poll page then flips to `/stats/<uuid>`.

### 3) Build and run with Docker

```bash
# Build base image once (heavy conda layer, ~3 min)
docker build -f Dockerfile.base -t cerit.io/krupickm/lambda-xtb-base:latest .

# Build app image (fast, ~20s)
docker build -t lambda-xtb-local .

docker run --rm -p 5000:5000 -e CALLBACK_TOKEN=dev lambda-xtb-local
```

## Kubernetes deployment (CERIT-SC)

> **CI never runs `kubectl apply`.** The release workflow only does `kubectl set image` /
> `kubectl set env COMPUTE_IMAGE=…` on an **existing** Deployment. Everything else in the
> pod spec — env vars, the ServiceAccount, RBAC, resources, volumes — reaches the cluster
> **only** when a human runs `kubectl apply -k`. Ship a change that adds an env var and
> release it without applying the overlay, and the pod runs the new code against the old
> spec: the symptom is a 500 with `KeyError: 'CALLBACK_BASE_URL'` on `POST /calculate`.
> See [Deploying a manifest change](#deploying-a-manifest-change-not-a-new-image).

Set up your shell once per session, and pick the instance:

```bash
module add kubectl
export KUBECONFIG=../kuba-cluster.yaml
export NS=krupicka-ns

# prod                                    # test
export OVERLAY=k8s/overlays/prod          # k8s/overlays/test
export DEPLOY=lambda-xtb                  # lambda-xtb-test
export SECRET=lambda-xtb-callback         # lambda-xtb-callback-test
```

### Bringing an instance up from scratch

**0. Check the image exists.** `k8s/base/deployment.yaml` pins an exact tag (in two
places: the container image and `COMPUTE_IMAGE`). Applying an overlay for a tag that has
not been built yet leaves the pod in `ImagePullBackOff`.

```bash
grep -n 'cerit.io/krupickm/lambda-xtb:' k8s/base/deployment.yaml
git tag -l                                # the tag must already have been pushed & built
```

**1. Create that instance's callback token.** The Secret is declared **name-only** in
`k8s/base/secret.yaml` — the token is never committed — so it must exist *before* the
overlay is applied, or the pod cannot start (`CreateContainerConfigError`). Once per
instance, never again: re-applying an overlay cannot overwrite a live token.

```bash
kubectl create secret generic $SECRET \
  --from-literal=token=$(openssl rand -hex 32) -n $NS
```

**2. Inspect what you are about to apply**, then dry-run it against the API server:

```bash
kubectl kustomize $OVERLAY                       # prod: expect today's exact names
kubectl apply -k $OVERLAY --dry-run=server -n $NS
```

For **prod** this is the important check: the overlay changes nothing but the namespace, so
every name is byte-identical to what is live and the apply **adopts** the running objects
instead of creating a parallel set beside them. For **test**, `nameSuffix: -test` creates a
fully separate set.

**3. Apply, and watch the rollout finish:**

```bash
kubectl apply -k $OVERLAY
kubectl rollout status deployment/$DEPLOY -n $NS --timeout=300s
```

If the rollout hangs on **prod**: 1 replica on a ReadWriteOnce PVC cannot roll, because the
new pod blocks waiting for a volume the old pod still holds if it lands on another node.
(The test overlay patches in `strategy: Recreate` for exactly this; prod has not got it
yet.) Force it through:

```bash
kubectl scale deploy/$DEPLOY --replicas=0 -n $NS
kubectl scale deploy/$DEPLOY --replicas=1 -n $NS
```

**4. On the test instance only**, put an image on it — the apply pinned it to the tag in
the manifest, which is prod's release, not your branch:

```
Actions → Build & Push Docker Image → Run workflow → instance: test
```

**5. Verify** (below) before calling it up.

### Verifying an instance

```bash
kubectl get pods    -n $NS -l app=lambda-xtb        # app=lambda-xtb-test for test
kubectl get ingress -n $NS                          # cert issued? ADDRESS assigned?

# The spec actually running — catches exactly the drift described above
kubectl exec deploy/$DEPLOY -n $NS -- env \
  | grep -E 'CALLBACK_BASE_URL|CALLBACK_TOKEN|COMPUTE_IMAGE|NAMESPACE|INSTANCE'
kubectl get deploy/$DEPLOY -n $NS \
  -o jsonpath='{.spec.template.spec.serviceAccountName}{"\n"}'   # must be lambda-xtb-sa

kubectl describe quota -n $NS                       # 14-CPU Jobs still fit?
```

Then submit a SMILES through the instance's URL and watch one calculation travel the whole
stack — frontend → compute Job → callbacks → SQLite:

```bash
kubectl get jobs,pods -n $NS -w
```

[docs/OBSERVING_A_RUN.md](docs/OBSERVING_A_RUN.md) walks through that in detail, including
the login-node thread hazard that comes with a parked `kubectl logs -f`.

### Environment the frontend Deployment must supply

All of it comes from `k8s/base/deployment.yaml` plus the instance's overlay. A missing
**required** var is a 500 on the first submission, not a failure at boot.

| Variable | Required | Default if unset | Purpose |
|---|---|---|---|
| `COMPUTE_IMAGE` | ✅ | — | image for the spawned compute Job; kept in lockstep with the frontend image by CI |
| `CALLBACK_BASE_URL` | ✅ | — | **this instance's own** in-cluster Service URL, so worker callbacks land in the right SQLite |
| `CALLBACK_TOKEN` | ✅ | — | shared Bearer token, from the Secret; also what the internal API checks |
| `NAMESPACE` | — | `default` | namespace the compute Jobs are created in — the default is silently *wrong* on-cluster |
| `DATABASE_URL` | — | `sqlite:///data/lambda.db` | SQLite file on the PVC |
| `INSTANCE` | — | `prod` | `instance=` label on spawned Jobs; attribution only |
| `JOB_CPU` / `JOB_MEMORY_REQUEST` / `JOB_MEMORY_LIMIT` | — | `14` / `4Gi` / `16Gi` | compute-Job size |
| `XTB_NPROC` | — | `JOB_CPU` | xtb/CREST threads inside the Job |
| `JOB_TIMEOUT_SECONDS` | — | `900` | when reconciliation gives up on a wedged Job |
| `OMP_NUM_THREADS`, `XDG_DATA_HOME` | — | — | frontend-local; `XDG_DATA_HOME=/tmp` is easyxtb's scratch dir |

The pod also needs `serviceAccountName: lambda-xtb-sa` and the Role/RoleBinding from
`k8s/base/rbac.yaml`; without them Job creation fails with `403 Forbidden`.

### Useful commands

```bash
# Logs
kubectl logs deploy/$DEPLOY -n $NS --tail=50

# Manual rollout (if CI failed)
kubectl rollout restart deployment/$DEPLOY -n $NS
kubectl rollout status  deployment/$DEPLOY -n $NS

# Copy SQLite DB out for inspection / export
kubectl cp $NS/<pod-name>:/app/data/lambda.db ./lambda.db
```

## Releasing a new version

Merging to `main` **deploys nothing**. It builds `:main` and `:<sha>` so an image exists if
you want one, and stops. **A tag is the only thing that moves prod**, and the manifests pin
that exact tag rather than `:latest` — so a λ value that ends up in a paper is traceable to
an exact image, and a rollback is just re-pinning the previous tag.

### Cutting a release

```bash
# 1. Bump BOTH image references (container image and COMPUTE_IMAGE) in one commit,
#    and merge it as a normal PR with CI green.
sed -i 's|lambda-xtb:v1\.2|lambda-xtb:v1.3|g' k8s/base/deployment.yaml
git checkout -b release-v1.3
git commit -am "Release v1.3"
gh pr create --fill

# 2. Tag the merged commit. This is the release.
git checkout main && git pull
git tag v1.3 && git push origin v1.3
```

The tag push runs CI, builds `cerit.io/krupickm/lambda-xtb:v1.3`, moves `:latest` onto it,
and rolls out prod with the exact tag.

The order is not optional, and the workflow enforces half of it: a release **refuses to
build** unless `k8s/base/deployment.yaml` already pins the tag being released, in both
places. The half it cannot enforce: do **not** `kubectl apply -k k8s/overlays/prod` for a
bump whose tag has not been built yet — the image does not exist and the pod will sit in
`ImagePullBackOff`.

**If the release also changed the Deployment spec** (a new env var, RBAC, resources,
volumes), the tag rollout does *not* carry it — see the next section.

### Deploying a manifest change (not a new image)

The release workflow only patches the image and `COMPUTE_IMAGE`. Anything else in
`k8s/` reaches the cluster only by hand:

```bash
kubectl apply -k $OVERLAY --dry-run=server -n $NS   # read the diff it reports
kubectl apply -k $OVERLAY
kubectl rollout status deployment/$DEPLOY -n $NS
```

Then re-verify the env with the commands under [Verifying an instance](#verifying-an-instance).

**Drift, both directions.** CI pins the live Deployment to a specific image with
`set image`/`set env`; an apply resets it to whatever `k8s/base/deployment.yaml` pins. On
**prod** those agree as long as you applied after the release that bumped them. On
**test**, an apply always knocks it back to prod's release tag — re-run the dispatch
workflow for `test` after any apply.

### Trying a branch on the test instance

Any branch, no tag, no PR needed:

```
Actions → Build & Push Docker Image → Run workflow → instance: test
```

Builds `:<sha>` and rolls out `lambda-xtb-test`. There is deliberately **no** manual prod
option: prod moves by tag or not at all. The test instance must already exist (the rollout
step does `kubectl set image` on an existing Deployment) — see
[Bringing an instance up from scratch](#bringing-an-instance-up-from-scratch).

### Rolling back

```bash
kubectl set image deployment/lambda-xtb \
  lambda-xtb=cerit.io/krupickm/lambda-xtb:v1.1 -n $NS
kubectl set env deployment/lambda-xtb \
  COMPUTE_IMAGE=cerit.io/krupickm/lambda-xtb:v1.1 -n $NS
```

Then bump `k8s/base/deployment.yaml` back to match, so the manifest stays an honest record
of what is running — and so the next apply does not silently roll forward again.

## CI/CD (GitHub Actions)

Three workflows in `.github/workflows/`:

| Workflow | Triggers | What it does |
|----------|----------|--------------|
| `ci.yml` | every pull request; called before every build | `ruff check`, unit tests (xtb mocked), one real xtb calculation on ethylene |
| `docker-build.yml` | push to `main`; push of a `v*` tag; manual dispatch | **tag** → builds `:<tag>` + `:latest` and rolls out prod; **main** → builds `:main` + `:<sha>`, deploys nothing; **dispatch** → builds `:<sha>` and rolls out the test instance |
| `base-image.yml` | `environment.yml` or `Dockerfile.base` change, or manual dispatch | rebuilds `lambda-xtb-base:latest` |

Nothing is built or deployed from a red tree: every build job depends on `ci.yml`.

### Required secrets

| Secret | Value |
|--------|-------|
| `HARBOR_USERNAME` | Harbor login name |
| `HARBOR_PASSWORD` | Harbor password |
| `KUBECONFIG_DATA` | Full contents of `kuba-cluster.yaml` |

## See also

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branch/PR workflow, test markers, what CI runs.
- [`SERVICE_SPLIT.md`](SERVICE_SPLIT.md) — architecture, the API contract, job state machine.
- [`docs/OBSERVING_A_RUN.md`](docs/OBSERVING_A_RUN.md) — watching one calculation end to end.
- [`DEVLOG.md`](DEVLOG.md) — architecture decisions, science background, known issues, open TODOs.
