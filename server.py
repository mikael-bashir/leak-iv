import asyncio
import os
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

mcp = FastMCP(
    "Leak-Daemon",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

# How long a single (warm) verify may run before we give up (Lean elaboration
# for a hard proof can be slow; this is the backstop, not the normal path).
VERIFY_TIMEOUT = 180.0
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
        self.process = await asyncio.create_subprocess_exec(
            "lake", "serve",
            cwd=self.project_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
                logger.info(f"🛰️  [LSP-STDERR] {line.decode('utf-8', 'replace').rstrip()}")
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
            full_text = (
                script if "import Mathlib" in script else f"import Mathlib\n\n{script}"
            ).strip() + "\n\n"

            preview = " ".join(script.strip().split())[:200]
            logger.info("─" * 60)
            logger.info(f"🔎 [#{n}] VERIFY  version={ver}  chars={len(script)}")
            logger.info(f"🔎 [#{n}] script: {preview}{'…' if len(script) > 200 else ''}")

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

                    msg = await asyncio.wait_for(self._read(), timeout=timeout)

                    # response to OUR waitForDiagnostics -> done for this version
                    if msg.get("id") == wf_id and "method" not in msg:
                        if "error" in msg:
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

                elapsed = int((time.time() - t0) * 1000)
                if not bad:
                    logger.info(f"✅ [#{n}] VERIFIED in {elapsed}ms — no errors/warnings")
                    return "✅ Compilation Successful! The proof is 100% verified."

                lines = []
                for d in bad:
                    sev = "Error" if d.get("severity", 1) == 1 else "Warning"
                    ln = d.get("range", {}).get("start", {}).get("line", 0) + 1
                    m = " ".join(str(d.get("message", "")).split())
                    lines.append(f"Line {ln} ({sev}): {m}")
                    logger.info(f"❌ [#{n}] {lines[-1]}")
                logger.info(f"❌ [#{n}] FAILED in {elapsed}ms — {len(bad)} issue(s)")
                return "❌ Compilation Failed:\n" + "\n".join(lines)

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

    IMPORTANT: "import Mathlib" is injected for you — do not add imports, and
    assume only Mathlib is available.
    """
    try:
        return await fast_compiler.verify_script(script)
    except Exception:
        err = traceback.format_exc()
        logger.error(f"💥 [TOOL] unexpected error:\n{err}")
        return f"❌ Unexpected server error during verification:\n{err}"


# =============================================================================
# BOOT
# =============================================================================
async def _warmup():
    logger.info("⏳ Warmup: cold-loading Mathlib into the elaborator "
                "(first load can take several minutes on a small CPU)…")
    try:
        r = await fast_compiler.verify_script(
            "theorem warmup : 1 + 1 = 2 := by rfl", timeout=WARMUP_TIMEOUT)
        logger.info(f"✅ Warmup complete — Mathlib resident. Result: {r}")
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
    asyncio.create_task(_warmup())

    http_app = mcp.sse_app()
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*", "mcp-protocol-version", "mcp-session-id"],
        expose_headers=["mcp-session-id"],
    )

    logger.info("🌐 Serving MCP (SSE) on 0.0.0.0:7860")
    config = uvicorn.Config(
        http_app, host="0.0.0.0", port=7860,
        proxy_headers=True, forwarded_allow_ips="*",
        log_level="info", loop="asyncio",
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main_serve())
