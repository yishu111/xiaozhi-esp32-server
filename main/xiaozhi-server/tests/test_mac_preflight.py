"""Startup scripts stay inside the supplied directory and do not print config."""

import os
import socket
import stat
import subprocess
import textwrap
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "scripts" / "mac" / "preflight_xiaozhi.sh"
START = ROOT / "scripts" / "mac" / "start_xiaozhi.sh"
PLIST = ROOT / "scripts" / "mac" / "com.xiaozhi.server.plist.template"
SECRET = "SUPER_SECRET_CONFIG_MARKER"


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _Handler(BaseHTTPRequestHandler):
    status_code = 200

    def do_GET(self):
        self.send_response(self.status_code)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, _format, *_args):
        return


def _serve(status):
    _Handler.status_code = status
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    import threading

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _tree(tmp_path: Path) -> Path:
    server = tmp_path / "xiaozhi-server"
    data = server / "data"
    data.mkdir(parents=True)
    (server / "app.py").write_text("print('booting')\n", encoding="utf-8")
    (data / ".config.yaml").write_text(f"marker: {SECRET}\n", encoding="utf-8")
    return server


def _python(tmp_path: Path, counter: Path) -> Path:
    path = tmp_path / "python-wrapper"
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            if [ "${{1:-}}" = "-c" ]; then
              exit 0
            fi
            if [ "${{1:-}}" = "-" ]; then
              exec python3 -
            fi
            if [ "${{1:-}}" = "app.py" ]; then
              echo app >> "{counter}"
              exit 1
            fi
            exec python3 "$@"
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _env(server: Path, python: Path, **extra):
    env = os.environ.copy()
    env.update(
        {
            "XIAOZHI_SERVER_DIR": str(server),
            "XIAOZHI_PYTHON": str(python),
            "XIAOZHI_WS_PORT": str(_free_port()),
            "XIAOZHI_HTTP_PORT": str(_free_port()),
            "XIAOZHI_MEM0_ATTEMPTS": "2",
            "XIAOZHI_MEM0_DELAY_SEC": "0",
        }
    )
    env.update(extra)
    return env


def _run(script, env):
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_preflight_refuses_root_language_and_hides_config():
    text = PREFLIGHT.read_text(encoding="utf-8")
    assert "refusing to run as root" in text
    assert "sudo" in text
    assert "tccutil" not in text
    assert "osascript" not in text
    plist = PLIST.read_text(encoding="utf-8")
    assert "<false/>" in plist
    assert "RunAtLoad" in plist


def test_preflight_accepts_ready_mem0_and_rejects_502(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("preflight refuses root, so this user cannot exercise the happy path")
    server_dir = _tree(tmp_path)
    python = _python(tmp_path, tmp_path / "count")
    ready = _serve(200)
    host, port = ready.server_address
    try:
        result = _run(
            PREFLIGHT,
            _env(
                server_dir,
                python,
                XIAOZHI_MEM0_HOST=host,
                XIAOZHI_MEM0_PORT=str(port),
                XIAOZHI_MEM0_HEALTH_URL=f"http://{host}:{port}/health",
                XIAOZHI_HOSTNAME_CHECK="1",
                XIAOZHI_HOSTNAME="localhost",
            ),
        )
    finally:
        ready.shutdown()
    assert result.returncode == 0, result.stderr
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
    assert "mem0_tcp_ready" in result.stdout
    assert "hostname_resolved=1" in result.stdout

    down = _serve(502)
    host, port = down.server_address
    try:
        failed = _run(
            PREFLIGHT,
            _env(
                server_dir,
                python,
                XIAOZHI_MEM0_HOST=host,
                XIAOZHI_MEM0_PORT=str(port),
                XIAOZHI_MEM0_HEALTH_URL=f"http://{host}:{port}/health",
            ),
        )
    finally:
        down.shutdown()
    assert failed.returncode == 78
    assert SECRET not in failed.stdout + failed.stderr


def test_preflight_refuses_unreadable_dir_busy_port_and_sudo(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root can still read mode 000 directories")
    server_dir = _tree(tmp_path)
    python = _python(tmp_path, tmp_path / "count")
    server_dir.chmod(0)
    try:
        denied = _run(PREFLIGHT, _env(server_dir, python))
    finally:
        server_dir.chmod(0o755)
    assert denied.returncode == 73
    assert "privacy settings" in denied.stderr

    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    try:
        busy = _run(
            PREFLIGHT,
            _env(server_dir, python, XIAOZHI_WS_PORT=str(held.getsockname()[1])),
        )
    finally:
        held.close()
    assert busy.returncode == 76
    assert "already in use" in busy.stdout

    sudo = _run(
        PREFLIGHT,
        _env(server_dir, python, XIAOZHI_MEM0_START_CMD="sudo true"),
    )
    assert sudo.returncode == 76


def test_start_retries_are_bounded_when_the_process_exits(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("preflight refuses root")
    server_dir = _tree(tmp_path)
    counter = tmp_path / "count"
    python = _python(tmp_path, counter)
    ready = _serve(200)
    host, port = ready.server_address
    try:
        result = _run(
            START,
            _env(
                server_dir,
                python,
                XIAOZHI_MEM0_HOST=host,
                XIAOZHI_MEM0_PORT=str(port),
                XIAOZHI_START_RETRIES="2",
                XIAOZHI_START_GRACE_SEC="0.1",
                XIAOZHI_RETRY_DELAY_SEC="0",
            ),
        )
    finally:
        ready.shutdown()
    assert result.returncode == 1
    assert counter.read_text(encoding="utf-8").count("app") == 2
    assert "stopped after 2 attempts" in result.stderr
