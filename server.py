import asyncio
import base64
import os
import signal
import re
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
import json
import logging
import uvicorn
import time
import traceback
from pathlib import Path

# =============================================================================
# Leak Lean Daemon (verify_full_script)
# -----------------------------------------------------------------------------
# A single-purpose service: compile a whole Lean 4 + Mathlib script and report,
# DETERMINISTICALLY, whether it checks. It is the source of truth for "is this
# proof true under the toolchain". Nothing else — no interactive Pantograph
# proof state, no heuristics on the proof text.
#
# WHY THIS REWRITE (the old daemon's false positives):
#   The old code decided a compile was done via two independently-latching flags
#   (a version-matched publishDiagnostics + an empty $/lean/fileProgress) and
#   OVERWROTE the diagnostics on each message. But Lean's server DEBOUNCES and
#   can DROP publishDiagnostics, and streams them incrementally — so that loop
#   could exit on a stale/empty snapshot and miss the real `sorry`/error
#   diagnostics, reporting "100% verified" for a proof full of holes. It was also
#   nondeterministic (same script -> ✅ one run, ❌ the next).
#
#   The fix is Lean's own synchronisation primitive, exactly as the reference
#   client (Lean/Data/Lsp/Ipc.lean `collectDiagnostics`) does it:
#     1. didOpen/didChange to a fresh version N
#     2. send request  textDocument/waitForDiagnostics { uri, version = N }
#     3. MERGE every publishDiagnostics for our uri (respecting `isIncremental?`)
#        until the response to that request arrives — the server only replies
#        once ALL diagnostics for version >= N have been emitted.
#     4. verdict from the merged diagnostic set.
#   Validated locally against real `lake serve` (Lean 4.29.1 + Mathlib): correct
#   and identical across repeated runs.
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("leak-daemon")


class WorkerCrashed(Exception):
    """Lean's per-file worker aborted (native stack overflow / bug) while the
    `lake serve` watchdog stayed alive. Recoverable: respawn the file worker."""


# stderr markers that mean the file worker hard-aborted its OS process (as
# opposed to a graceful "(kernel) deep recursion detected" diagnostic, which the
# worker survives). Seeing any of these lets us fail the in-flight read FAST
# instead of blocking the whole VERIFY_TIMEOUT for a reply that will never come.
_CRASH_MARKERS = ("stack overflow", "aborting", "panic", "segmentation fault", "libc++abi")
# JSON-RPC error codes the Lean watchdog returns when the file worker died:
#   -32902 workerCrashed, -32900 workerExited. Watchdog is still up → recover.
_WORKER_DEAD_CODES = (-32902, -32900)

mcp = FastMCP(
    "Leak-Daemon",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

# How long a single (warm) verify may run before we give up (Lean elaboration
# for a hard proof can be slow; this is the backstop, not the normal path).
VERIFY_TIMEOUT = 180.0
# What every script compiles against: the Tengoku tree's root. Newline-separated
# import lines; the first one also replaces a legacy `import Mathlib`.
TENGOKU_IMPORTS = os.environ.get("TENGOKU_IMPORTS", "import Tengoku").strip()
# The tree checkout the daemon serves (its own lakefile, its own build cache).
TENGOKU_DIR = os.environ.get("LEAN_PROJECT_PATH", ".")
# The FIRST compile has to load all of Mathlib into the elaborator. On a small
# shared CPU (e.g. HF cpu-basic) that cold load can take several minutes, so the
# one-off warmup gets a much larger ceiling. If this is too small, the warmup
# times out mid-load, the next verify's didChange restarts the load, and Mathlib
# never becomes resident — which is exactly what made the daemon look "stuck".
WARMUP_TIMEOUT = 1200.0


class LeanCompilerDaemon:
    """One long-lived `lake serve` LSP subprocess, driven over stdio."""

    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.project_dir = os.environ.get("LEAN_PROJECT_PATH", ".")
        self.lock = asyncio.Lock()          # verify calls are serialised
        # Tripped by the stderr watcher when the file worker prints a native
        # abort, so the read loop can bail out immediately instead of hanging.
        self._crash_event = asyncio.Event()
        self.version = 1                    # monotonic LSP document version
        self.request_id = 1000              # monotonic LSP request id
        self.verify_count = 0               # for log correlation
        self.sandbox_path = Path(self.project_dir).resolve() / "virtual_sandbox.lean"
        self.uri = self.sandbox_path.as_uri()
        self._is_file_open = False
        logger.info(f"🔧 [INIT] project_dir={self.project_dir!r}  uri={self.uri}")
        try:
            with open(self.sandbox_path, "a"):
                pass
        except Exception as e:
            logger.error(f"❌ [INIT] could not touch sandbox file: {e}")

    # ---- process lifecycle -------------------------------------------------
    async def boot(self):
        if self.process and self.process.returncode is None:
            return
        logger.info("🚨 [BOOT] starting `lake serve` subprocess...")
        # Its own process group: `lake serve` forks `lean` workers that hold the
        # pipes, so a restart must kill the whole group or `wait()` never returns.
        self.process = await asyncio.create_subprocess_exec(
            "lake", "serve",
            cwd=self.project_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        asyncio.create_task(self._log_stderr())
        await self._send("initialize", {
            "processId": os.getpid(),
            "rootUri": Path(self.project_dir).resolve().as_uri(),
            "capabilities": {"textDocument": {"synchronization": {"change": 1}}},
        }, msg_id=0)
        while True:
            msg = await asyncio.wait_for(self._read(), timeout=60.0)
            if msg.get("id") == 0:
                logger.info("✅ [BOOT] initialize handshake complete")
                break
        await self._send("initialized", {})
        self._is_file_open = False
        logger.info("✅ [BOOT] daemon online")

    async def _log_stderr(self):
        if not self.process or not self.process.stderr:
            return
        while True:
            try:
                line = await self.process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                logger.info(f"🛰️  [LSP-STDERR] {text}")
                low = text.lower()
                if any(k in low for k in _CRASH_MARKERS):
                    # The file worker just hard-aborted. Signal the read loop so
                    # it stops waiting on a reply the dead worker won't send.
                    self._crash_event.set()
                    logger.error("💀 [LSP-STDERR] file worker aborted — arming self-heal")
            except Exception:
                break

    # ---- raw JSON-RPC over stdio ------------------------------------------
    async def _send(self, method: str, params: dict, msg_id: int | None = None):
        if not self.process or not self.process.stdin:
            raise BrokenPipeError("LSP stdin is dead")
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if msg_id is not None:
            msg["id"] = msg_id
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        self.process.stdin.write(header + body)
        await self.process.stdin.drain()
        logger.info(f"📤 [LSP-OUT] {method}" + (f" (id={msg_id})" if msg_id is not None else ""))

    async def _read(self) -> dict:
        if not self.process or not self.process.stdout:
            raise EOFError("LSP stdout is dead")
        content_length = 0
        while True:
            line_bytes = await self.process.stdout.readline()
            if not line_bytes:
                raise EOFError("LSP EOF (subprocess exited)")
            line = line_bytes.decode("utf-8", "replace").strip()
            if not line and content_length > 0:
                break
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":")[1].strip())
        body = await self.process.stdout.readexactly(content_length)
        return json.loads(body.decode("utf-8"))

    async def _await_message(self, timeout: float) -> dict:
        """Read the next LSP message, but bail out FAST (WorkerCrashed) if the
        file worker aborts mid-read. A dead worker never replies to our pending
        waitForDiagnostics, so a plain read would block for the full timeout —
        exactly the 3-minute hang from the 2026-07-12 outage. We race the read
        against the crash signal the stderr watcher sets on 'Aborting.'."""
        read_fut = asyncio.ensure_future(self._read())
        crash_fut = asyncio.ensure_future(self._crash_event.wait())
        try:
            done, _ = await asyncio.wait(
                {read_fut, crash_fut}, timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED)
            if read_fut in done:
                return read_fut.result()          # normal message (or raises EOF/etc.)
            if crash_fut in done:
                raise WorkerCrashed("file worker aborted (native stack overflow)")
            raise TimeoutError(f"verify exceeded {timeout:.0f}s")
        finally:
            for f in (read_fut, crash_fut):
                if not f.done():
                    f.cancel()
                try:
                    await f
                except BaseException:
                    pass  # swallow the cancellation / already-handled exception

    async def _recover_worker(self, n: int):
        """A file worker crashed but the `lake serve` WATCHDOG is usually still
        alive (that's what returns -32902 workerCrashed). Tear the dead worker
        down with didClose so the NEXT verify re-opens a fresh one (~Mathlib
        re-import, ~80s) — far cheaper than a full daemon reboot, and it un-poisons
        the session so healthy scripts stop failing. If the watchdog itself is
        gone, force a full reboot on the next call instead."""
        self._crash_event.clear()
        if not self.process or self.process.returncode is not None:
            self.process = None
            self._is_file_open = False
            logger.error(f"♻️  [#{n}] watchdog gone — full reboot armed for next call")
            return
        try:
            if self._is_file_open:
                await self._send("textDocument/didClose",
                                 {"textDocument": {"uri": self.uri}})
        except Exception:
            self.process = None            # watchdog stdin dead → reboot next call
        self._is_file_open = False
        logger.info(f"♻️  [#{n}] file-worker respawn armed (didClose sent; next verify re-opens)")

    # ---- the one public operation -----------------------------------------
    async def verify_script(self, script: str, timeout: float = VERIFY_TIMEOUT) -> str:
        async with self.lock:
            self.verify_count += 1
            n = self.verify_count
            t0 = time.time()

            if not self.process or self.process.returncode is not None:
                logger.info(f"♻️  [#{n}] LSP not running — booting")
                await self.boot()

            self.version += 1
            ver = self.version
            # The environment is the Tengoku tree (one self-contained library
            # seeded from Mathlib); its root import is injected unless the
            # script already imports from the tree. `import Mathlib` in a
            # legacy script is rewritten to the tree's root.
            # Module-level imports of the libraries the tree was seeded from map
            # onto the tree the same way the seed mapped them (Mathlib.X ->
            # Tengoku.X, Batteries.X -> Tengoku.Std.X, ...); `import Mathlib`
            # itself becomes the tree's root import.
            seed_map = (("Mathlib", "Tengoku"), ("Batteries", "Tengoku.Std"), ("Aesop", "Tengoku.Tactic.Aesop"),
                        ("Qq", "Tengoku.Meta.Qq"), ("ProofWidgets", "Tengoku.Widgets"), ("Plausible", "Tengoku.Testing.Random"),
                        ("LeanSearchClient", "Tengoku.Search.LeanSearchClient"), ("ImportGraph", "Tengoku.Meta.ImportGraph"),
                        ("Cli", "Tengoku.Meta.Cli"))

            def _map_seed_import(m):
                root, rest = m.group(2), m.group(3)
                for old, new in seed_map:
                    if root == old:
                        if old == "Mathlib" and not rest:
                            return TENGOKU_IMPORTS.split("\n")[0]
                        return f"{m.group(1)}{new}{rest}"
                return m.group(0)

            script = re.sub(r"^([ \t]*import[ \t]+)([A-Za-z_]\w*)((?:\.[\w«»]+)*)[ \t]*$", _map_seed_import, script, flags=re.M)
            full_text = (
                script if re.search(r"^[ \t]*import[ \t]+Tengoku\b", script, re.M) else f"{TENGOKU_IMPORTS}\n\n{script}"
            ).strip() + "\n\n"

            preview = " ".join(script.strip().split())[:200]
            logger.info("─" * 60)
            logger.info(f"🔎 [#{n}] VERIFY  version={ver}  chars={len(script)}")
            logger.info(f"🔎 [#{n}] script: {preview}{'…' if len(script) > 200 else ''}")

            # Fresh crash slate: a set flag left over from a prior worker abort
            # must not instantly trip this (already recovered) verify.
            self._crash_event.clear()
            try:
                if not self._is_file_open:
                    await self._send("textDocument/didOpen", {"textDocument": {
                        "uri": self.uri, "languageId": "lean",
                        "version": ver, "text": full_text}})
                    self._is_file_open = True
                else:
                    await self._send("textDocument/didChange", {
                        "textDocument": {"uri": self.uri, "version": ver},
                        "contentChanges": [{"text": full_text}]})

                # Lean's synchronisation primitive: this request only gets a
                # response once ALL diagnostics for `ver` have been emitted.
                self.request_id += 1
                wf_id = self.request_id
                await self._send("textDocument/waitForDiagnostics",
                                 {"uri": self.uri, "version": ver}, msg_id=wf_id)

                merged: list | None = None   # accumulated diagnostics for `ver`
                publishes = 0
                while True:
                    if time.time() - t0 > timeout:
                        raise TimeoutError(f"verify exceeded {timeout:.0f}s")

                    msg = await self._await_message(timeout)

                    # response to OUR waitForDiagnostics -> done for this version
                    if msg.get("id") == wf_id and "method" not in msg:
                        if "error" in msg:
                            err = msg["error"] or {}
                            if err.get("code") in _WORKER_DEAD_CODES:
                                # The watchdog told us the file worker crashed —
                                # recoverable, not a genuine proof failure.
                                raise WorkerCrashed(f"waitForDiagnostics: {err}")
                            raise RuntimeError(
                                f"waitForDiagnostics error: {msg['error']}")
                        logger.info(f"🏁 [#{n}] waitForDiagnostics returned "
                                    f"({publishes} publishes merged)")
                        break

                    method = msg.get("method")
                    if method == "textDocument/publishDiagnostics":
                        p = msg.get("params", {})
                        if p.get("uri") == self.uri:
                            publishes += 1
                            incremental = bool(p.get("isIncremental", False))
                            diags = p.get("diagnostics", [])
                            if merged is None or not incremental:
                                merged = list(diags)          # replace
                            else:
                                merged = merged + list(diags)  # append
                            logger.info(f"📥 [#{n}] publishDiagnostics "
                                        f"(incremental={incremental}, "
                                        f"n={len(diags)}, total={len(merged)})")
                    elif method == "$/lean/fileProgress":
                        p = msg.get("params", {})
                        if p.get("textDocument", {}).get("uri") == self.uri:
                            remaining = len(p.get("processing", []))
                            if remaining:
                                logger.info(f"🏗️  [#{n}] compiling "
                                            f"({remaining} ranges)…")

                diags = merged or []
                # severity 1 = error, 2 = warning. `sorry`/`admit` surface as a
                # warning ("declaration uses `sorry`"), so BOTH count as failure:
                # the promise is a hole-free proof, and a warning here is a hole.
                bad = [d for d in diags if d.get("severity", 1) in (1, 2)]

                # `apply?` / `exact?` / `rw?` / `simp?` report their hits as
                # severity-3 INFORMATION ("Try this: exact Nat.add_zero n"),
                # which this filter dropped — so a caller could run apply? and
                # never read the answer. They are ADVISORY: collected here, they
                # never touch the pass/fail verdict, and the format deliberately
                # does NOT match the "Line N (Error|Warning):" grammar callers
                # parse, so no existing consumer changes behaviour.
                hints = []
                for d in diags:
                    if d.get("severity") != 3:
                        continue
                    m = " ".join(str(d.get("message", "")).split())
                    if not m.lower().startswith("try this"):
                        continue
                    ln = d.get("range", {}).get("start", {}).get("line", 0) + 1
                    hints.append(f"Suggestion @ line {ln}: {m}")
                hint_block = ("\n[[LEAK_SUGGESTIONS]]\n" + "\n".join(hints)) if hints else ""
                if hints:
                    logger.info(f"💡 [#{n}] {len(hints)} tactic suggestion(s) harvested")

                elapsed = int((time.time() - t0) * 1000)
                if not bad:
                    logger.info(f"✅ [#{n}] VERIFIED in {elapsed}ms — no errors/warnings")
                    # Callers persist "the script that got proved" as the final
                    # certificate — but they only ever see what THEY sent
                    # (`script`), not `full_text` (what actually got compiled,
                    # import injected). If the caller's script had no import, a
                    # naive caller stores an incomplete artifact that silently
                    # depended on this daemon's injection to ever have compiled.
                    # Ending the return with a machine-parseable, escaping-proof
                    # (base64, no embedded newlines/quotes) marker lets a caller
                    # recover the EXACT compiled text instead of re-deriving it.
                    # The leading sentence is unchanged, so any caller matching
                    # only on "Compilation Successful"/"100% verified" — the
                    # existing contract — keeps working without modification.
                    normalized_b64 = base64.b64encode(full_text.encode("utf-8")).decode("ascii")
                    return (
                        "✅ Compilation Successful! The proof is 100% verified.\n"
                        f"[[LEAK_NORMALIZED_SCRIPT_B64:{normalized_b64}]]"
                        f"{hint_block}"
                    )

                lines = []
                for d in bad:
                    sev = "Error" if d.get("severity", 1) == 1 else "Warning"
                    ln = d.get("range", {}).get("start", {}).get("line", 0) + 1
                    m = " ".join(str(d.get("message", "")).split())
                    lines.append(f"Line {ln} ({sev}): {m}")
                    logger.info(f"❌ [#{n}] {lines[-1]}")
                logger.info(f"❌ [#{n}] FAILED in {elapsed}ms — {len(bad)} issue(s)")
                return "❌ Compilation Failed:\n" + "\n".join(lines) + hint_block

            except WorkerCrashed as e:
                # The file worker aborted but the daemon is NOT dead: respawn the
                # worker so the next request works instead of failing forever.
                logger.error(f"💥 [#{n}] worker crash: {e}")
                await self._recover_worker(n)
                return ("❌ Verification Error: the Lean worker crashed (native "
                        "stack overflow or bug) while checking this script. The "
                        "daemon has self-healed — retry a different/lighter script. "
                        f"Detail: {e}")

            except Exception as e:
                logger.error(f"💥 [#{n}] {traceback.format_exc()}")
                # A dead pipe/EOF means the LSP crashed — force a reboot next call.
                if isinstance(e, (EOFError, BrokenPipeError)):
                    self.process = None
                    self._is_file_open = False
                    logger.error(f"♻️  [#{n}] LSP marked for reboot")
                return f"❌ Verification Error: {e}"


fast_compiler = LeanCompilerDaemon()


# =============================================================================
# MCP TOOL
# =============================================================================
@mcp.tool()
async def verify_full_script(script: str) -> str:
    """
    Compile a whole Lean 4 script and report whether it checks under Mathlib.

    Returns "✅ Compilation Successful!" only if the toolchain reports NO errors
    and NO warnings (a `sorry`/`admit` is a warning and therefore fails). On any
    problem it returns "❌ Compilation Failed:" followed by each Line/severity/message.

    IMPORTANT: the Tengoku tree's root import is injected for you — do not add
    imports (a legacy `import Mathlib` line is rewritten to the tree's root),
    and assume exactly what the tree contains is available: everything it was
    seeded with (Mathlib and what Mathlib pulled in) plus every verified
    addition since (`Tengoku.<Library>.*`).

    On success the return also ends with a machine-parseable marker
    `[[LEAK_NORMALIZED_SCRIPT_B64:<base64>]]` carrying the EXACT text that was
    compiled (your script with the tree import injected if it was missing).
    A caller that persists "the proof" should decode and store this instead of
    its own `script` argument, so the saved artifact is self-contained and
    doesn't silently depend on this daemon's injection to compile standalone.
    """
    try:
        return await fast_compiler.verify_script(script)
    except Exception:
        err = traceback.format_exc()
        logger.error(f"💥 [TOOL] unexpected error:\n{err}")
        return f"❌ Unexpected server error during verification:\n{err}"


async def _run(cmd: list[str], cwd: str, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return -1, f"timed out after {timeout:.0f}s: {' '.join(cmd)}"
    return proc.returncode, out.decode("utf-8", errors="replace")


# --- Tree refresh -------------------------------------------------------------
# One implementation behind three doors: the `tengoku_sync` MCP tool, the
# POST /refresh endpoint the nightly cache workflow calls, and the check at
# start-up. `scripts/pin.sh` (in the tree) does the git/cache/replay work;
# here we only serialise it with verification and restart the elaborator.
_refresh = {"running": False, "last_post": 0.0, "last": ""}
# TENGOKU_AUTO_REFRESH=0 turns every door off: for an instance that runs on a
# developer's working tree (which must never be checked out or overwritten).
AUTO_REFRESH = os.environ.get("TENGOKU_AUTO_REFRESH", "1") != "0"
PIN_SH = os.path.join(TENGOKU_DIR, "scripts", "pin.sh")


async def _ensure_pin() -> None:
    """A tree pinned to a cache commit that predates scripts/pin.sh has no copy
    of it: take the newest helper scripts from origin/main first."""
    if os.path.exists(PIN_SH):
        return
    await _run(["git", "fetch", "-q", "origin", "main"], TENGOKU_DIR, 300)
    await _run(["git", "checkout", "-q", "origin/main", "--", "scripts/pin.sh", "scripts/cache.sh"], TENGOKU_DIR, 60)


async def _tree_check() -> tuple[str, str]:
    """('current' | 'newer' | 'unknown', sha-or-detail) — changes nothing."""
    await _ensure_pin()
    rc, out = await _run([PIN_SH, "--check"], TENGOKU_DIR, 300)
    last = out.strip().splitlines()[-1] if out.strip() else ""
    parts = last.split()
    if rc in (0, 3) and len(parts) == 2 and parts[0] in ("current", "newer"):
        return parts[0], parts[1]
    return "unknown", last[:200]


async def _stop_elaborator() -> None:
    """Kill `lake serve` and every `lean` worker it forked, and never wait forever for the exit."""
    p = fast_compiler.process
    if p and p.returncode is None:
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except Exception:
            p.kill()
        try:
            await asyncio.wait_for(p.wait(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("elaborator did not report its exit within 15 s — continuing with a fresh one")
    fast_compiler.process = None
    fast_compiler._is_file_open = False


async def _tengoku_sync() -> str:
    # A refresh that has been "running" for hours is a wedged one: let the next attempt through.
    if _refresh["running"] and time.time() - _refresh.get("started_at", 0) < 3 * 3600:
        return "⏳ tengoku_sync: a refresh is already running"
    _refresh["running"] = True
    _refresh["started_at"] = time.time()
    try:
        async with fast_compiler.lock:
            head_before = (await _run(["git", "rev-parse", "--short", "HEAD"], TENGOKU_DIR, 30))[1].strip()
            await _ensure_pin()
            rc, out = await _run([PIN_SH], TENGOKU_DIR, 3600)
            tail = out.strip().splitlines()[-1] if out.strip() else ""
            if rc != 0:
                _refresh["last"] = f"failed: {tail}"
                return "❌ tengoku_sync: could not pin the tree to the newest cache\n" + out[-2000:]
            head_after = (await _run(["git", "rev-parse", "--short", "HEAD"], TENGOKU_DIR, 30))[1].strip()
            # Restart the resident elaborator so the refreshed oleans are what
            # every following verify imports.
            await _stop_elaborator()
        await fast_compiler.boot()
        asyncio.create_task(_warmup())
        _refresh["last"] = f"{head_before} → {head_after}"
        return f"✅ tengoku_sync: tree {head_before} → {head_after} ({tail}); elaborator restarted, warming."
    finally:
        _refresh["running"] = False


@mcp.tool()
async def tengoku_sync() -> str:
    """
    Move this verifier onto the newest published Tengoku build cache: pin the
    tree to that cache's commit, unpack it, replay `Tengoku.All` (nothing is
    compiled) and restart the resident elaborator. Serialised with
    verification. A tree already at the newest cache is a no-op apart from the
    restart.
    """
    if not AUTO_REFRESH:
        return "⛔ tengoku_sync is disabled on this instance (TENGOKU_AUTO_REFRESH=0: it runs on a working tree)."
    return await _tengoku_sync()


async def _refresh_endpoint(request):
    """GET: is a newer cache published than the one loaded? POST: if so, refresh
    in the background. Public on purpose: it can only ever move the tree to a
    cache competemath/tengoku has PUBLISHED, so the most a stranger can do is
    make this server look at GitHub once every five minutes."""
    from starlette.responses import JSONResponse
    head = (await _run(["git", "rev-parse", "HEAD"], TENGOKU_DIR, 30))[1].strip()
    if request.method == "GET":
        status, sha = await _tree_check()
        return JSONResponse({"status": status, "pinned": head, "newest": sha, "refreshing": _refresh["running"], "last": _refresh["last"]})
    if not AUTO_REFRESH:
        return JSONResponse({"status": "disabled", "pinned": head}, status_code=403)
    if _refresh["running"] and time.time() - _refresh.get("started_at", 0) < 3 * 3600:
        return JSONResponse({"status": "busy", "pinned": head}, status_code=409)
    now = time.time()
    if now - _refresh["last_post"] < 300:
        return JSONResponse({"status": "cooldown", "pinned": head}, status_code=429)
    _refresh["last_post"] = now
    status, sha = await _tree_check()
    if status != "newer":
        return JSONResponse({"status": status, "pinned": head, "newest": sha})
    asyncio.create_task(_tengoku_sync())
    return JSONResponse({"status": "refreshing", "pinned": head, "newest": sha}, status_code=202)


async def _startup():
    """At start: if a newer cache was published since this image was built (a
    nightly went by while the Space slept), move onto it before warming up."""
    if not AUTO_REFRESH:
        logger.info("🌳 Tree auto-refresh is off (TENGOKU_AUTO_REFRESH=0)")
        await _warmup()
        return
    try:
        status, sha = await _tree_check()
    except Exception as e:  # never let the check keep the service from warming up
        logger.warning(f"tree check failed: {e}")
        status, sha = "unknown", str(e)[:120]
    if status == "newer":
        logger.info(f"🌱 A newer Tengoku cache is published ({sha[:12]}) — refreshing before warm-up…")
        result = await _tengoku_sync()
        logger.info(result.splitlines()[0])
        if result.startswith("✅"):
            return  # _tengoku_sync booted the elaborator and started the warm-up
    else:
        logger.info(f"🌳 Tree check: {status} {sha[:12]}")
    await _warmup()


# =============================================================================
# BOOT
# =============================================================================
async def _warmup():
    logger.info("⏳ Warmup: cold-loading the Tengoku tree into the elaborator "
                "(first load can take several minutes on a small CPU)…")
    try:
        r = await fast_compiler.verify_script(
            "theorem warmup : 1 + 1 = 2 := by rfl", timeout=WARMUP_TIMEOUT)
        logger.info(f"✅ Warmup complete — Tengoku resident. Result: {r}")
    except Exception as e:
        logger.error(f"⚠️  Warmup did not finish: {e}")


async def main_serve():
    logger.info("=" * 60)
    logger.info("Booting Leak Lean Daemon…")
    logger.info("=" * 60)
    await fast_compiler.boot()

    # Warm Mathlib in the BACKGROUND and start serving immediately. Two reasons:
    #  1. The port opens right away so HF marks the Space healthy (no startup
    #     kill while a multi-minute cold load runs).
    #  2. The daemon lock serialises verifies, so the first real request simply
    #     WAITS behind this warmup instead of firing its own didChange and
    #     restarting the load — the thrash that kept Mathlib from ever loading.
    asyncio.create_task(_startup())

    http_app = mcp.sse_app()
    http_app.add_route("/refresh", _refresh_endpoint, methods=["GET", "POST"])
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*", "mcp-protocol-version", "mcp-session-id"],
        expose_headers=["mcp-session-id"],
    )

    port = int(os.environ.get("PORT", "7860"))
    logger.info(f"🌐 Serving MCP (SSE) on 0.0.0.0:{port}")
    config = uvicorn.Config(
        http_app, host="0.0.0.0", port=port,
        proxy_headers=True, forwarded_allow_ips="*",
        log_level="info", loop="asyncio",
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main_serve())
