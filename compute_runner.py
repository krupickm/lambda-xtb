"""
compute_runner.py
==================
Fire-and-forget worker entrypoint for the per-request compute Job.

This process is NOT a server: it exposes no API and listens on nothing. It
runs once, reports its outcome to the frontend via HTTP callbacks, and exits.
See SERVICE_SPLIT.md ("API Contract & Job States") for the authoritative
contract this module implements.

Sequence
--------
    POST {CALLBACK_BASE_URL}/api/jobs/{JOB_UUID}/start    (PENDING -> PROCESSING)
    results = calculate_lambda(SMILES)                     (lambda_xtb.py)
    POST {CALLBACK_BASE_URL}/api/jobs/{JOB_UUID}/result    (-> DONE)
        on exception:
    POST {CALLBACK_BASE_URL}/api/jobs/{JOB_UUID}/error     (-> ERROR)

Required environment variables
-------------------------------
    JOB_UUID            job identifier (path segment for all callbacks)
    SMILES              SMILES string to run calculate_lambda on
    CALLBACK_BASE_URL   frontend base URL, e.g. http://lambda-xtb-svc.krupicka-ns.svc.cluster.local
    CALLBACK_TOKEN      shared Bearer token for the worker-facing API

Exit code: 0 on success (POSTed /result), 1 on any failure (POSTed /error,
or failed before that point). A non-zero exit with no /error POST is caught
by the frontend's dead-worker reconciliation (SERVICE_SPLIT.md).

Run as a script, stdout/stderr are wrapped so every line carries a wall-clock
time and an elapsed counter (`_TimestampedStream`) — the compute pod's log is
the only record of how long each stage of a calculation took.
"""

import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request

from lambda_xtb import atoms_to_xyz, calculate_lambda

# Small retry budget for transient network errors / 5xx from the frontend.
RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2
_TRANSIENT_HTTP_STATUSES = {502, 503, 504}


class _TimestampedStream:
    """Line-prefixing stdout/stderr wrapper: `[HH:MM:SS +MM.Ms] <line>`.

    calculate_lambda() reports progress with a mix of whole lines and partial
    ones (`print(..., end=" ")` completed by a later `print`), so the prefix is
    written only where a line actually begins — a continuation stays on the
    line it belongs to, and blank separator lines stay blank.

    Only Python-level writes are stamped. Anything a child process (xtb, CREST)
    writes straight to the inherited file descriptor bypasses this.
    """

    def __init__(self, stream, started_at: float | None = None) -> None:
        self._stream = stream
        self._started_at = time.monotonic() if started_at is None else started_at
        self._at_line_start = True

    def _prefix(self) -> str:
        elapsed = time.monotonic() - self._started_at
        return f"[{time.strftime('%H:%M:%S')} +{elapsed:7.1f}s] "

    def write(self, text: str) -> int:
        if not text:
            return 0
        out = []
        for part in text.splitlines(keepends=True):
            if self._at_line_start and part != "\n":
                out.append(self._prefix())
            out.append(part)
            self._at_line_start = part.endswith("\n")
        self._stream.write("".join(out))
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name):
        stream = self.__dict__.get("_stream")
        if stream is None:
            raise AttributeError(name)
        return getattr(stream, name)


def _post(url: str, token: str, payload: dict) -> None:
    """POST JSON to `url` with Bearer auth, retrying transient failures.

    Retries on HTTP 502/503/504 and on connection-level errors (URLError);
    any other HTTPError (e.g. 401, 409, 400) is raised immediately since
    retrying will not help.
    """
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }

    for attempt in range(1, RETRY_ATTEMPTS + 1):
        request = urllib.request.Request(url, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request) as response:
                response.read()
            return
        except urllib.error.HTTPError as exc:
            if exc.code not in _TRANSIENT_HTTP_STATUSES or attempt == RETRY_ATTEMPTS:
                raise
        except urllib.error.URLError:
            if attempt == RETRY_ATTEMPTS:
                raise
        time.sleep(RETRY_BACKOFF_SECONDS)


def run() -> int:
    """Execute one job end to end. Returns the process exit code."""
    try:
        job_uuid = os.environ["JOB_UUID"]
        smiles = os.environ["SMILES"]
        base_url = os.environ["CALLBACK_BASE_URL"].rstrip("/")
        token = os.environ["CALLBACK_TOKEN"]
    except KeyError as exc:
        print(f"compute_runner: missing required env var {exc}", file=sys.stderr)
        return 1

    start_url = f"{base_url}/api/jobs/{job_uuid}/start"
    result_url = f"{base_url}/api/jobs/{job_uuid}/result"
    error_url = f"{base_url}/api/jobs/{job_uuid}/error"

    try:
        _post(start_url, token, {})
    except Exception:
        traceback.print_exc()
        return 1

    try:
        results = calculate_lambda(smiles)
        payload = {
            "lambda_plus_eV": results["lambda_plus_eV"],
            "lambda_minus_eV": results["lambda_minus_eV"],
            "partial": results["partial"],
            "xyz_neutral": atoms_to_xyz(results["geometries"]["neutral"]),
            "xyz_cation": atoms_to_xyz(results["geometries"]["cation"]),
            "xyz_anion": atoms_to_xyz(results["geometries"]["anion"]),
        }
        _post(result_url, token, payload)
    except Exception as exc:
        traceback.print_exc()
        try:
            _post(error_url, token, {"message": str(exc)})
        except Exception:
            traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    # Installed here, not in run(), so importing this module (tests) never
    # replaces the interpreter's streams.
    _started_at = time.monotonic()
    sys.stdout = _TimestampedStream(sys.stdout, _started_at)
    sys.stderr = _TimestampedStream(sys.stderr, _started_at)
    sys.exit(run())
