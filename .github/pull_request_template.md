## What & why

<!-- One paragraph. What changes, and what problem it solves. -->

## How it was checked

<!-- Delete what doesn't apply. CI runs ruff + the mocked tests + the ethylene
     smoke test; say here what CI cannot see. -->

- [ ] `ruff check .` and `pytest -q` pass locally
- [ ] Tried on the test instance (Actions → Build & Push Docker Image → instance: test)
- [ ] Manifest change verified with `kubectl apply -k k8s/overlays/<inst> --dry-run=server`

## Release bump?

<!-- Only if this PR bumps the image tag in k8s/base/deployment.yaml. -->

- [ ] Both references bumped (container `image:` **and** `COMPUTE_IMAGE`)
- [ ] I will tag the merged commit — and not `kubectl apply` the new tag before
      the release build has pushed the image

<!-- See CONTRIBUTING.md for the full loop. -->
