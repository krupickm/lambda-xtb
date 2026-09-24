# Observing a calculation run on Kubernetes

How to watch one λ-xTB calculation travel through the split stack: browser → frontend →
spawned compute Job → callbacks → SQLite. Written for the **test** instance; for prod,
drop the `-test` suffixes and swap the URL.

Architecture and the authoritative API contract are in [`SERVICE_SPLIT.md`](../SERVICE_SPLIT.md);
this file is purely operational.

> **Read [Running these on a shared login node](#running-these-on-a-shared-login-node) first
> if you are on `perian` or another MetaCentrum frontend.** A long-lived `kubectl logs -f`
> has been observed accumulating enough OS threads there to make every *later* `kubectl`
> invocation fail.

```bash
export NS=krupicka-ns
export URL=https://lambda-xtb-test.dyn.cloud.e-infra.cz
```

---

## What you are watching

| Stage | Where it shows up |
|---|---|
| Browser submits a SMILES | frontend log: `POST /calculate` → 302 |
| Frontend writes PENDING and creates the Job | `kubectl get jobs -l instance=test` |
| Compute pod scheduled, image pulled | pod events |
| Worker announces itself | frontend log: `POST /api/jobs/<uuid>/start` |
| xTB/CREST actually running | compute pod log: `[1/5]`…`[5/5]` |
| Worker returns the result | frontend log: `POST /api/jobs/<uuid>/result` |
| Browser notices | frontend log: repeated `GET /api/jobs/<uuid>/status` |

Both the frontend pod and every compute pod carry `instance=test`, so one selector covers
the whole instance. Compute pods additionally carry `app=lambda-xtb-compute` and
`job-uuid=<uuid>`.

---

## Live: two panes, opened before you submit

**Pane 1 — objects appearing and disappearing.** Cheap, safe to leave running for a while:

```bash
kubectl get pods -l instance=test -n $NS -w
```

**Pane 2 — pod-level events** (scheduling, image pulls, OOM kills, evictions):

```bash
kubectl get events -n $NS --watch --field-selector involvedObject.kind=Pod
```

For the frontend's request log, prefer repeated snapshots over a parked `-f`:

```bash
kubectl logs deploy/lambda-xtb-test -n $NS --tail=50
```

## Submit and capture the uuid

```bash
JOB=$(curl -sS -o /dev/null -w '%{redirect_url}' -X POST -d 'smiles=c1ccccc1' $URL/calculate \
      | sed 's#.*/pending/##')
echo "job uuid: $JOB"
```

## Follow that one job

```bash
kubectl wait --for=condition=Ready pod -l job-uuid=$JOB -n $NS --timeout=600s
GOMAXPROCS=2 kubectl logs -f job/lambda-xtb-compute-$JOB -n $NS
```

That is the live calculation: `[1/5] Generating 3D starting geometry` through
`[5/5] Cross single-points`, the CREST conformer count, and each converged energy. The
compute container sets `PYTHONUNBUFFERED=1` so these arrive as they happen rather than in
one burst at exit — a frontend older than commit `ef9124d` will not set it, and the log
will look like it is doing nothing until the pod finishes.

Every line carries a wall clock and an elapsed counter, which is how you tell where the
time actually went:

```
[13:49:01 +   12.4s] [2/5] GFN-FF pre-optimisation ...
[13:49:01 +   12.4s]   Optimizing  [neutral] (GFN-FF/loose) ... converged  E = -12.34567890 Eh
[13:49:14 +   25.7s] [3/5] Flexible molecule — running CREST --squick --gfnff ...
```

The stamp marks where a *line* began. `lambda_xtb` writes some steps as
`Optimizing ... ` followed later by `converged E = ...` on the same line, so such a line
is stamped with the moment the step **started**; the following line's counter tells you
when it ended. Only Python-level output is stamped — anything xtb or CREST writes straight
to the inherited file descriptor is not.

`kubectl` can also stamp lines itself, with the time the container runtime received them.
That needs no rebuild and works on older images, so it is the fallback when you are
looking at a pod that predates this:

```bash
kubectl logs --timestamps job/lambda-xtb-compute-$JOB -n $NS
```

If the pod never becomes Ready, the reason is in its events (usually quota or scheduling):

```bash
kubectl describe pod -l job-uuid=$JOB -n $NS | sed -n '/Events:/,$p'
```

## Confirm the Job got what the manifest promised

```bash
kubectl get job lambda-xtb-compute-$JOB -n $NS -o jsonpath='{.metadata.labels}{"\n"}'
kubectl get pod -l job-uuid=$JOB -n $NS -o jsonpath='{.items[0].spec.containers[0].resources}{"\n"}'
kubectl get pod -l job-uuid=$JOB -n $NS -o wide
```

Expect `instance: test` in the labels and `cpu: "14"`, `memory: 4Gi`/`16Gi` in resources.

## The state machine

From outside, the same endpoint the pending page polls:

```bash
while true; do curl -s $URL/api/jobs/$JOB/status; echo; sleep 3; done
```

`PENDING → PROCESSING → DONE`. And the row behind it, which is the source of truth:

```bash
kubectl exec deploy/lambda-xtb-test -n $NS -- python -c "
import sqlite3
c = sqlite3.connect('data/lambda.db')
for r in c.execute('select uuid, status, smiles_input, lambda_plus_eV, error_message from jobs order by created_at desc limit 5'):
    print(r)
"
```

Status codes: `-1` ERROR, `0` PENDING, `1` PROCESSING, `2` DONE, `3` SEEN.

## Two properties worth demonstrating deliberately

Callback auth is enforced — without the Bearer token there is no way in:

```bash
curl -si -X POST $URL/api/jobs/$JOB/start | head -1      # 401
```

The two instances are genuinely separate. Each selector must return exactly one pod; if
either returns both, the label isolation has regressed and the Services will cross over:

```bash
kubectl get pods -l app=lambda-xtb      -n $NS      # prod only
kubectl get pods -l app=lambda-xtb-test -n $NS      # test only
```

---

## After the fact: you missed the run

A finished Job and its pod survive `ttlSecondsAfterFinished: 3600` — **one hour** after
completion — and `kubectl logs` works fine on a `Completed` pod.

```bash
kubectl get jobs -l instance=test -n $NS
kubectl get job -l instance=test -n $NS -o custom-columns=\
'NAME:.metadata.name,SUCCEEDED:.status.succeeded,FAILED:.status.failed,DONE:.status.completionTime'
```

Save everything before the TTL collects it:

```bash
kubectl logs job/lambda-xtb-compute-$JOB -n $NS | tee /tmp/compute-$JOB.log
kubectl get job lambda-xtb-compute-$JOB -n $NS -o yaml > /tmp/compute-$JOB.yaml
```

`backoffLimit: 1` means a failed job may have **two** pods and `kubectl logs job/...` shows
only one:

```bash
for p in $(kubectl get pods -l job-uuid=$JOB -n $NS -o name); do
  echo "=== $p"; kubectl logs $p -n $NS; done
```

**If the Job is already gone**, the outcome survives in two places. How far it got:

```bash
kubectl logs deploy/lambda-xtb-test -n $NS --tail=300 | grep 'api/jobs'
```

`/start` with no `/result` or `/error` means the worker died mid-calculation. And the
verdict, from the row: status `2` completed normally; `-1` with
`compute pod terminated unexpectedly` is `reconcile()` having caught a silent death; `-1`
with any other message came from the worker's own `/error` callback.

Cluster events also outlive the pod by roughly an hour:

```bash
kubectl get events -n $NS --sort-by=.lastTimestamp | tail -30
```

---

## Running these on a shared login node

`kubectl` is a Go binary, and the Go runtime starts a new OS thread whenever a goroutine
blocks in a syscall. Threads are cheap on a workstation. On a shared MetaCentrum frontend
they are capped — not by `ulimit -u` (31509 on `perian`, far above anything you will reach)
but by a cgroup `pids.max` on the user slice, which an ordinary user cannot read.

**Observed on `perian`, 2026-09-24:** a single

```bash
kubectl logs -f deploy/lambda-xtb-test -n krupicka-ns
```

left running for about 15 minutes reached **103 OS threads**, roughly 40% of the ~254 the
user session had in total. Every subsequent `kubectl` then died before it could start:

```
runtime: failed to create new OS thread (have 54 already; errno=11)
runtime: may need to increase max user processes (ulimit -u)
fatal error: newosproc
```

`errno=11` is `EAGAIN` — the kernel refusing a new thread. The message's own advice about
`ulimit -u` is a red herring here.

**The cause was never established.** The calculation that run was following completed
normally, and no pod restart or crash-loop was found that would explain a reconnect loop.
Treat it as a known hazard of parking `logs -f` on a login node rather than as a
diagnosed bug.

### Detect

```bash
ps -u $USER -L -o pid,nlwp,comm --no-headers | awk '{t[$3]+=1} END {for (c in t) print t[c], c}' | sort -rn | head
ps -u $USER -o pid,etime,nlwp,args --no-headers | grep '[k]ubectl'
```

A healthy `kubectl` sits at a handful of threads. Tens is the warning sign.

### Recover

```bash
pkill -u $USER -f 'kubectl logs -f'
pkill -u $USER -f 'kubectl get.*-w'
```

Killing the offender frees its threads immediately; no cluster state is affected, because
all of these commands are read-only.

### Avoid

- Prefer snapshots to follows: `kubectl logs --tail=100` in a loop beats `logs -f` parked
  for an hour.
- When you do want to follow, bound the runtime's parallelism: `GOMAXPROCS=2 kubectl logs -f …`
- Give any follow a deadline, e.g. `timeout 300 kubectl logs -f …`.
- `kubectl get … -w` is far lighter than `logs -f`, but still worth closing when done.
- For a long watch, run it somewhere that is not the shared frontend — a laptop or WSL
  clone with its own kubeconfig.
