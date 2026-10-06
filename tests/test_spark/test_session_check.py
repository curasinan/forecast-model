"""scripts/spark/session_check.py: is a Gateway session possible on this machine?"""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "scripts" / "spark") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "spark"))

import session_check  # noqa: E402

SS_OUTPUT = """\
LISTEN 0      4096       127.0.0.1:4002       0.0.0.0:*
LISTEN 0      128          0.0.0.0:22         0.0.0.0:*
LISTEN 0      4096            [::]:8000          [::]:*
"""
NETSTAT_OUTPUT = """\
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING
  TCP    [::]:4002              [::]:0                 LISTENING
  TCP    192.0.2.20:52011       192.0.2.31:22          ESTABLISHED
  TCP    127.0.0.1:4002         127.0.0.1:52100        ESTABLISHED
  UDP    0.0.0.0:5353           0.0.0.0:0
"""


def _runner(outputs: dict[str, tuple[int, str]]):
    """A stand-in for subprocess.run keyed on the program name."""
    calls: list[list[str]] = []

    def run(cmd, **_kwargs):
        calls.append(list(cmd))
        code, text = outputs.get(cmd[0], (127, ""))
        return subprocess.CompletedProcess(cmd, code, stdout=text, stderr="")

    run.calls = calls
    return run


def test_listening_ports_reads_ss_on_posix(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    assert session_check.listening_ports(_runner({"ss": (0, SS_OUTPUT)})) == {4002, 22, 8000}


def test_listening_ports_reads_netstat_on_windows_and_ignores_connections(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "win32")
    ports = session_check.listening_ports(_runner({"netstat": (0, NETSTAT_OUTPUT)}))
    assert ports == {135, 4002}  # the ESTABLISHED lines are not listeners


def test_exit_0_when_nothing_listens_and_nothing_runs(monkeypatch, capsys):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    run = _runner({"ss": (0, "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"), "pgrep": (1, ""),
                   "docker": (0, "")})
    rc = session_check.main(["--port", "4002", "--process", "ibgateway", "--container", "ibgw"],
                            run=run)
    assert rc == 0
    assert "no Gateway" in capsys.readouterr().out


def test_exit_1_when_the_port_listens(monkeypatch, capsys):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    rc = session_check.main(["--port", "4002"], run=_runner({"ss": (0, SS_OUTPUT)}))
    assert rc == 1
    assert "4002" in capsys.readouterr().out


def test_exit_1_when_only_the_process_runs(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "win32")
    run = _runner({"netstat": (0, "  TCP    0.0.0.0:135   0.0.0.0:0   LISTENING\n"),
                   "tasklist": (0, "ibgateway.exe   1234 Console   1   400,000 K\n")})
    assert session_check.main(["--port", "4002", "--process", "ibgateway"], run=run) == 1


def test_exit_1_when_only_the_container_runs(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    run = _runner({"ss": (0, ""), "docker": (0, "3f2a9c1d7b10\n")})
    assert session_check.main(["--port", "4002", "--container", "ibgw"], run=run) == 1


def test_a_tool_that_cannot_be_run_is_not_read_as_all_clear(monkeypatch, capsys):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    rc = session_check.main(["--port", "4002"], run=_runner({"ss": (127, "")}))
    assert rc == 1
    assert "could not" in capsys.readouterr().out


def test_a_real_listening_socket_is_seen():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert port in session_check.listening_ports()
    assert port not in session_check.listening_ports()


def test_ipv6_listening_socket_is_seen_on_windows(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "win32")
    netstat_with_ipv6 = """\
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING
  TCP    [::]:4002              [::]:0                 LISTENING
  TCP    127.0.0.1:4002         127.0.0.1:52100        ESTABLISHED
"""
    ports = session_check.listening_ports(_runner({"netstat": (0, netstat_with_ipv6)}))
    assert 4002 in ports  # IPv6 listener should be caught
    assert 135 in ports


def test_a_real_ipv6_listening_socket_is_seen():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as server:
            server.bind(("::1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            assert port in session_check.listening_ports()
        assert port not in session_check.listening_ports()
    except OSError:
        # IPv6 not available on this host
        import pytest
        pytest.skip("IPv6 not available")


def test_exit_0_when_pgrep_matches_only_checker_and_parent(monkeypatch, capsys):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    monkeypatch.setattr(session_check.os, "getpid", lambda: 1234)
    monkeypatch.setattr(session_check.os, "getppid", lambda: 1000)
    run = _runner({"ss": (0, "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"),
                   "pgrep": (0, "1000\n1234\n"),
                   "docker": (0, "")})
    rc = session_check.main(["--port", "4002", "--process", "ibgateway", "--container", "ibgw"],
                            run=run)
    assert rc == 0
    assert "no Gateway" in capsys.readouterr().out


def test_exit_1_when_pgrep_matches_checker_and_another_process(monkeypatch):
    monkeypatch.setattr(session_check.sys, "platform", "linux")
    monkeypatch.setattr(session_check.os, "getpid", lambda: 1234)
    monkeypatch.setattr(session_check.os, "getppid", lambda: 1000)
    run = _runner({"ss": (0, "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"),
                   "pgrep": (0, "1000\n1234\n5000\n"),
                   "docker": (0, "")})
    rc = session_check.main(["--port", "4002", "--process", "ibgateway"], run=run)
    assert rc == 1
