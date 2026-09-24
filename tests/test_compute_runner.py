"""
Acceptance tests for WP3 — compute_runner.py (fire-and-forget worker).

No real network or infrastructure: a stub HTTP server (stdlib
http.server) captures the callback POSTs in-process, and `calculate_lambda`
/ `atoms_to_xyz` are monkeypatched so no xtb/CREST binaries run.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import compute_runner


class _CapturingHandler(BaseHTTPRequestHandler):
    """Records every POST (path, headers, JSON body); responds per `status_for`."""

    # Set per-test on the class before starting the server.
    requests = []
    status_for = None  # optional callable(path, request_index) -> int

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else {}

        index = len(type(self).requests)
        type(self).requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": body,
            }
        )

        status = 200
        if type(self).status_for is not None:
            status = type(self).status_for(self.path, index)

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, fmt, *args):  # silence test output
        pass


def _start_server(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _stop_server(server, thread):
    server.shutdown()
    thread.join(timeout=5)


class _FakeAtoms:
    """Stand-in for an ASE Atoms object, keyed by label for assertions."""

    def __init__(self, label):
        self.label = label


def _fake_atoms_to_xyz(atoms):
    return f"XYZ:{atoms.label}"


def _fake_calculate_lambda_ok(smiles):
    return {
        "lambda_plus_eV": 0.25,
        "lambda_minus_eV": 0.31,
        "partial": {"E0_geo0": -1.0},
        "geometries": {
            "neutral": _FakeAtoms("neutral"),
            "cation": _FakeAtoms("cation"),
            "anion": _FakeAtoms("anion"),
        },
    }


def _set_env(monkeypatch, base_url, job_uuid="test-uuid-123", smiles="c1ccccc1", token="secret-token"):
    monkeypatch.setenv("JOB_UUID", job_uuid)
    monkeypatch.setenv("SMILES", smiles)
    monkeypatch.setenv("CALLBACK_BASE_URL", base_url)
    monkeypatch.setenv("CALLBACK_TOKEN", token)


def test_happy_path_posts_start_then_result_and_exits_zero(monkeypatch):
    class Handler(_CapturingHandler):
        requests = []

    server, thread = _start_server(Handler)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        _set_env(monkeypatch, base_url)
        monkeypatch.setattr(compute_runner, "calculate_lambda", _fake_calculate_lambda_ok)
        monkeypatch.setattr(compute_runner, "atoms_to_xyz", _fake_atoms_to_xyz)

        exit_code = compute_runner.run()
    finally:
        _stop_server(server, thread)

    assert exit_code == 0
    assert [r["path"] for r in Handler.requests] == [
        "/api/jobs/test-uuid-123/start",
        "/api/jobs/test-uuid-123/result",
    ]
    for r in Handler.requests:
        assert r["auth"] == "Bearer secret-token"

    result_body = Handler.requests[1]["body"]
    assert result_body == {
        "lambda_plus_eV": 0.25,
        "lambda_minus_eV": 0.31,
        "partial": {"E0_geo0": -1.0},
        "xyz_neutral": "XYZ:neutral",
        "xyz_cation": "XYZ:cation",
        "xyz_anion": "XYZ:anion",
    }


def test_failure_path_posts_error_with_message_and_exits_one(monkeypatch):
    class Handler(_CapturingHandler):
        requests = []

    server, thread = _start_server(Handler)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        _set_env(monkeypatch, base_url)

        def _boom(smiles):
            raise ValueError("xtb blew up")

        monkeypatch.setattr(compute_runner, "calculate_lambda", _boom)
        monkeypatch.setattr(compute_runner, "atoms_to_xyz", _fake_atoms_to_xyz)

        exit_code = compute_runner.run()
    finally:
        _stop_server(server, thread)

    assert exit_code == 1
    assert [r["path"] for r in Handler.requests] == [
        "/api/jobs/test-uuid-123/start",
        "/api/jobs/test-uuid-123/error",
    ]
    assert Handler.requests[1]["body"] == {"message": "xtb blew up"}
    assert Handler.requests[1]["auth"] == "Bearer secret-token"


def test_retries_after_transient_503_then_succeeds(monkeypatch):
    class Handler(_CapturingHandler):
        requests = []

        @staticmethod
        def status_for(path, index):
            # Fail the very first callback attempt (the /start POST) once,
            # then succeed on every subsequent attempt.
            if index == 0:
                return 503
            return 200

    server, thread = _start_server(Handler)
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        _set_env(monkeypatch, base_url)
        monkeypatch.setattr(compute_runner, "calculate_lambda", _fake_calculate_lambda_ok)
        monkeypatch.setattr(compute_runner, "atoms_to_xyz", _fake_atoms_to_xyz)
        monkeypatch.setattr(compute_runner, "RETRY_BACKOFF_SECONDS", 0)

        exit_code = compute_runner.run()
    finally:
        _stop_server(server, thread)

    assert exit_code == 0
    # First /start attempt (503, retried) + successful retry + /result.
    paths = [r["path"] for r in Handler.requests]
    assert paths == [
        "/api/jobs/test-uuid-123/start",
        "/api/jobs/test-uuid-123/start",
        "/api/jobs/test-uuid-123/result",
    ]


# ── timestamped log output ──────────────────────────────────────────────────

def _stamped(text, stream=None):
    """Feed `text` through a _TimestampedStream and return what was written."""
    import io

    buf = stream if stream is not None else io.StringIO()
    out = compute_runner._TimestampedStream(buf, started_at=0.0)
    out.write(text)
    return buf.getvalue()


_STAMP = r"^\[\d{2}:\d{2}:\d{2} \+\s*\d+\.\d+s\] "


def test_timestamp_prefixes_each_line():
    import re

    written = _stamped("[1/5] starting\n[2/5] next\n")
    lines = written.splitlines()

    assert len(lines) == 2
    for line, tail in zip(lines, ("[1/5] starting", "[2/5] next"), strict=True):
        assert re.match(_STAMP, line), line
        assert line.endswith(tail)


def test_timestamp_not_repeated_mid_line():
    """calculate_lambda writes `Optimizing ... ` then completes the line later;
    the continuation must not get a second prefix."""
    import io
    import re

    buf = io.StringIO()
    out = compute_runner._TimestampedStream(buf, started_at=0.0)
    out.write("  Optimizing [neutral] ... ")   # print(..., end=" ")
    out.write("converged  E = -1.5 Eh\n")     # completing print

    written = buf.getvalue()
    assert len(written.splitlines()) == 1
    assert len(re.findall(_STAMP, written, flags=re.M)) == 1
    assert written.endswith("Optimizing [neutral] ... converged  E = -1.5 Eh\n")


def test_timestamp_leaves_blank_lines_blank():
    written = _stamped("\n[3/5] after a separator\n")
    first, second = written.splitlines()

    assert first == ""
    assert second.endswith("[3/5] after a separator")


def test_timestamp_stream_passes_through_flush_and_attrs():
    import io

    buf = io.StringIO()
    out = compute_runner._TimestampedStream(buf, started_at=0.0)
    out.flush()                      # must not raise
    assert out.isatty() is False
    assert out.writable() is True    # delegated to the wrapped stream


def test_missing_env_var_returns_exit_one(monkeypatch):
    monkeypatch.delenv("JOB_UUID", raising=False)
    monkeypatch.delenv("SMILES", raising=False)
    monkeypatch.delenv("CALLBACK_BASE_URL", raising=False)
    monkeypatch.delenv("CALLBACK_TOKEN", raising=False)

    assert compute_runner.run() == 1
