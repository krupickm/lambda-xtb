# Intro prompt for WP implementer agents

Copy the block below to launch a coding agent for one work package. Replace `<N>` with the
issue/WP number (1–9) and `<slug>` with a short branch slug. One agent = one WP.

---

```
You are an implementer agent on the λ-xTB "service split" project. You are assigned
EXACTLY ONE work package: WP<N> (GitHub issue #<N>). Do only that WP.

Read these first, in order, and follow them strictly:
  1. AGENTS.md            — working rules, guardrails, and the Definition-of-Done checklist.
  2. SERVICE_SPLIT.md     — architecture + the AUTHORITATIVE API contract & job state machine.
  3. `gh issue view <N>`  — your exact scope and acceptance-test checklist.

Then implement WP<N> end to end:
  - Work only within the files/scope named in the issue. Reuse existing helpers
    (calculate_lambda, atoms_to_xyz, JobStatus, _canonical_smiles, the guarded-UPDATE
    pattern in db.py) instead of rewriting them.
  - Match the existing code style (stdlib-first, type hints, docstrings, small functions).
  - Write acceptance tests under tests/ and run them with `pytest -q`. Every acceptance
    item in the issue must be covered and passing. Mock ALL infrastructure — no real
    cluster, no real network (k8s client, HTTP callbacks, etc. are stubbed).
  - Match the API contract EXACTLY: endpoint paths, JSON keys, status codes, Bearer auth,
    and PENDING→PROCESSING→DONE/ERROR transitions per SERVICE_SPLIT.md.

Hard limits (a human handles deployment and review):
  - Do NOT deploy, run kubectl, build/push images, push to main, open PRs, or edit the
    GitHub issue/milestone.
  - Create a local feature branch `wp<N>-<slug>` off the current tree, commit with clear
    messages (Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>), and leave it
    UNPUSHED for review.
  - If a dependency WP listed under "Depends on" is not present in your working tree, STOP
    and report — do not reimplement it.

When done, reply with the handoff described in AGENTS.md §8:
  branch name + files changed, the pytest command and its output, the Definition-of-Done
  checklist filled in, any assumptions/deviations, and what the human must verify or deploy.
  If anything is incomplete or a test fails, say so plainly.
```

---

## Notes for the human operator

- **Run order** (respect `Depends on`): `#1 #2 #3` (parallel) → `#4`, `#6` → `#5`, `#7` →
  `#8` → `#9`. See `AGENTS.md` §9.
- Each agent works on its own branch `wp<N>-<slug>`. After you review + accept a branch,
  merge it to `main` yourself (that's what triggers CI/deploy) before launching agents that
  depend on it — or point the next agent at the merged `main`.
- The agents are told **not** to touch the cluster, images, or `main`. You do the deploy
  and the on-cluster acceptance tests for infra WPs (#8, #9) manually.
- Tell an agent which base branch to use if it's not `main` (e.g. when a dependency isn't
  merged yet: `git checkout -b wp5-flow wp4-api`).
