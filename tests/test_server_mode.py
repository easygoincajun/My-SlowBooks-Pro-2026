"""Server Edition groundwork (PR-S1): LAN bind plumbing, SQLite WAL
tuning, and the /api/system server_mode flag the UI keys its header off."""

import sqlite3
import sys

import pytest

from sqlalchemy import create_engine, text

import desktop_launcher
from app.database import enable_sqlite_tuning

# ---------------------------------------------------------------------------
# SQLite concurrency tuning
# ---------------------------------------------------------------------------


def test_sqlite_tuning_enables_wal(tmp_path):
    db = tmp_path / "tuned.db"
    engine = create_engine(f"sqlite:///{db}")
    enable_sqlite_tuning(engine)
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        busy = conn.execute(text("PRAGMA busy_timeout")).scalar()
        sync = conn.execute(text("PRAGMA synchronous")).scalar()
    assert str(mode).lower() == "wal"
    assert int(busy) == 5000
    assert int(sync) == 1  # NORMAL

    # WAL is persistent in the file: a plain sqlite3 connection sees it too
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    raw.close()


def test_sqlite_tuning_harmless_on_memory_db():
    engine = create_engine("sqlite:///:memory:")
    enable_sqlite_tuning(engine)
    with engine.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar()
    assert str(mode).lower() == "memory"  # no-op, no error


# ---------------------------------------------------------------------------
# Launcher bind plumbing
# ---------------------------------------------------------------------------


def test_server_env_default_is_loopback():
    env = desktop_launcher._server_env("sqlite:///x.db", 3001)
    assert env["APP_HOST"] == "127.0.0.1"
    assert env["SLOWBOOKS_SERVER_MODE"] == "0"


def test_server_env_lan_bind_sets_server_mode():
    env = desktop_launcher._server_env("sqlite:///x.db", 3001, bind_host="0.0.0.0")
    assert env["APP_HOST"] == "0.0.0.0"
    assert env["SLOWBOOKS_SERVER_MODE"] == "1"
    env = desktop_launcher._server_env(
        "sqlite:///x.db", 3001, bind_host="192.168.68.50"
    )
    assert env["SLOWBOOKS_SERVER_MODE"] == "1"


def test_lan_addresses_never_raises():
    addrs = desktop_launcher._lan_addresses()
    assert isinstance(addrs, list)
    assert all(isinstance(a, str) and a for a in addrs)


# ---------------------------------------------------------------------------
# /api/system flag
# ---------------------------------------------------------------------------


def test_system_info_reports_server_mode(authed_client, monkeypatch):
    info = authed_client.get("/api/system").json()
    assert info["server_mode"] is False

    monkeypatch.setenv("SLOWBOOKS_SERVER_MODE", "1")
    info = authed_client.get("/api/system").json()
    assert info["server_mode"] is True


# ---------------------------------------------------------------------------
# PR-S4: banner composition + --data-dir override
# ---------------------------------------------------------------------------


def test_serve_banner_lists_all_addresses():
    text = desktop_launcher._compose_serve_banner(3001, ["OFFICE-PC", "192.168.68.50"])
    assert "http://OFFICE-PC:3001" in text
    assert "http://192.168.68.50:3001" in text
    assert "plain HTTP" in text
    # No addresses discovered: still renders something actionable
    fallback = desktop_launcher._compose_serve_banner(3001, [])
    assert "3001" in fallback


def test_data_dir_flag_redirects_everything(tmp_path):
    import shutil
    import subprocess
    import sys as _sys
    from pathlib import Path

    # Run a copy: from a checkout the launcher keeps its .env beside itself,
    # so running the checkout's own copy rewrote the developer's .env (its
    # APP_HOST, and a SETTINGS_ENCRYPTION_KEY that then outranks their
    # .slowbooks-master.key) — or left a new one in the tree (2.18.0 gate,
    # macbase1).
    checkout = Path(desktop_launcher.__file__).resolve().parent
    launcher = tmp_path / "desktop_launcher.py"
    shutil.copyfile(checkout / "desktop_launcher.py", launcher)
    their_env = checkout / ".env"
    before = their_env.read_bytes() if their_env.exists() else None

    target = tmp_path / "server-data"
    r = subprocess.run(
        [
            _sys.executable,
            str(launcher),
            "--setup-only",
            "--data-dir",
            str(target),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**__import__("os").environ, "SLOWBOOKS_DATA_DIR": ""},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert (target / "companies").is_dir()
    assert (tmp_path / ".env").is_file()
    assert (their_env.read_bytes() if their_env.exists() else None) == before


# ---------------------------------------------------------------------------
# Issue #52: the server child must die with its launcher parent
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the watcher is POSIX-only by design — desktop_launcher._serve() "
        "guards it with `if sys.platform != 'win32'`, and this test does not. "
        "It matters more than an unused feature: os.kill(pid, 0) is an "
        "existence check on POSIX but TERMINATES the target on Windows, so "
        "running this there kills processes rather than probing them — "
        "including, via a reused parent pid, the pytest process itself. That "
        "is what took the Windows CI job down at 84% with no summary (#121)."
    ),
)
def test_parent_watcher_exits_when_parent_dies(tmp_path):
    """Real process pair: a fake 'launcher' spawns a watcher child; killing
    the launcher must take the child down within the poll interval."""
    import subprocess
    import sys as _sys
    import time

    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, sys, time\n"
        "sys.path.insert(0, %r)\n"
        "import desktop_launcher\n"
        "desktop_launcher._watch_parent(os.getppid(), poll_seconds=0.2)\n"
        "time.sleep(30)\n"
        % str(
            tmp_path.parent
            and __import__("pathlib").Path(__file__).resolve().parents[1].as_posix()
        )
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    parent = subprocess.Popen(
        [_sys.executable, str(parent_script)], stdout=subprocess.PIPE, text=True
    )
    child_pid = int(parent.stdout.readline().strip())

    # While the parent lives, the child survives several poll cycles
    time.sleep(1.0)
    import os as _os

    _os.kill(child_pid, 0)  # raises if dead — it must be alive

    # Kill the launcher; the watcher must take the child down
    parent.kill()
    parent.wait()
    deadline = time.time() + 5
    alive = True
    while time.time() < deadline:
        try:
            _os.kill(child_pid, 0)
            time.sleep(0.2)
        except OSError:
            alive = False
            break
    assert not alive, "server child outlived its launcher parent"


def test_source_run_starts_the_watched_serve_child(monkeypatch):
    """From source, start_server() must launch the same --_serve child as
    the frozen build: that is where the parent watcher lives. It launched a
    bare `python -m uvicorn`, which has no watcher, so Command-Q orphaned
    it on the port (#52 fixed only the frozen build)."""
    spawned = {}

    def fake_popen(cmd, cwd, env, **kwargs):
        spawned.update(cmd=cmd, env=env)

    monkeypatch.setattr(desktop_launcher, "FROZEN", False)
    monkeypatch.setattr(desktop_launcher.subprocess, "Popen", fake_popen)
    desktop_launcher.start_server("sqlite:///x.db", 3123, bind_host="0.0.0.0")

    script = __import__("pathlib").Path(desktop_launcher.__file__).resolve()
    assert spawned["cmd"] == [sys.executable, str(script), "--_serve"]
    # _serve() takes its host and port from the environment, not argv
    assert spawned["env"]["APP_HOST"] == "0.0.0.0"
    assert spawned["env"]["APP_PORT"] == "3123"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only, like test_parent_watcher_exits_when_parent_dies (#121)",
)
def test_source_run_server_dies_with_its_launcher(tmp_path):
    """Real process pair, end to end: a fake launcher starts the server with
    start_server(), exactly as a source run does, and is then killed the way
    Command-Q kills it, with no cleanup. The server must follow it down."""
    import os as _os
    import socket
    import subprocess
    import time
    import urllib.request

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    db_url = "sqlite:///" + (tmp_path / "books.db").as_posix()

    launcher_script = tmp_path / "launcher.py"
    launcher_script.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "import desktop_launcher\n"
        f"proc = desktop_launcher.start_server({db_url!r}, {port})\n"
        "print(proc.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    launcher = subprocess.Popen(
        [sys.executable, str(launcher_script)],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    server_pid = int(launcher.stdout.readline().strip())

    def alive(pid):
        try:
            _os.kill(pid, 0)
            return True
        except OSError:
            return False

    def healthy():
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            ) as resp:
                return resp.status == 200
        except OSError:
            return False

    try:
        # The real server, answering: not a child that died on its own
        deadline = time.time() + 60
        while not healthy() and alive(server_pid) and time.time() < deadline:
            time.sleep(0.25)
        assert healthy(), "server never came up"
        # Several poll cycles with the launcher alive: the server stays up
        time.sleep(1)
        assert healthy(), "server exited while its launcher lived"

        launcher.kill()  # SIGKILL: no finally, no stop_server()
        launcher.wait()
        deadline = time.time() + 10
        while alive(server_pid) and time.time() < deadline:
            time.sleep(0.2)
        assert not alive(server_pid), "server child outlived its launcher"
    finally:
        if alive(server_pid):
            _os.kill(server_pid, 9)
        if launcher.poll() is None:
            launcher.kill()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows parent watcher (W-7)")
def test_parent_watcher_exits_when_parent_dies_on_windows(tmp_path):
    """Windows (2.22.3 gate, W-7): a launcher ended from Task Manager, or
    crashed, left the server on its port — the watcher ran on POSIX only
    and the window-close cleanup never saw TerminateProcess. The same
    process pair as the POSIX test, with liveness read from tasklist (an
    os.kill probe is a kill on Windows, #121) and the launcher ended the
    way End task ends it."""
    import subprocess
    import sys as _sys
    import time

    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "import os, sys, time\n"
        f"sys.path.insert(0, {str(root)!r})\n"
        "import desktop_launcher\n"
        "desktop_launcher._watch_parent(os.getppid())\n"
        "time.sleep(30)\n"
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, {str(child_script)!r}])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(30)\n"
    )

    def alive(pid):
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        ).stdout
        return f" {pid} " in out

    parent = subprocess.Popen(
        [_sys.executable, str(parent_script)], stdout=subprocess.PIPE, text=True
    )
    child_pid = int(parent.stdout.readline().strip())
    try:
        time.sleep(1.0)
        assert alive(child_pid), "the child died on its own"
        parent.kill()  # TerminateProcess: End task, a crash — no cleanup
        parent.wait()
        deadline = time.time() + 5
        while alive(child_pid) and time.time() < deadline:
            time.sleep(0.2)
        assert not alive(child_pid), "server child outlived its launcher parent"
    finally:
        if alive(child_pid):
            subprocess.run(
                ["taskkill", "/PID", str(child_pid), "/F"], capture_output=True
            )
        if parent.poll() is None:
            parent.kill()
