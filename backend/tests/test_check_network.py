"""Behavioural tests for CHECK-NETWORK.bat.

This script is the diagnosis of record: the user is told to screenshot its
output and send it back. It used to branch on curl's exit code alone, and curl
exits 0 for ANY completed HTTP transaction — including the 403 interstitial a
corporate/campus web filter answers with. So the one condition the script
exists to detect printed a clean sheet of OKs.

These tests run the real :check subroutine against a local server that answers
like a filtering proxy. Windows-only (cmd.exe + curl.exe), skipped elsewhere.
"""
from __future__ import annotations

import http.server
import os
import re
import subprocess
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "CHECK-NETWORK.bat"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("curl.exe") is None,
    reason="CHECK-NETWORK.bat needs Windows cmd.exe and curl.exe",
)


def _serve(status: int, body: bytes = b"<html>Access Denied by policy</html>"):
    """Start a one-off local HTTP server returning `status`; yields its base URL."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", "http://portal.example/login")
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _run_check(url: str) -> str:
    """Invoke the real :check subroutine from CHECK-NETWORK.bat against `url`."""
    src = SCRIPT.read_text(encoding="utf-8")
    # Split on the LABEL line, not on the earlier `call :check ...` lines.
    body = src.split("\n:check\n", 1)[1]
    harness = (
        '@echo off\r\n'
        'setlocal\r\n'
        'set "STATUSFILE=%TEMP%\\hcas-netcheck-test-%RANDOM%.txt"\r\n'
        f'call :check {url} "TEST TARGET"\r\n'
        'if exist "%STATUSFILE%" del "%STATUSFILE%" >nul 2>nul\r\n'
        'exit /b 0\r\n'
        ':check\r\n' + body.replace("\n", "\r\n")
    )
    tmp = Path(tempfile.gettempdir()) / f"hcas_netcheck_harness_{os.getpid()}.bat"
    tmp.write_bytes(harness.encode("ascii", "replace"))
    try:
        out = subprocess.run(
            ["cmd.exe", "/c", str(tmp)],
            capture_output=True, text=True, timeout=60,
        )
        return out.stdout
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.parametrize("status", [403, 429, 502, 407])
def test_block_page_is_reported_as_failed(status):
    """The exact failure the script exists to catch. curl exits 0 here."""
    server = _serve(status)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        out = _run_check(url)
    finally:
        server.shutdown()

    assert "FAILED" in out, out
    assert not re.search(r"^\s*OK\s", out, re.M), out
    assert str(status) in out, out  # the code itself must reach the screenshot


def test_redirect_to_a_portal_is_reported_as_failed():
    # The script follows redirects (-L), because a bare 3xx is NORMAL for several of
    # the real targets — cdn.jsdelivr.net 301s its own bare domain, and reporting
    # that as a captive portal sent the user chasing a fault that did not exist. So
    # we no longer assert on the intermediate "302". What must survive is that a
    # redirect off to a portal still FAILS and that the portal is named, so whoever
    # reads the screenshot can see where their traffic was sent.
    server = _serve(302)
    try:
        out = _run_check(f"http://127.0.0.1:{server.server_address[1]}/")
    finally:
        server.shutdown()
    assert "FAILED" in out, out
    assert not re.search(r"^\s*OK\s", out, re.M), out
    assert "portal.example" in out, out          # where the traffic actually went
    assert "redirect" in out, out                # and that it got there via a redirect
    assert "captive portal" in out, out


def test_a_normal_redirect_to_a_working_host_is_not_called_a_portal():
    # Regression guard for the false failure this replaced: a plain 3xx that lands
    # somewhere healthy is a success, and the landing URL must still be printed so a
    # genuine off-host portal stays visible to a human.
    dest = _serve(200, b'{"ok":true}')
    port = dest.server_address[1]

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(301)
            self.send_header("Location", f"http://127.0.0.1:{port}/landed")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    hop = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=hop.serve_forever, daemon=True).start()
    try:
        out = _run_check(f"http://127.0.0.1:{hop.server_address[1]}/")
    finally:
        hop.shutdown()
        dest.shutdown()
    assert re.search(r"^\s*OK\s+TEST TARGET - HTTP 200", out, re.M), out
    assert "FAILED" not in out, out
    assert "captive portal" not in out, out
    assert f"127.0.0.1:{port}/landed" in out, out


def test_real_200_still_reports_ok():
    server = _serve(200, b'{"ok":true}')
    try:
        out = _run_check(f"http://127.0.0.1:{server.server_address[1]}/")
    finally:
        server.shutdown()
    assert re.search(r"^\s*OK\s+TEST TARGET - HTTP 200", out, re.M), out
    assert "FAILED" not in out, out


def test_unreachable_port_is_reported_as_failed():
    # Nothing listening: a transport-level failure, still FAILED, never OK.
    out = _run_check("http://127.0.0.1:9/")
    assert "FAILED" in out, out
    assert not re.search(r"^\s*OK\s", out, re.M), out
