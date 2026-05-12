#!/usr/bin/env python3
"""wrap_per_cycle_landing.py — Generate a jelleo-chromed landing page for one cycle.

For each /var/www/jelleo.com/cycles/<id>/, writes index.html wrapped in the same
top-nav + footer chrome the archive page uses. Detects the artefact-pair naming
(cycle.* vs hunt_report.*) and links to whatever exists.

Usage:
    python3 wrap_per_cycle_landing.py <cycle-id> [--docroot /var/www/jelleo.com]
    python3 wrap_per_cycle_landing.py --all       # backfill every cycle dir
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

DEFAULT_DOCROOT = Path('/var/www/jelleo.com')
DEFAULT_CHROME_DIR = Path(__file__).resolve().parent / 'jelleo_chrome'


def _read_chrome(chrome_dir: Path) -> tuple[str, str] | None:
    top = chrome_dir / 'chrome_top.html'
    bot = chrome_dir / 'chrome_bottom.html'
    if not (top.is_file() and bot.is_file()):
        return None
    return top.read_text(encoding='utf-8'), bot.read_text(encoding='utf-8')


def _detect_artefacts(cycle_dir: Path) -> dict[str, str] | None:
    """Return artefact filenames present in the cycle dir, or None if no signed pair."""
    for html in ('cycle.html', 'hunt_report.html'):
        if (cycle_dir / html).is_file() and (cycle_dir / f'{html}.sig').is_file():
            pdf = html[:-5] + '.pdf'
            return {
                'html': html,
                'html_sig': f'{html}.sig',
                'pdf': pdf if (cycle_dir / pdf).is_file() else '',
                'pdf_sig': f'{pdf}.sig' if (cycle_dir / f'{pdf}.sig').is_file() else '',
            }
    return None


def _render_body(cycle_id: str, artefacts: dict[str, str]) -> str:
    html_link = artefacts['html']
    sig_link = artefacts['html_sig']
    pdf_link = artefacts.get('pdf') or ''
    pdf_sig_link = artefacts.get('pdf_sig') or ''
    pdf_row = (
        f'<li><a href="{pdf_link}">{pdf_link}</a> &middot; PDF render of the report</li>'
        if pdf_link else ''
    )
    pdf_sig_row = (
        f'<li><a href="{pdf_sig_link}">{pdf_sig_link}</a> &middot; Ed25519 signature over the PDF</li>'
        if pdf_sig_link else ''
    )
    return f"""<main id="main">
<style>
  .cyc-page {{ max-width: 760px; margin: 0 auto; padding: 96px 32px 64px; color: var(--ink, #f5f3ed); }}
  .cyc-eyebrow {{ font-family: var(--mono); font-size: 11px; letter-spacing: .24em; text-transform: uppercase; color: var(--amber, #f5b800); margin-bottom: 12px; }}
  .cyc-h1 {{ font-size: clamp(34px, 4vw, 48px); font-weight: 700; letter-spacing: -0.02em; line-height: 1.05; margin-bottom: 16px; }}
  .cyc-h1 code {{ font-family: var(--mono); font-size: 0.7em; color: var(--amber); padding: 4px 12px; background: rgba(245,184,0,0.08); border-radius: 6px; border: 1px solid rgba(245,184,0,0.2); }}
  .cyc-lede {{ color: var(--ink-2, rgba(245,243,237,0.72)); font-size: 17px; line-height: 1.55; margin-bottom: 28px; }}
  .cyc-h2 {{ font-size: 20px; font-weight: 600; letter-spacing: -0.005em; color: var(--amber); margin: 36px 0 14px; }}
  .cyc-page ul {{ list-style: none; padding: 0; }}
  .cyc-page ul li {{ padding: 8px 14px; margin: 6px 0; border: 1px solid var(--rule, rgba(245,243,237,0.08)); border-left: 3px solid var(--amber); border-radius: 0 6px 6px 0; background: rgba(245,184,0,0.03); font-family: var(--mono); font-size: 13px; }}
  .cyc-page ul li a {{ color: var(--ink, #f5f3ed); border-bottom: 1px dashed rgba(245,184,0,0.32); }}
  .cyc-page ul li a:hover {{ color: var(--amber); }}
  .cyc-pre {{ background: rgba(245,184,0,0.04); border: 1px solid rgba(245,184,0,0.18); border-radius: 6px; padding: 16px 20px; font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); overflow-x: auto; line-height: 1.5; }}
  .cyc-pre .cyc-cmt {{ color: var(--ink-4, rgba(245,243,237,0.28)); }}
  .cyc-back {{ display: inline-block; margin-bottom: 24px; font-family: var(--mono); font-size: 12px; color: var(--ink-3); }}
  .cyc-back:hover {{ color: var(--amber); }}
</style>
<div class="cyc-page">
  <a class="cyc-back" href="/cycles/">&larr; Cycle archive</a>
  <div class="cyc-eyebrow">Signed cycle receipt</div>
  <h1 class="cyc-h1">Cycle <code>{cycle_id}</code></h1>
  <p class="cyc-lede">
    Every artefact below is attested with the Jelleo platform&rsquo;s Ed25519 key. Verify independently of Jelleo using the public key at
    <a href="https://api.jelleo.com/keys/jelleo.ed25519.pub">api.jelleo.com/keys/jelleo.ed25519.pub</a>.
  </p>

  <h2 class="cyc-h2">Artefacts</h2>
  <ul>
    <li><a href="{html_link}">{html_link}</a> &middot; branded HTML audit report</li>
    <li><a href="{sig_link}">{sig_link}</a> &middot; Ed25519 signature over the HTML</li>
    {pdf_row}
    {pdf_sig_row}
  </ul>

  <h2 class="cyc-h2">Verify independently</h2>
  <p class="cyc-lede" style="margin-bottom: 14px;">
    Pin the platform public key once, then verify any artefact against it without trusting the operator:
  </p>
<pre class="cyc-pre">curl -O https://api.jelleo.com/keys/jelleo.ed25519.pub
curl -O https://api.jelleo.com/cycles/{cycle_id}/{html_link}
curl -O https://api.jelleo.com/cycles/{cycle_id}/{sig_link}

audit-pipeline sign verify --pubkey jelleo.ed25519.pub \\
  --artifact {html_link} --sig {sig_link}
<span class="cyc-cmt"># &rarr; "&check; signature valid, signed by &lt;fingerprint&gt;"</span></pre>

  <p style="color: var(--ink-4); font-family: var(--mono); font-size: 11px; margin-top: 32px; padding-top: 18px; border-top: 1px solid var(--rule);">
    Methodology &sect;07 &middot;
    <a href="https://github.com/Copenhagen0x/audit-pipeline-cli/tree/main/docs/methodology">spec</a> &middot;
    <a href="https://api.jelleo.com/keys/jelleo.ed25519.pub">platform public key</a>
  </p>
</div>
"""


def write_index(cycle_dir: Path, chrome_dir: Path = DEFAULT_CHROME_DIR) -> bool:
    artefacts = _detect_artefacts(cycle_dir)
    if not artefacts:
        return False
    chrome = _read_chrome(chrome_dir)
    if not chrome:
        print(f'no chrome partials at {chrome_dir}', file=sys.stderr)
        return False
    top, bot = chrome
    body = _render_body(cycle_dir.name, artefacts)
    (cycle_dir / 'index.html').write_text(top + body + bot, encoding='utf-8')
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('cycle_id', nargs='?')
    ap.add_argument('--docroot', type=Path, default=DEFAULT_DOCROOT)
    ap.add_argument('--all', action='store_true', help='backfill every cycle dir')
    args = ap.parse_args()

    cycles_dir = args.docroot / 'cycles'
    if args.all:
        n_ok, n_skip = 0, 0
        for child in sorted(cycles_dir.iterdir()):
            if not child.is_dir() or child.name.startswith('.'):
                continue
            if write_index(child):
                print(f'wrote {child}/index.html')
                n_ok += 1
            else:
                n_skip += 1
        print(f'\ndone: {n_ok} wrote, {n_skip} skipped')
        return 0
    if not args.cycle_id:
        ap.error('cycle_id required (or pass --all)')
    cycle_dir = cycles_dir / args.cycle_id
    if not cycle_dir.is_dir():
        print(f'{cycle_dir} not a directory', file=sys.stderr)
        return 1
    if not write_index(cycle_dir):
        print(f'{cycle_dir}: no signed artefacts found', file=sys.stderr)
        return 1
    print(f'wrote {cycle_dir}/index.html')
    return 0


if __name__ == '__main__':
    sys.exit(main())
