"""sandbox: runs the agent's shell commands over one sharepoint_ask call's files, with no network and no secrets.

    POST   /sessions/{id}/files    {"files": [{"name", "content"}]}  -> {"ok"}   (creates the session)
    POST   /sessions/{id}/execute  {"command", "timeout"}            -> {"output", "omitted_bytes", "exit_code", "timed_out"}
    DELETE /sessions/{id}                                            -> {"ok"}
    GET    /health                                                   -> {"ok"}

The same three operations as Azure Container Apps dynamic sessions (docs/code-interpreter.md). Callers authenticate
with SANDBOX_API_KEY. Commands are model-written and the model reads untrusted documents, so they are treated as
hostile: each runs under nsjail as its session's own uid from a pool, in fresh mount, pid, network (no interface at
all), ipc and uts namespaces, with only its session's folder writable, seen as /work, and its own empty /tmp and /proc.
Each session's folder is its own tmpfs, so its size is capped and /proc/self/mountinfo shows "/work", never the id.
The service needs privileges for this (docs/code-interpreter.md, "Privileges"): run its container under gVisor.
"""
import asyncio
import hmac
import logging
import math
import os
import re
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, field_validator
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger(__name__)

API_KEY = os.environ.get("SANDBOX_API_KEY", "")
if not API_KEY and os.environ.get("SANDBOX_ALLOW_ANONYMOUS", "").lower() != "true":
    raise RuntimeError("Set SANDBOX_API_KEY (or SANDBOX_ALLOW_ANONYMOUS=true for a closed test setup)")
ROOT = Path(os.environ.get("SANDBOX_ROOT", "/tmp"))  # a tmpfs: a restart leaves nothing
UIDS = range(20000, 21000)  # one per live session
# Session folders are tmpfs, held in memory. All of it must fit the container's memory limit
# (infra/docker-compose.yml, 4 GB): 10 sessions x 160 MB, plus 4 commands x 512 MB, is 3.6 GB.
MAX_SESSIONS = int(os.environ.get("SANDBOX_MAX_SESSIONS", "10"))
SESSION_FILE_BYTES = 112 * 1024 * 1024  # uploaded files per session
SESSION_DISK_MB = 160  # the session's tmpfs: its files plus what its commands write
IDLE_SECONDS = 15 * 60  # a session not used for this long is a crashed request: removed
DEFAULT_TIMEOUT, MAX_TIMEOUT = 5.0, 15.0  # wall clock per command, seconds
MAX_OUTPUT_BYTES = 64 * 1024  # kept per command; the rest is counted, not stored
CPU_SECONDS, MEMORY_MB, PROCESSES, FILE_MB, OPEN_FILES, TMP_MB = 10, 512, 64, 50, 256, 64
MAX_COMMANDS = int(os.environ.get("SANDBOX_MAX_COMMANDS", "4"))  # running at once, all sessions
NSJAIL = os.environ.get("SANDBOX_NSJAIL", "nsjail")
_ID = re.compile(r"[0-9a-f]{32}")  # the mcp-server's uuid4().hex
_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")  # no "/", and no leading "-" or "."
# What a command sees besides /work: the image's programs and libraries, read-only.
_READ_ONLY = [p for p in ("/usr", "/bin", "/lib", "/lib64", "/etc") if os.path.exists(p)]
_DEVICES = ["/dev/null", "/dev/zero", "/dev/urandom"]


class UploadFile(BaseModel):
    name: str
    content: str

    @field_validator("name")
    @classmethod
    def _plain_name(cls, name: str) -> str:
        if not _NAME.fullmatch(name):  # a whole-string match: pydantic's `pattern` only searches
            raise ValueError("file names are letters, digits, '_', '.' and '-', not starting with '.' or '-'")
        return name


class Upload(BaseModel):
    files: list[UploadFile] = Field(max_length=500)


class Execute(BaseModel):
    command: str = Field(min_length=1, max_length=20_000)
    timeout: float = Field(DEFAULT_TIMEOUT, gt=0, le=MAX_TIMEOUT)


@dataclass
class Session:
    id: str
    uid: int
    path: Path
    used: float = field(default_factory=time.monotonic)
    sizes: dict[str, int] = field(default_factory=dict)  # uploaded file -> bytes
    running: set[asyncio.subprocess.Process] = field(default_factory=set)


sessions: dict[str, Session] = {}
_lock = asyncio.Lock()
_commands = asyncio.Semaphore(MAX_COMMANDS)


async def _run(*args: str) -> None:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError(f"{args[0]} failed: {err.decode(errors='replace')[:300]}")


async def _open(session_id: str) -> Session:
    """The session, created on first use: a tmpfs of its own, owned by a uid no other live session has."""
    async with _lock:
        if session_id in sessions:
            return sessions[session_id]
        if len(sessions) >= MAX_SESSIONS:
            raise OverflowError("too many sessions")
        uid = next(u for u in UIDS if u not in {s.uid for s in sessions.values()})
        path = ROOT / session_id
        path.mkdir(mode=0o700)
        try:
            await _run("mount", "-t", "tmpfs", "-o",
                       f"size={SESSION_DISK_MB}m,mode=0700,uid={uid},gid={uid},nosuid,nodev,noexec", "tmpfs", str(path))
        except Exception:
            path.rmdir()
            raise
        sessions[session_id] = session = Session(session_id, uid, path)
        return session


async def _close(session: Session) -> None:
    for proc in list(session.running):
        proc.kill()
    try:
        await _run("umount", "-l", str(session.path))
        session.path.rmdir()
    except Exception:
        log.exception("removing a session's folder failed")  # logged by uid, never by id
        shutil.rmtree(session.path, ignore_errors=True)


def _authorized(request: Request) -> bool:
    if not API_KEY:
        return True
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
    return hmac.compare_digest(supplied.encode(), API_KEY.encode())


async def _parse(request: Request, model: type[BaseModel]):
    if not _authorized(request):
        return None, None, JSONResponse({"error": "unauthorized"}, 401)
    session_id = request.path_params["id"]
    if not _ID.fullmatch(session_id):
        return None, None, JSONResponse({"error": "bad session id"}, 400)
    if model is None:
        return session_id, None, None
    try:
        body = model.model_validate(await request.json())
    except ValueError as e:
        return None, None, JSONResponse({"error": str(e)[:500]}, 400)
    return session_id, body, None


def _write(session: Session, name: str, content: bytes) -> None:
    """Written as root into a folder the session's uid controls: never through a link or FIFO it planted there. A new
    file with a random name, then renamed over whatever `name` is."""
    tmp = session.path / f".upload-{secrets.token_hex(8)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    os.rename(tmp, session.path / name)


async def upload(request: Request) -> JSONResponse:
    session_id, body, error = await _parse(request, Upload)
    if error:
        return error
    files = {f.name: f.content.encode() for f in body.files}
    try:
        session = await _open(session_id)
    except OverflowError:
        return JSONResponse({"error": "too many sessions; try again later"}, 429)
    if sum({**session.sizes, **{n: len(c) for n, c in files.items()}}.values()) > SESSION_FILE_BYTES:
        return JSONResponse({"error": f"a session holds at most {SESSION_FILE_BYTES // 2**20} MB of files"}, 413)
    try:
        for name, content in files.items():
            await asyncio.to_thread(_write, session, name, content)
            session.sizes[name] = len(content)
    except OSError as e:  # the tmpfs is full (its commands wrote a lot), or a name was taken by a folder
        return JSONResponse({"error": f"writing the file failed: {e.strerror}"}, 507)
    session.used = time.monotonic()
    return JSONResponse({"ok": True})


def _seconds(timeout: float) -> int:
    """nsjail's time limit is whole seconds: rounded up, so a command is never stopped before the time it was given."""
    return max(1, math.ceil(timeout))


def _jail(session: Session, command: str, timeout: float) -> list[str]:
    """nsjail's arguments for one command. No environment is passed on but these variables (nsjail clears the rest),
    so the service's own key never reaches a command."""
    uid = str(session.uid)
    mounts = [a for p in _READ_ONLY for a in ("-R", p)] + [a for d in _DEVICES for a in ("-B" if d == "/dev/null" else "-R", d)]
    return [
        NSJAIL, "-Mo", "--really_quiet", "--disable_clone_newuser", "--iface_no_lo",
        "-u", uid, "-g", uid, "--hostname", "sandbox", "--cwd", "/work",
        *mounts, "-B", f"{session.path}:/work", "-m", f"none:/tmp:tmpfs:size={TMP_MB * 2**20}",
        "--time_limit", str(_seconds(timeout)), "--rlimit_cpu", str(CPU_SECONDS), "--rlimit_as", str(MEMORY_MB),
        "--rlimit_nproc", str(PROCESSES), "--rlimit_fsize", str(FILE_MB), "--rlimit_nofile", str(OPEN_FILES),
        "--env", "PATH=/usr/local/bin:/usr/bin:/bin", "--env", "HOME=/work", "--env", "LANG=C.UTF-8",
        "--", "/bin/bash", "-c", command,
    ]


async def execute(request: Request) -> JSONResponse:
    session_id, body, error = await _parse(request, Execute)
    if error:
        return error
    session = sessions.get(session_id)
    if session is None:
        return JSONResponse({"error": "no such session"}, 404)
    session.used = time.monotonic()
    async with _commands:
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(*_jail(session, body.command, body.timeout),
                                                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        session.running.add(proc)
        kept, total = bytearray(), 0

        async def drain() -> None:
            nonlocal total
            while chunk := await proc.stdout.read(65536):
                total += len(chunk)
                kept.extend(chunk[:MAX_OUTPUT_BYTES - len(kept)])

        try:
            # nsjail's own time limit ends the command; this one only guards against nsjail itself hanging.
            await asyncio.wait_for(asyncio.gather(drain(), proc.wait()), body.timeout + 5)
        except TimeoutError:
            proc.kill()
            await proc.wait()
        finally:
            session.running.discard(proc)
    elapsed = time.monotonic() - started
    session.used = time.monotonic()
    return JSONResponse({"output": kept.decode(errors="replace"), "omitted_bytes": total - len(kept),
                         "exit_code": proc.returncode, "timed_out": proc.returncode != 0 and elapsed >= _seconds(body.timeout)})


async def delete(request: Request) -> JSONResponse:
    session_id, _, error = await _parse(request, None)
    if error:
        return error
    async with _lock:
        session = sessions.pop(session_id, None)
    if session is not None:
        await _close(session)
    return JSONResponse({"ok": True})


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def _sweep() -> None:
    while True:
        await asyncio.sleep(60)
        async with _lock:
            idle = [s for s in sessions.values() if time.monotonic() - s.used > IDLE_SECONDS and not s.running]
            for s in idle:
                del sessions[s.id]
        for s in idle:
            log.info("removing a session idle for %d s (uid %d)", IDLE_SECONDS, s.uid)
            await _close(s)


@asynccontextmanager
async def lifespan(app: Starlette):
    sweep = asyncio.create_task(_sweep())
    try:
        yield
    finally:
        sweep.cancel()


app = Starlette(routes=[
    Route("/sessions/{id}/files", upload, methods=["POST"]),
    Route("/sessions/{id}/execute", execute, methods=["POST"]),
    Route("/sessions/{id}", delete, methods=["DELETE"]),
    Route("/health", health, methods=["GET"]),
], lifespan=lifespan)
