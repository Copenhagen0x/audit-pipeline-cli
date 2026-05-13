#!/usr/bin/env python3
"""HMAC token verification sidecar for customer manifest URLs.

nginx subrequests via ``auth_request`` to ``GET /verify?cid=<id>&t=<token>``.
Returns 204 on valid + unexpired, 403 otherwise.

Without this, the HMAC token primitive in ``customers.issue_customer_url_token``
is theater — nginx serves /customer/<id>/manifest.json based on path
obscurity alone. This sidecar wires the token into a real
challenge/response gate.

Enable by adding to your nginx /customer/ location block:

    location /customer/ {
        auth_request /_auth_token;
        ...existing config...
    }

    location = /_auth_token {
        internal;
        proxy_pass http://127.0.0.1:8766/verify$is_args$args;
        proxy_set_header X-Original-URI $request_uri;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
    }

Then the customer URL becomes ``/customer/<id>/manifest.json?cid=<id>&t=<tok>``
or — cleaner — a small nginx map extracts the cid from the URI path so
the customer only passes ``?t=<tok>``.

stdlib only (asyncio + raw HTTP/1.1) so we don't add a dep. Reads
JELLEO_PLATFORM_KEY_PATH from env (default
``/root/audit_runs/percolator-live/keys/jelleo.ed25519``).
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8766
CRLF = b"\r\n"

DEFAULT_KEY_PATH = "/root/audit_runs/percolator-live/keys/jelleo.ed25519"
DEFAULT_WORKSPACE = "/root/audit_runs/percolator-live"
_seed_cache: bytes | None = None
_seed_path_cache: Path | None = None


def _load_seed() -> bytes | None:
    """Load + cache the platform private key seed (32 bytes).

    Re-reads the seed if the file's mtime changes (operator can swap in
    a new platform key without restarting the sidecar; the cache is
    invalidated on mtime mismatch).
    """
    global _seed_cache, _seed_path_cache
    path = Path(os.environ.get("JELLEO_PLATFORM_KEY_PATH", DEFAULT_KEY_PATH))
    if _seed_cache and _seed_path_cache == path:
        return _seed_cache
    if not path.exists():
        return None
    try:
        from audit_pipeline.customers import load_platform_priv_seed
        seed = load_platform_priv_seed(path)
        _seed_cache = seed
        _seed_path_cache = path
        return seed
    except Exception as e:  # noqa: BLE001
        print(f"[token-auth] failed to load seed: {e}", file=sys.stderr, flush=True)
        return None


def _load_url_salt(cid: str) -> bytes:
    """Look up the customer's URL-token salt from customers.json.

    POST-AUDIT FIX (2026-05-12 re-audit catch): previously this sidecar
    never looked up the per-customer URL salt, so the verify call ran
    with salt=b"" for everyone. `customer rotate-key` could write a fresh
    salt to customers.json but it was ignored here — outstanding tokens
    stayed valid forever. Now the registry-backed salt is read on every
    verify (no caching by cid alone; the registry file is small and
    nginx already caches the upstream subrequest result).
    """
    ws = Path(os.environ.get("JELLEO_WORKSPACE", DEFAULT_WORKSPACE))
    try:
        from audit_pipeline.customers import get_customer_url_salt
        return get_customer_url_salt(ws, cid)
    except Exception as e:  # noqa: BLE001
        print(f"[token-auth] url-salt lookup failed: {e}", file=sys.stderr, flush=True)
        return b""


def _verify_token(cid: str, token: str) -> bool:
    seed = _load_seed()
    if not seed or not cid or not token:
        return False
    try:
        from audit_pipeline.customers import verify_customer_url_token
        salt = _load_url_salt(cid)
        return verify_customer_url_token(seed, cid, token, salt=salt)
    except Exception as e:  # noqa: BLE001
        print(f"[token-auth] verify error: {e}", file=sys.stderr, flush=True)
        return False


async def _respond(writer: asyncio.StreamWriter, code: int, msg: str) -> None:
    lines = [
        f"HTTP/1.1 {code} {msg}".encode("ascii"),
        b"Content-Length: 0",
        b"Connection: close",
        b"Cache-Control: no-store",
        b"",
        b"",
    ]
    writer.write(CRLF.join(lines))
    await writer.drain()


async def _handle(reader, writer):
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        request_line = line.decode("ascii", errors="replace").strip()
        method, _, rest = request_line.partition(" ")
        path, _, _ = rest.partition(" ")
        # Drain headers
        while True:
            ln = await asyncio.wait_for(reader.readline(), timeout=5)
            if not ln.strip():
                break

        if method != "GET":
            await _respond(writer, 405, "Method Not Allowed")
            return

        parsed = urlparse(path)
        if parsed.path != "/verify":
            await _respond(writer, 404, "Not Found")
            return

        qs = parse_qs(parsed.query)
        cid = (qs.get("cid", [""])[0] or "").strip()
        token = (qs.get("t", [""])[0] or "").strip()

        if _verify_token(cid, token):
            await _respond(writer, 204, "No Content")
        else:
            await _respond(writer, 403, "Forbidden")
    except (asyncio.TimeoutError, ConnectionResetError, BrokenPipeError):
        pass
    except Exception as e:  # noqa: BLE001
        print(f"[token-auth] handler error: {e!r}", file=sys.stderr, flush=True)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def main() -> None:
    server = await asyncio.start_server(_handle, LISTEN_HOST, LISTEN_PORT)
    print(f"jelleo-token-auth listening on {LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
