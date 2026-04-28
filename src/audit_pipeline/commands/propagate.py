"""`audit-pipeline propagate` — cross-protocol pattern propagation.

Layer 1.6. Two subcommands:

  init-corpus: Clone a curated list of popular Solana programs into a
    corpus directory. One-time setup. Default list = ~15 well-known
    DeFi protocols (pinned commits for reproducibility).

  search: Search the corpus for a finding's pattern using one or more
    regex signatures. Ranks files by signature match count. Top hits
    are candidate findings to escalate to Layer 1 hypothesis dispatch.

Most bug classes recur. F7's "shrink counter, don't debit vault" pattern
probably exists in any protocol with insurance accounting. CatchupAccrue's
"advance clock without touching accounts" pattern probably exists in any
protocol with multi-instruction settlement. The corpus is how we find them.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()

# File extensions worth searching across the corpus
SEARCH_EXTENSIONS = (".rs",)

# Minimum match score to surface in the report
MIN_SCORE_TO_REPORT = 1


@dataclass
class CorpusMatch:
    repo: str
    file: str
    line: int
    score: int
    matched_signatures: list[str] = field(default_factory=list)
    snippet: str = ""


# ---------------------------------------------------------------------------
# Curated corpus — popular Solana programs worth cross-checking against.
# Pinned commits where possible; falls back to default branch otherwise.
# ---------------------------------------------------------------------------

DEFAULT_CORPUS = [
    # Engine being audited (always include for cross-check baselines)
    {
        "name": "percolator",
        "url": "https://github.com/aeyakovenko/percolator",
        "ref": None,  # default branch
    },
    {
        "name": "percolator-prog",
        "url": "https://github.com/aeyakovenko/percolator-prog",
        "ref": None,
    },
    # Anchor framework + spl programs (canonical Solana code)
    {
        "name": "anchor",
        "url": "https://github.com/coral-xyz/anchor",
        "ref": None,
    },
    {
        "name": "solana-program-library",
        "url": "https://github.com/solana-program/program-library",
        "ref": None,
    },
    # Major DeFi (perp DEXes, lending, vaults)
    {
        "name": "drift-protocol-v2",
        "url": "https://github.com/drift-labs/protocol-v2",
        "ref": None,
    },
    {
        "name": "mango-v4",
        "url": "https://github.com/blockworks-foundation/mango-v4",
        "ref": None,
    },
    {
        "name": "marginfi-v2",
        "url": "https://github.com/mrgnlabs/marginfi-v2",
        "ref": None,
    },
    {
        "name": "kamino-lending",
        "url": "https://github.com/Kamino-Finance/klend",
        "ref": None,
    },
    {
        "name": "phoenix-v1",
        "url": "https://github.com/Ellipsis-Labs/phoenix-v1",
        "ref": None,
    },
    {
        "name": "openbook-v2",
        "url": "https://github.com/openbook-dex/openbook-v2",
        "ref": None,
    },
    {
        "name": "orca-whirlpools",
        "url": "https://github.com/orca-so/whirlpools",
        "ref": None,
    },
    {
        "name": "meteora-dlmm",
        "url": "https://github.com/MeteoraAg/dlmm-sdk",
        "ref": None,
    },
    {
        "name": "raydium-amm",
        "url": "https://github.com/raydium-io/raydium-amm",
        "ref": None,
    },
    {
        "name": "jupiter-swap-api-client",
        "url": "https://github.com/jup-ag/jupiter-swap-api-client",
        "ref": None,
    },
    {
        "name": "marinade-finance-onchain-sdk",
        "url": "https://github.com/marinade-finance/marinade-anchor",
        "ref": None,
    },
]


@click.group(name="propagate")
def propagate_cmd() -> None:
    """Cross-protocol pattern propagation (init-corpus + search)."""


@propagate_cmd.command(name="init-corpus")
@click.option(
    "--corpus",
    "-c",
    required=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to clone repos into (created if missing)",
)
@click.option(
    "--list-file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help=(
        "Optional JSON file overriding the default corpus list. Each entry "
        "should be {name, url, ref?}."
    ),
)
@click.option(
    "--shallow",
    is_flag=True,
    default=True,
    show_default=True,
    help="Use --depth 1 clones to save disk space and bandwidth",
)
@click.option(
    "--skip-existing",
    is_flag=True,
    default=True,
    show_default=True,
    help="Skip repos already present in the corpus dir",
)
def corpus_init(
    corpus: Path,
    list_file: str | None,
    shallow: bool,
    skip_existing: bool,
) -> None:
    """Clone the curated list of Solana programs into CORPUS.

    Default list includes the major DeFi protocols (Drift, Mango, Marginfi,
    Kamino, Phoenix, OpenBook, Orca, Meteora, Raydium) plus the SPL
    library and Anchor framework. ~15 repos total, ~5-10 GB on disk
    after shallow clone.

    Pass --list-file <path> to override with your own curated list.
    """
    if list_file:
        repos = json.loads(Path(list_file).read_text())
    else:
        repos = DEFAULT_CORPUS

    corpus.mkdir(parents=True, exist_ok=True)

    table = Table(title=f"Cloning {len(repos)} repos into {corpus}")
    table.add_column("#", style="dim", justify="right")
    table.add_column("Name", style="cyan")
    table.add_column("Status", style="bold")

    cloned = skipped = failed = 0
    for i, entry in enumerate(repos, start=1):
        name = entry["name"]
        target = corpus / name
        if target.exists() and skip_existing:
            table.add_row(str(i), name, "[dim]skipped (exists)[/dim]")
            skipped += 1
            continue
        if target.exists() and not skip_existing:
            console.print(f"[yellow]{name} already exists; --skip-existing=False not implemented; skipping[/yellow]")
            skipped += 1
            continue

        clone_cmd = ["git", "clone"]
        if shallow:
            clone_cmd += ["--depth", "1"]
        clone_cmd += [entry["url"], str(target)]

        console.print(f"[cyan]Cloning {name}...[/cyan]")
        try:
            proc = subprocess.run(
                clone_cmd, capture_output=True, text=True, timeout=600
            )
            if proc.returncode != 0:
                table.add_row(str(i), name, f"[red]FAILED: {proc.stderr.strip()[:80]}[/red]")
                failed += 1
                continue

            if entry.get("ref"):
                subprocess.run(
                    ["git", "checkout", entry["ref"]],
                    cwd=str(target), capture_output=True, text=True,
                )
            table.add_row(str(i), name, "[green]cloned[/green]")
            cloned += 1
        except subprocess.TimeoutExpired:
            table.add_row(str(i), name, "[red]TIMEOUT[/red]")
            failed += 1
        except Exception as e:  # noqa: BLE001
            table.add_row(str(i), name, f"[red]ERROR: {e}[/red]")
            failed += 1

    console.print(table)
    console.print(
        f"\n[bold]Done.[/bold] cloned={cloned} skipped={skipped} failed={failed}"
    )
    console.print(
        f"\nNext step:\n  [cyan]audit-pipeline propagate search "
        f"-c {corpus} -s '<regex1>' -s '<regex2>'[/cyan]"
    )


@propagate_cmd.command(name="search")
@click.option(
    "--corpus",
    "-c",
    required=True,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Directory containing cloned Solana program repos to search",
)
@click.option(
    "--signature",
    "-s",
    multiple=True,
    required=True,
    help=(
        "Pattern signature(s) to search for. Repeat for multiple signatures. "
        "Each is a regex (anchored to a single line). Higher match count = "
        "stronger candidate. Example: -s 'insurance.*\\.balance' -s 'vault.*[-+]?='"
    ),
)
@click.option(
    "--min-score",
    type=int,
    default=MIN_SCORE_TO_REPORT,
    show_default=True,
    help="Only report matches with at least this many signatures hitting in the same file",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output dir for the propagation report (defaults to <workspace>/recon/propagate/)",
)
@click.option(
    "--report-name",
    default="propagation_report",
    show_default=True,
    help="Filename stem for the report",
)
@click.pass_context
def propagate_search(
    ctx: click.Context,
    corpus: Path,
    signature: tuple[str, ...],
    min_score: int,
    output: Path | None,
    report_name: str,
) -> None:
    """Search a corpus of Solana programs for a finding's pattern.

    Workflow:
      1. Build a corpus directory by cloning N Solana programs into one
         folder, one subfolder per repo.
      2. Identify the bug's pattern as one or more regex signatures
         (e.g. for F7: 'insurance.*\\.balance' AND 'vault.*[-+]?='
         absent from the same function).
      3. Run propagate with -c <corpus> -s <sig1> -s <sig2>.
      4. Report ranks files by how many signatures matched. Top hits are
         candidate findings to escalate to Layer 1 hypothesis dispatch.

    The pattern matching is intentionally simple (regex) rather than AST-
    based to keep the corpus indexer fast and language-agnostic. For
    deeper matches, escalate the top hits to a Layer 1 agent.
    """
    workspace = Path(ctx.obj["workspace"])

    if output is None:
        output = workspace / "recon" / "propagate"
    output.mkdir(parents=True, exist_ok=True)

    console.print(
        f"[bold]Scanning corpus[/bold] [cyan]{corpus}[/cyan] for "
        f"{len(signature)} signature(s)..."
    )

    compiled_sigs = [(s, re.compile(s)) for s in signature]

    # Walk the corpus
    repos = sorted(p for p in corpus.iterdir() if p.is_dir())
    if not repos:
        raise click.ClickException(f"No subdirectories found in corpus {corpus}")

    matches_by_file: dict[str, CorpusMatch] = {}
    files_scanned = 0

    for repo_dir in repos:
        repo_name = repo_dir.name
        for src_path in _walk_source_files(repo_dir):
            files_scanned += 1
            try:
                content = src_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            file_matches = _scan_file_for_signatures(content, compiled_sigs)
            if not file_matches:
                continue

            # Aggregate per-file score = number of distinct signatures hit
            distinct_sigs_hit = sorted({s for s, _, _ in file_matches})
            score = len(distinct_sigs_hit)
            if score < min_score:
                continue

            # Pick the first match line as the anchor + snippet
            first = file_matches[0]
            snippet = _snippet_around(content, first[1], context=2)

            key = f"{repo_name}:{src_path.relative_to(repo_dir)}"
            matches_by_file[key] = CorpusMatch(
                repo=repo_name,
                file=str(src_path.relative_to(repo_dir)),
                line=first[1],
                score=score,
                matched_signatures=distinct_sigs_hit,
                snippet=snippet,
            )

    ranked = sorted(matches_by_file.values(), key=lambda m: -m.score)

    _print_report(ranked, files_scanned, len(repos), len(signature))
    _write_report(ranked, output / f"{report_name}.md", signature, files_scanned, len(repos))


def _walk_source_files(repo: Path):
    """Yield every .rs file under `repo`, skipping target/ and similar."""
    skip_dirs = {"target", "node_modules", ".git", "build"}
    for path in repo.rglob("*"):
        if not path.is_file():
            continue
        if not path.suffix in SEARCH_EXTENSIONS:
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        yield path


def _scan_file_for_signatures(
    content: str,
    compiled_sigs: list[tuple[str, "re.Pattern[str]"]],
) -> list[tuple[str, int, str]]:
    """Return list of (signature_str, line_number, line_text) hits in file."""
    hits: list[tuple[str, int, str]] = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        for sig_str, pattern in compiled_sigs:
            if pattern.search(line):
                hits.append((sig_str, line_no, line))
    return hits


def _snippet_around(content: str, line_no: int, context: int = 2) -> str:
    lines = content.splitlines()
    start = max(0, line_no - context - 1)
    end = min(len(lines), line_no + context)
    return "\n".join(lines[start:end])


def _print_report(
    ranked: list[CorpusMatch],
    files_scanned: int,
    repos_scanned: int,
    n_signatures: int,
) -> None:
    console.print()
    console.print(
        f"[dim]Scanned {files_scanned:,} files across {repos_scanned} repos.[/dim]"
    )
    console.print()

    if not ranked:
        console.print("[yellow]No files matched any signature.[/yellow]")
        return

    table = Table(title=f"Cross-protocol propagation results (top {min(20, len(ranked))})")
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Repo", style="cyan")
    table.add_column("File", style="dim")
    table.add_column("Anchor line", justify="right")
    table.add_column("Signatures matched")

    for m in ranked[:20]:
        table.add_row(
            f"{m.score}/{n_signatures}",
            m.repo,
            m.file,
            str(m.line),
            ", ".join(m.matched_signatures),
        )

    console.print(table)


def _write_report(
    ranked: list[CorpusMatch],
    out_path: Path,
    signatures: tuple[str, ...],
    files_scanned: int,
    repos_scanned: int,
) -> None:
    lines = [
        "# Cross-protocol propagation report",
        "",
        f"- Files scanned: {files_scanned:,}",
        f"- Repos scanned: {repos_scanned}",
        f"- Signatures: {len(signatures)}",
        "",
        "## Signatures",
        "",
    ]
    for s in signatures:
        lines.append(f"- `{s}`")
    lines.extend(["", "## Ranked matches", ""])

    if not ranked:
        lines.append("_No files matched any signature._")
    else:
        for m in ranked:
            lines.append(f"### {m.repo} / `{m.file}` (score {m.score}/{len(signatures)})")
            lines.append(f"Anchor: line {m.line}")
            lines.append(f"Signatures matched: {', '.join(f'`{s}`' for s in m.matched_signatures)}")
            lines.append("")
            lines.append("```rust")
            lines.append(m.snippet)
            lines.append("```")
            lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    console.print(f"\n[green]Report written: {out_path}[/green]")
