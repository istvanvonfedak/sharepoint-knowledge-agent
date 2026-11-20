"""The sandbox service's isolation and limits, against a running service (it needs Linux privileges and nsjail).

Skipped unless SANDBOX_TEST_URL is set. Run them from the sandbox's own network, so "no network" is tested where it
matters (the mcp-server is reachable from there, and a command must not reach it):

    docker compose --env-file .env -f infra/docker-compose.yml --profile sandbox up -d --build sandbox
    printf 'FROM python:3.13-slim\\nRUN pip install pytest httpx\\n' | docker build -t sandbox-tests -
    export SANDBOX_API_KEY=$(grep '^SANDBOX_API_KEY=' .env | cut -d= -f2-)
    docker run --rm --network infra_sandbox -e SANDBOX_TEST_URL=http://sandbox:8000 -e SANDBOX_API_KEY \\
        -v "$PWD:/repo" -w /repo sandbox-tests pytest -p no:cacheprovider -o addopts= apps/sandbox/tests

(The sandbox network has no route out, so the test image brings pytest with it.)
"""
import os
import time
import uuid

import pytest

httpx = pytest.importorskip("httpx")

URL = os.environ.get("SANDBOX_TEST_URL")
KEY = os.environ.get("SANDBOX_API_KEY", "")
pytestmark = pytest.mark.skipif(not URL, reason="SANDBOX_TEST_URL is not set")


@pytest.fixture
def client():
    with httpx.Client(base_url=URL, headers={"Authorization": f"Bearer {KEY}"}, timeout=60) as c:
        yield c


@pytest.fixture
def session(client):
    sid = uuid.uuid4().hex
    yield sid
    client.delete(f"/sessions/{sid}")


def upload(client, sid, **files):
    r = client.post(f"/sessions/{sid}/files", json={"files": [{"name": n, "content": c} for n, c in files.items()]})
    assert r.status_code == 200, r.text
    return r


def run(client, sid, command, timeout=5):
    r = client.post(f"/sessions/{sid}/execute", json={"command": command, "timeout": timeout})
    assert r.status_code == 200, r.text
    return r.json()


def test_runs_commands_over_the_sessions_files_in_work(client, session):
    upload(client, session, **{"S1.txt": "=== Page 56 ===\nCrankcase\ncapacity is 7 quarts with filter\n"})
    out = run(client, session, "pwd; rg -n -i -U 'crankcase\\s+capac' S*.txt; section S1.txt 3")
    assert out["output"] == "/work\n2:Crankcase\n3:capacity is 7 quarts with filter\n3: === Page 56 ===\n"
    assert out["exit_code"] == 0 and not out["timed_out"]
    assert run(client, session, "grep -c nothing S1.txt")["exit_code"] == 1


def test_a_session_cannot_see_another_sessions_files(client, session):
    other = uuid.uuid4().hex
    try:
        secret = uuid.uuid4().hex
        upload(client, other, **{"S1.txt": secret})
        upload(client, session, **{"S1.txt": "mine"})
        # The command names the secret and the other id only reversed, so they can't come back from the command line.
        out = run(client, session, f"id -u; ls -a / /tmp; o=$(echo {other[::-1]} | rev); "
                                   "cat ../$o/S1.txt /tmp/$o/S1.txt 2>/dev/null; "
                                   f"grep -rs \"$(echo {secret[::-1]} | rev)\" / --exclude-dir=proc; true")["output"]
        assert secret not in out and other not in out
        assert run(client, other, "id -u")["output"] != out.splitlines()[0] + "\n"  # each session its own uid
    finally:
        client.delete(f"/sessions/{other}")


def test_commands_have_no_network_at_all(client, session):
    upload(client, session, **{"S1.txt": "x"})
    probe = ("import socket\n"
             "for host, port in [('mcp-server', 8080), ('sandbox', 8000), ('127.0.0.1', 8000), ('1.1.1.1', 80)]:\n"
             "    try:\n"
             "        socket.create_connection((host, port), 2); print('REACHED', host)\n"
             "    except OSError as e: print('blocked', host, type(e).__name__)\n")
    out = run(client, session, f"python3 -c \"{probe}\"; ls /sys/class/net 2>/dev/null")["output"]
    assert "REACHED" not in out and out.count("blocked") == 4


def test_output_never_shows_the_session_id_or_the_services_key(client, session):
    upload(client, session, **{"S1.txt": "x"})
    out = run(client, session, "pwd; ls -la / /tmp /work; cat /proc/self/mountinfo; hostname; env; "
                               "cat /proc/1/environ /proc/1/cmdline; ls /nonexistent; cd /nowhere; "
                               "cat /proc/*/environ 2>&1 | tr '\\0' '\\n'")["output"]
    assert session not in out
    assert not KEY or KEY not in out
    assert "/work" in out


def test_everything_outside_work_is_read_only_and_root_files_unreadable(client, session):
    upload(client, session, **{"S1.txt": "x"})
    out = run(client, session, "touch /usr/x /etc/x /x 2>&1; cat /etc/shadow 2>&1; id -u; echo ok > /work/new && cat /work/new")
    assert out["output"].count("Read-only file system") + out["output"].count("Permission denied") >= 4
    assert out["output"].splitlines()[-2] != "0"  # not root
    assert out["output"].endswith("ok\n")


def test_uploads_replace_links_and_fifos_planted_in_work(client, session):
    upload(client, session, **{"S1.txt": "x"})
    run(client, session, "ln -s /etc/hostname S2.txt; mkfifo S3.txt")
    started = time.monotonic()
    upload(client, session, **{"S2.txt": "two", "S3.txt": "three"})
    assert time.monotonic() - started < 5  # a FIFO would have blocked a plain open()
    assert run(client, session, "cat S2.txt S3.txt; test -L S2.txt || echo plain")["output"] == "twothreeplain\n"


def test_limits_hold(client, session):
    upload(client, session, **{"S1.txt": "x"})
    started = time.monotonic()
    out = run(client, session, "sleep 30", timeout=2)
    assert out["timed_out"] and time.monotonic() - started < 6
    big = run(client, session, "head -c 10000000 /dev/zero | tr '\\0' a")
    assert len(big["output"]) == 64 * 1024 and big["omitted_bytes"] == 10_000_000 - 64 * 1024
    out = run(client, session, "head -c 60000000 /dev/zero > big; ls -l big | awk '{print $5}'")
    assert int(out["output"].split()[-1]) <= 50 * 2**20  # file size limit
    out = run(client, session, "python3 -c 'x = bytearray(2 * 1024**3)' 2>&1 | tail -1")
    assert "MemoryError" in out["output"]
    started = time.monotonic()
    run(client, session, "for i in $(seq 200); do sleep 2 & done 2>/dev/null; wait", timeout=5)
    assert time.monotonic() - started < 10  # a burst of processes stays within the wall clock
    out = run(client, session, "head -c 300000000 /dev/zero > /work/fill 2>&1; ls -l /work/fill | awk '{print $5}'")
    assert int(out["output"].split()[-1]) <= 160 * 2**20  # the session's disk is capped


def test_requests_are_checked(client, session):
    assert httpx.post(f"{URL}/sessions/{session}/files", json={"files": []}, headers={"Authorization": "Bearer no"}).status_code == 401
    assert client.post("/sessions/not-a-uuid/files", json={"files": []}).status_code == 400
    for name in ("../x", "-rf", ".hidden", "a/b", ""):
        r = client.post(f"/sessions/{session}/files", json={"files": [{"name": name, "content": "x"}]})
        assert r.status_code == 400, name
    assert client.post(f"/sessions/{uuid.uuid4().hex}/execute", json={"command": "true"}).status_code == 404
    assert client.post(f"/sessions/{session}/execute", json={"command": "true", "timeout": 60}).status_code == 400


def test_deleted_sessions_are_gone(client, session):
    upload(client, session, **{"S1.txt": "x"})
    assert client.delete(f"/sessions/{session}").status_code == 200
    assert client.post(f"/sessions/{session}/execute", json={"command": "true"}).status_code == 404
    other = uuid.uuid4().hex
    upload(client, other, **{"S1.txt": "y"})
    try:  # the folder went with the session: a new one sees nothing of it
        assert session not in run(client, other, "ls -a /tmp /")["output"]
    finally:
        client.delete(f"/sessions/{other}")
