"""`audit-pipeline merkle` — per-cycle Merkle root computation (P4 Y0).

Subcommands:
  compute <cycle-id>   Compute + write merkle.json sidecar for one cycle
  verify <cycle-id>    Recompute and compare against the on-disk merkle.json
                       (tamper detection)
  list                 Show recent cycles + their stored Merkle roots
  rebuild-all          Compute merkle.json for every cycle that doesn't have one
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from audit_pipeline.bundle.paths import (
    patch_path,
    verification_path,
    writeup_path,
)
from audit_pipeline.db import open_findings_db
from audit_pipeline.merkle import (
    SCHEMA_VERSION,
    cycle_merkle_root,
    cycle_merkle_summary,
)


def _sha256_or_empty(p: Path) -> str:
    """Return hex sha256 of ``p`` (or empty string if missing/unreadable).

    Empty-string sentinel means the field simply doesn't appear in the
    canonical encoding (legacy cycles without published HTML/PDF stay
    verifiable), but the moment a file exists, any byte-level rewrite
    invalidates the Merkle root.
    """
    try:
        if not p.is_file():
            return ""
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return ""


def _enrich_finding_for_merkle(workspace: Path, finding: dict) -> dict:
    """Compute ``bundle_digest`` for a finding row from the on-disk bundle.

    bundle_digest = sha256 of canonical concat of patch.diff || NUL ||
    verification.json || NUL || writeup.md, each file-or-empty. A finding
    without a bundle gets ``bundle_digest=""`` (empty string canonical-
    encodes as the field absent, preserving v3 root prefix compatibility).
    """
    enriched = dict(finding)
    fid = finding.get("id")
    if fid is None:
        enriched["bundle_digest"] = ""
        return enriched
    h = hashlib.sha256()
    any_part = False
    for part_path in (
        patch_path(workspace, int(fid)),
        verification_path(workspace, int(fid)),
        writeup_path(workspace, int(fid)),
    ):
        try:
            if part_path.is_file():
                h.update(part_path.read_bytes())
                any_part = True
        except OSError:
            pass
        h.update(b"\x00")
    enriched["bundle_digest"] = h.hexdigest() if any_part else ""
    return enriched


def _enrich_cycle_for_merkle(workspace: Path, cycle: dict) -> dict:
    """Compute ``cycle_html_sha256`` + ``cycle_pdf_sha256`` from the
    published artefacts at compute time. Published path convention:
    ``<workspace>/public/cycles/<cycle_id>/cycle.{html,pdf}``."""
    enriched = dict(cycle)
    cid = cycle.get("cycle_id")
    if not cid:
        return enriched
    pub_dir = workspace / "public" / "cycles" / str(cid)
    enriched["cycle_html_sha256"] = _sha256_or_empty(pub_dir / "cycle.html")
    enriched["cycle_pdf_sha256"] = _sha256_or_empty(pub_dir / "cycle.pdf")
    return enriched

console = Console()

# FIX B-#27/28: cycle_id is operator-supplied (CLI arg) and flows directly
# into filesystem path concatenation. Without validation, a malicious value
# like `../../tmp/evil` could write merkle.json + sign it OUTSIDE the
# workspace — turning the signing key into a notary for arbitrary content.
# Accept only the formats this command actually issues: YYYYMMDD-HHMMSS
# optionally suffixed by `-<sha7>` and/or `-<hex4>` collision-suffix.
_CYCLE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_cycle_id(cycle_id: str) -> None:
    if not cycle_id or not _CYCLE_ID_RE.match(cycle_id) or len(cycle_id) > 128:
        raise click.ClickException(
            f"invalid cycle_id {cycle_id!r}: expected alphanumeric / dash / "
            f"underscore, ≤128 chars"
        )


def _ws(ctx: click.Context) -> Path:
    return Path(ctx.obj["workspace"])


def _merkle_path(workspace: Path, cycle_id: str) -> Path:
    """Sidecar location: <workspace>/hunts/<cycle-id>/merkle.json.

    Validates the cycle_id and the resolved path stays inside the workspace.
    """
    _validate_cycle_id(cycle_id)
    out = workspace / "hunts" / cycle_id / "merkle.json"
    # Defense-in-depth: resolve both and ensure the merkle.json is under
    # workspace/hunts/. Catches symlink shenanigans + obscure path tricks.
    try:
        resolved = out.resolve(strict=False)
        anchor = (workspace / "hunts").resolve(strict=False)
        resolved.relative_to(anchor)
    except (ValueError, OSError) as e:
        raise click.ClickException(
            f"cycle_id {cycle_id!r} resolves outside workspace/hunts/: {e}"
        )
    return out


def _findings_for_cycle(db, cycle_id: str) -> list[dict]:
    """Targeted DB query — avoids silent truncation that list_findings has."""
    return db.list_findings_by_cycle(cycle_id)


def _cycle_record(db, cycle_id: str) -> dict | None:
    for c in db.list_cycles(limit=10_000):
        if c.get("cycle_id") == cycle_id:
            return c
    return None


@click.group(name="merkle")
def merkle_cmd() -> None:
    """Per-cycle Merkle root: tamper-evident summary, on-chain ready (P4 Y0)."""


@merkle_cmd.command(name="compute")
@click.argument("cycle_id", type=str)
@click.option("--protocol", "protocol_b58", default=None,
              help="base58 program id of the AUDITED protocol. When set, it is "
                   "embedded in merkle.json and SIGNED, so `merkle publish-onchain` "
                   "reads it from the signed sidecar (REQUIRED before this cycle "
                   "can be attested on-chain — binds the (protocol, cycle, root) "
                   "tuple cryptographically).")
@click.option("--out", type=click.Path(path_type=Path), default=None,
              help="Output path (default: <workspace>/hunts/<cycle-id>/merkle.json)")
@click.option("--sign/--no-sign", default=True, show_default=True,
              help="Auto-sign the merkle.json with the workspace Ed25519 key")
@click.pass_context
def compute_cmd(
    ctx: click.Context, cycle_id: str, protocol_b58: str | None,
    out: Path | None, sign: bool,
) -> None:
    """Compute + persist the Merkle root for one cycle."""
    workspace = _ws(ctx)
    # Validate cycle_id FIRST (charset + <=128 len), unconditionally. _merkle_path
    # also validates, but it is SKIPPED when --out is given, so a crafted id (e.g.
    # non-ASCII) could reach signing on the --protocol path (threat-modeler P4).
    # Validating up front covers every path.
    _validate_cycle_id(cycle_id)
    # Validate the protocol pubkey BEFORE computing/signing — never sign a
    # garbage protocol string into the attested sidecar.
    if protocol_b58 is not None:
        from audit_pipeline import attestation_client as _ac
        try:
            _ac.parse_pubkey(protocol_b58)
        except _ac.AttestationError as e:
            raise click.ClickException(str(e))
        # With --protocol this sidecar is meant for on-chain attestation. The
        # Solana program AND PublishArgs.validate cap cycle_id at
        # MAX_CYCLE_ID_LEN (32 UTF-8 bytes). _validate_cycle_id alone allows up
        # to 128 chars, so a 33-128 char id used to pass here, get SIGNED, then
        # fail at publish-onchain — a signed-but-unpublishable sidecar with no
        # rollback (paranoid-goober P4 review). Enforce the on-chain cap BEFORE
        # signing so a compute-able-for-attestation id is always publishable.
        _cid_bytes = len(cycle_id.encode("utf-8"))
        if _cid_bytes > _ac.MAX_CYCLE_ID_LEN:
            raise click.ClickException(
                f"cycle_id is {_cid_bytes} bytes but on-chain attestation caps "
                f"it at {_ac.MAX_CYCLE_ID_LEN} (MAX_CYCLE_ID_LEN); shorten the "
                f"cycle_id, or omit --protocol for a non-attested merkle.json."
            )
    db = open_findings_db(workspace)
    cycle = _cycle_record(db, cycle_id)
    if not cycle:
        raise click.ClickException(f"cycle {cycle_id} not found in DB")
    findings = _findings_for_cycle(db, cycle_id)

    # P3+P4 Defect 08 follow-through: enrich rows with computed digests
    # BEFORE handing them to the canonical encoder so v4 fields actually
    # bind to the on-disk artefacts (not just sit in the schema).
    cycle = _enrich_cycle_for_merkle(workspace, cycle)
    findings = [_enrich_finding_for_merkle(workspace, f) for f in findings]

    summary = cycle_merkle_summary(cycle, findings, protocol=protocol_b58)
    out = out or _merkle_path(workspace, cycle_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    console.print(f"[green]wrote[/green] {out}")
    console.print(f"  root: {summary['merkle_root']}")
    console.print(f"  leaves: {summary['n_leaves']} (1 cycle + {summary['n_findings']} findings)")
    if protocol_b58:
        console.print(f"  protocol: {protocol_b58} [dim](signed; on-chain-attestable)[/dim]")

    if sign:
        try:
            from audit_pipeline.commands.sign import default_key_path, sign_file
            key = default_key_path(workspace)
            if key.is_file():
                # Patch #2 round-2 fix: explicit domain="merkle" so any
                # operator-supplied --out filename (not just merkle.json) gets
                # signed under the merkle domain instead of silently failing
                # via SignError from _infer_domain.
                sig_path = sign_file(out, key, domain="merkle")
                console.print(f"[green]signed[/green] {sig_path}")
            else:
                console.print(f"[dim]signing skipped — no key at {key}[/dim]")
        except Exception as e:
            console.print(f"[yellow]signing failed:[/yellow] {e}")


@merkle_cmd.command(name="verify")
@click.argument("cycle_id", type=str)
@click.pass_context
def verify_cmd(ctx: click.Context, cycle_id: str) -> None:
    """Recompute the root and compare against the on-disk merkle.json.

    Exit non-zero if mismatch — that means either the DB or the
    merkle.json was modified since `compute` last ran.
    """
    workspace = _ws(ctx)
    sidecar = _merkle_path(workspace, cycle_id)
    if not sidecar.is_file():
        raise click.ClickException(
            f"no merkle.json at {sidecar} — run `merkle compute {cycle_id}` first"
        )
    saved = json.loads(sidecar.read_text(encoding="utf-8"))
    saved_root = saved.get("merkle_root", "")
    saved_schema = saved.get("schema", "")
    expected_schema = f"jelleo-cycle-merkle-{SCHEMA_VERSION}"

    # Schema-rotation check: if the sidecar's schema doesn't match the
    # current code's schema, the mismatch is by design (FINDING_FIELDS
    # was extended) — surface that distinctly from "tamper detected".
    if saved_schema and saved_schema != expected_schema:
        console.print(
            f"[yellow]SCHEMA ROTATED[/yellow] sidecar={saved_schema} "
            f"current={expected_schema}"
        )
        console.print(
            f"  This sidecar was computed under a previous Merkle schema. "
            f"Re-run `merkle compute {cycle_id}` to regenerate under "
            f"the current schema before re-verifying."
        )
        raise click.ClickException("schema rotated — sidecar predates current code")

    db = open_findings_db(workspace)
    cycle = _cycle_record(db, cycle_id)
    if not cycle:
        raise click.ClickException(f"cycle {cycle_id} not found in DB")
    findings = _findings_for_cycle(db, cycle_id)
    # Same enrichment as compute_cmd so verify recomputes the same root
    cycle = _enrich_cycle_for_merkle(workspace, cycle)
    findings = [_enrich_finding_for_merkle(workspace, f) for f in findings]
    current_root = cycle_merkle_root(cycle, findings)

    if saved_root == current_root:
        console.print(f"[green]OK[/green] root matches: {current_root}")
    else:
        console.print("[red]MISMATCH — DB or merkle.json changed since compute[/red]")
        console.print(f"  saved:   {saved_root}")
        console.print(f"  current: {current_root}")
        raise click.ClickException("merkle root mismatch")


@merkle_cmd.command(name="list")
@click.option("--limit", type=int, default=50, show_default=True)
@click.pass_context
def list_cmd(ctx: click.Context, limit: int) -> None:
    """Show recent cycles + the on-disk Merkle root (or '-' if missing)."""
    workspace = _ws(ctx)
    db = open_findings_db(workspace)
    cycles = db.list_cycles(limit=limit)
    table = Table(show_header=True, header_style="bold")
    table.add_column("Cycle")
    table.add_column("Engine SHA", width=12)
    table.add_column("Findings", justify="right")
    table.add_column("Merkle root (8 hex)", width=20)
    for c in cycles:
        cid = c.get("cycle_id") or "?"
        sidecar = _merkle_path(workspace, cid)
        root = "-"
        if sidecar.is_file():
            try:
                root = json.loads(sidecar.read_text(encoding="utf-8")).get("merkle_root", "?")[:16]
            except Exception:
                root = "(unreadable)"
        n_f = sum(1 for f in db.list_findings(limit=10_000)
                   if f.get("cycle_id") == cid)
        table.add_row(cid, (c.get("engine_sha") or "?")[:10], str(n_f), root)
    console.print(table)


@merkle_cmd.command(name="rebuild-all")
@click.option("--sign/--no-sign", default=True, show_default=True)
@click.pass_context
def rebuild_all_cmd(ctx: click.Context, sign: bool) -> None:
    """Compute merkle.json for every cycle that doesn't have one."""
    workspace = _ws(ctx)
    db = open_findings_db(workspace)
    cycles = db.list_cycles(limit=10_000)
    n_built, n_skipped = 0, 0
    for c in cycles:
        cid = c.get("cycle_id")
        if not cid:
            continue
        sidecar = _merkle_path(workspace, cid)
        if sidecar.is_file():
            n_skipped += 1
            continue
        findings = _findings_for_cycle(db, cid)
        c_enriched = _enrich_cycle_for_merkle(workspace, c)
        findings = [_enrich_finding_for_merkle(workspace, f) for f in findings]
        summary = cycle_merkle_summary(c_enriched, findings)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        if sign:
            try:
                from audit_pipeline.commands.sign import default_key_path, sign_file
                key = default_key_path(workspace)
                if key.is_file():
                    # Patch #2 round-2 fix: domain="merkle" explicit (same as
                    # build_cmd above).
                    sign_file(sidecar, key, domain="merkle")
            except Exception as _e:
                # Patch #2 round-3 fix (all 3 reviewers HIGH r2): SURFACE the
                # failure. Previously bare `except Exception: pass` silently
                # produced unsigned sidecars during bulk rebuild — operator
                # saw "built N merkle root(s)" with no hint signing failed.
                # Now print yellow warning per failed sidecar so the rebuild
                # summary line is accurate.
                console.print(f"[yellow]signing failed for {sidecar}:[/yellow] {_e}")
        n_built += 1
    console.print(f"[green]built[/green] {n_built} merkle root(s); skipped {n_skipped} existing; "
                   f"schema {SCHEMA_VERSION}")


# ── live-send helpers (P4.4): shared by attest-init / attest-register / publish-onchain --send ──
def _load_signer(keypair_path, *, must_be_authority: bool):
    """Load the signing keypair from the operator-supplied path. For
    authority-gated instructions, verify it IS the program authority — a
    mismatch would just be rejected on-chain, so fail fast with a clear error.
    The secret is never logged; only the live keypair object is returned."""
    from audit_pipeline import attestation_client as ac
    from audit_pipeline import attestation_send as asend
    if not keypair_path:
        raise click.ClickException("--send requires --keypair <path to the signing keypair>")
    try:
        kp = asend.load_keypair(keypair_path)
    except asend.AttestationSendError as e:
        raise click.ClickException(str(e))
    if must_be_authority and str(kp.pubkey()) != ac.EXPECTED_AUTHORITY:
        raise click.ClickException(
            f"--keypair pubkey {kp.pubkey()} is NOT the program authority "
            f"({ac.EXPECTED_AUTHORITY}); this instruction is authority-gated and the "
            f"transaction would be rejected. Use the authority keypair."
        )
    return kp


def _submit(cluster: str, ixs: list, kp) -> dict:
    """Resolve the cluster (mainnet hard-blocked, incl. genesis check), submit +
    confirm, and print the signature + explorer link."""
    from audit_pipeline import attestation_send as asend
    try:
        url = asend.resolve_cluster(cluster)
    except asend.AttestationSendError as e:
        raise click.ClickException(str(e))
    console.print(f"[yellow]SENDING[/yellow] {len(ixs)} instruction(s) to {url} as {kp.pubkey()} ...")
    try:
        res = asend.submit_and_confirm(url, ixs, kp, dry_run=False)
    except asend.AttestationSendError as e:
        raise click.ClickException(f"send failed: {e}")
    # The signature is operator-RPC-sourced; validate it's a real base58 sig
    # before composing the explorer link + returning it (a hostile custom RPC
    # could otherwise return a string that corrupts the printed URL) — and use
    # .get so a future return-shape change is a clean error, not a KeyError.
    import re as _re
    sig = res.get("signature")
    if not isinstance(sig, str) or not _re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{86,90}", sig):
        raise click.ClickException(f"submit returned an unexpected signature: {sig!r}")
    _cl = cluster if cluster in ("devnet", "testnet") else "custom"
    console.print(f"[bold green]submitted[/bold green] signature={sig}")
    console.print(f"  explorer: https://explorer.solana.com/tx/{sig}?cluster={_cl}")
    return res


@merkle_cmd.command(name="publish-onchain")
@click.argument("cycle_id", type=str)
@click.option("--pubkey", type=click.Path(path_type=Path), default=None,
              help="Ed25519 public key to verify the merkle.json.sig against "
                   "(default: <workspace>/keys/jelleo.ed25519.pub)")
@click.option("--out", "out_path", type=click.Path(path_type=Path), default=None,
              help="Write the attestation request (JSON) here.")
@click.option("--send/--no-send", default=False, show_default=True,
              help="Submit the publish_attestation tx (devnet). Default is "
                   "preview-only; requires --keypair (the authority).")
@click.option("--keypair", "keypair_path", type=click.Path(path_type=Path), default=None,
              help="Authority keypair path (the signer; must be EXPECTED_AUTHORITY). "
                   "Required with --send.")
@click.option("--cluster", default="devnet", show_default=True,
              help="Solana cluster (devnet/testnet/localnet). Mainnet is BLOCKED.")
@click.pass_context
def publish_onchain_cmd(
    ctx: click.Context, cycle_id: str, pubkey: Path | None, out_path: Path | None,
    send: bool, keypair_path: Path | None, cluster: str,
) -> None:
    """Build a VERIFY-GATED publish_attestation instruction for one cycle.

    This is the consumer of the signed merkle.json sidecar. Its FIRST action is
    a strict Ed25519 verification (sign.verify_signature, expect_domain=
    'merkle') — it will NOT construct an attestation for an unsigned, tampered,
    wrong-domain, or rebound sidecar. The on-chain merkle_root is the bytes from
    the SIGNED sidecar, never recomputed.

    PREVIEW ONLY: derives the PDAs + builds the instruction + prints/writes the
    request. The irreversible on-chain send (devnet, authority keypair) is wired
    in P4.4 — never mainnet without the external audit (see build-inventory).
    """
    import json as _json

    from audit_pipeline import attestation_client as ac
    from audit_pipeline.commands.sign import SignError, verify_signature

    workspace = _ws(ctx)
    sidecar = _merkle_path(workspace, cycle_id)
    if not sidecar.is_file():
        raise click.ClickException(
            f"no merkle.json at {sidecar} — run `merkle compute {cycle_id}` first"
        )
    sig_path = sidecar.parent / (sidecar.name + ".sig")
    pub_path = pubkey or (workspace / "keys" / "jelleo.ed25519.pub")

    # ── STEP 1 (FAIL-CLOSED): the sidecar must carry a valid Ed25519 signature
    # under the 'merkle' domain before we will read a single field from it for
    # on-chain use. A SignError here aborts the publish — nothing reaches chain.
    try:
        verified_bytes = verify_signature(sidecar, sig_path, pub_path, expect_domain="merkle")
    except SignError as e:
        raise click.ClickException(
            f"refusing to publish — merkle.json signature check FAILED: {e}"
        )
    console.print(f"[green]signature verified[/green] (merkle domain) {sig_path.name}")

    # ── STEP 2: parse the EXACT bytes verify_signature confirmed — NOT a second
    # `sidecar.read_text()`. A re-read reopens a TOCTOU window where the file is
    # swapped between verify and use (P4.3 review HIGH, all three reviewers).
    # A signed-but-non-JSON / non-UTF-8 blob surfaces as a clean error, not a
    # raw traceback (P4.3 r2 — goober + threat-modeler).
    try:
        merkle = _json.loads(verified_bytes.decode("utf-8"))
    except (UnicodeDecodeError, _json.JSONDecodeError) as e:
        raise click.ClickException(
            f"merkle.json passed signature verification but is not valid "
            f"UTF-8 JSON: {e}"
        )
    try:
        # protocol is read + validated from the SIGNED sidecar inside
        # from_merkle_json — no operator-supplied --protocol flag (the bound
        # tuple is (protocol, cycle, root), all signature-covered).
        args = ac.from_merkle_json(merkle, expect_cycle_id=cycle_id)
        pdas = ac.derive_pdas(args.protocol, args.cycle_id)
        ix = ac.build_instruction(args, authority=ac.EXPECTED_AUTHORITY)
    except ac.AttestationError as e:
        raise click.ClickException(str(e))
    # from_merkle_json validated merkle["protocol"] is a real base58 pubkey.
    protocol_b58 = merkle["protocol"]

    ix_data = args.instruction_data()
    # ── STEP 3: preview. (No cluster contact — live send is P4.4.)
    console.print()
    console.print("[bold]publish_attestation — PREVIEW (verify-gated, nothing sent)[/bold]")
    console.print(f"  program:    {ac.PROGRAM_ID}")
    console.print(f"  authority:  {ac.EXPECTED_AUTHORITY}  [dim](only allowed signer)[/dim]")
    console.print(f"  protocol:   {protocol_b58}")
    console.print(f"  cycle_id:   {args.cycle_id}")
    console.print(f"  engine_sha: {args.engine_sha}")
    console.print(f"  invariants: {args.invariant_count}  [dim](= n_findings)[/dim]")
    console.print(f"  merkle_root:{args.merkle_root.hex()}")
    console.print(f"  PDA config: {pdas['config']}")
    console.print(f"  PDA cycle:  {pdas['cycle']}")
    console.print(f"  PDA latest: {pdas['latest']}  [dim](must be register_protocol'd first)[/dim]")
    console.print(f"  ix data:    {len(ix_data)} bytes, {ix_data.hex()}")
    console.print(
        "[dim]protocol is read from the SIGNED sidecar (bound at "
        "`merkle compute --protocol`) — the attested (protocol, cycle, root) "
        "tuple is cryptographically fixed; there is no operator flag to fumble.[/dim]"
    )
    if not send:
        console.print("[yellow]PREVIEW[/yellow] — add `--send --keypair <authority>` "
                      "to submit (devnet only; mainnet is blocked).")

    if out_path is not None:
        request = {
            "schema": "jelleo-attestation-request/v1",
            "program_id": ac.PROGRAM_ID,
            "authority": ac.EXPECTED_AUTHORITY,
            "protocol": protocol_b58,
            "cycle_id": args.cycle_id,
            "engine_sha": args.engine_sha,
            "invariant_count": args.invariant_count,
            "merkle_root_hex": args.merkle_root.hex(),
            "pdas": pdas,
            "instruction_data_hex": ix_data.hex(),
            "accounts": [
                {"pubkey": str(m.pubkey), "is_signer": m.is_signer, "is_writable": m.is_writable}
                for m in ix.accounts
            ],
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_json.dumps(request, indent=2, sort_keys=True), encoding="utf-8")
        console.print(f"[green]wrote request[/green] {out_path}")

    # ── STEP 4 (optional): actually submit. Default is preview-only; --send +
    # --keypair (the authority) is required to put anything on devnet.
    if send:
        kp = _load_signer(keypair_path, must_be_authority=True)
        _submit(cluster, [ix], kp)


@merkle_cmd.command(name="attest-init")
@click.option("--send/--no-send", default=False, show_default=True,
              help="Submit the initialize tx (devnet). Default preview. Needs --keypair.")
@click.option("--keypair", "keypair_path", type=click.Path(path_type=Path), default=None,
              help="Funded keypair that pays + signs initialize. The on-chain "
                   "authority is FORCED to EXPECTED_AUTHORITY regardless of payer.")
@click.option("--cluster", default="devnet", show_default=True,
              help="Solana cluster (devnet/testnet/localnet). Mainnet is BLOCKED.")
@click.pass_context
def attest_init_cmd(ctx: click.Context, send: bool, keypair_path: Path | None, cluster: str) -> None:
    """One-time: create the on-chain attestation Config PDA. `initialize` forces
    authority = EXPECTED_AUTHORITY, so the payer key is irrelevant to control
    (front-run-safe). Preview by default; --send submits to devnet."""
    from audit_pipeline import attestation_client as ac
    from audit_pipeline import attestation_send as asend

    kp = None
    payer = ac.EXPECTED_AUTHORITY
    if send:
        kp = _load_signer(keypair_path, must_be_authority=False)
        payer = str(kp.pubkey())
    try:
        ix = asend.build_initialize_ix(payer)
    except asend.AttestationSendError as e:
        raise click.ClickException(str(e))
    console.print("[bold]initialize — attestation Config[/bold]")
    console.print(f"  program:            {ac.PROGRAM_ID}")
    console.print(f"  authority (forced): {ac.EXPECTED_AUTHORITY}")
    console.print(f"  payer:              {payer}")
    console.print(f"  ix data:            {bytes(ix.data).hex()}")
    if send:
        _submit(cluster, [ix], kp)
    else:
        console.print("[yellow]PREVIEW[/yellow] — payer shown is a placeholder; on "
                      "--send it becomes your --keypair pubkey. Add `--send "
                      "--keypair <funded key>` to submit (devnet).")


@merkle_cmd.command(name="attest-register")
@click.argument("protocol_b58", type=str)
@click.option("--send/--no-send", default=False, show_default=True,
              help="Submit the register_protocol tx (devnet). Default preview. Needs --keypair (authority).")
@click.option("--keypair", "keypair_path", type=click.Path(path_type=Path), default=None,
              help="Authority keypair (must be EXPECTED_AUTHORITY). Required with --send.")
@click.option("--cluster", default="devnet", show_default=True,
              help="Solana cluster (devnet/testnet/localnet). Mainnet is BLOCKED.")
@click.pass_context
def attest_register_cmd(ctx: click.Context, protocol_b58: str, send: bool,
                        keypair_path: Path | None, cluster: str) -> None:
    """One-time per audited protocol: create its `latest` freshness pointer.
    Authority-gated — must be signed by EXPECTED_AUTHORITY. Preview by default."""
    from audit_pipeline import attestation_client as ac
    from audit_pipeline import attestation_send as asend

    try:
        protocol = ac.parse_pubkey(protocol_b58)
        ix = asend.build_register_protocol_ix(ac.EXPECTED_AUTHORITY, protocol)
        pdas = ac.derive_pdas(protocol, "")  # only config + latest are relevant here
    except (ac.AttestationError, asend.AttestationSendError) as e:
        raise click.ClickException(str(e))
    console.print("[bold]register_protocol[/bold]")
    console.print(f"  program:    {ac.PROGRAM_ID}")
    console.print(f"  authority:  {ac.EXPECTED_AUTHORITY}")
    console.print(f"  protocol:   {protocol_b58}")
    console.print(f"  PDA latest: {pdas['latest']}")
    console.print(f"  ix data:    {bytes(ix.data).hex()}")
    if send:
        kp = _load_signer(keypair_path, must_be_authority=True)
        _submit(cluster, [ix], kp)
    else:
        console.print("[yellow]PREVIEW[/yellow] — add `--send --keypair <authority>` to submit (devnet).")
