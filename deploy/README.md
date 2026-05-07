# Jelleo deployment artifacts

VPS deployment scripts for the audit-pipeline running 24/7 against Percolator.

## Files

### Core daemons (Sprint 0 — already deployed)

| File | Purpose |
|---|---|
| `bootstrap.sh` | Idempotent VPS setup — clones repos, installs CLI, inits workspace, pulls target repos, smoke-tests both daemons |
| `install_systemd.sh` | Installs all systemd units + timers (run as root after `bootstrap.sh`). Sprint-3-aware. |
| `jelleo-shadow.service` | systemd unit for the 24/7 mainnet shadow audit |
| `jelleo-watch.service` | systemd unit for the source-code watcher |
| `jelleo-health.{service,timer}` | Periodic daemon health check |
| `jelleo-backup.{service,timer}` | Findings DB backup |
| `sync_to_gist.sh` | Hourly cron — pushes alert log + state to a public Gist |

### Sprint 3 add-ons (cadence scheduler + dashboard snapshot)

| File | Purpose |
|---|---|
| `jelleo-scheduler-24h.{service,timer}` | Daily 09:00 UTC — fires confirmed-Critical/High immediate alerts + 24h rollup |
| `jelleo-scheduler-weekly.{service,timer}` | Mondays 09:15 UTC — 7-day rollup with severity breakdown |
| `jelleo-scheduler-monthly.{service,timer}` | 1st of month 09:30 UTC — executive monthly digest |
| `jelleo-snapshot.{service,timer}` | Every 60s — writes `/var/www/jelleo.com/snapshot.json` for the live dashboard |
| `notifier.example.json` | Customer recipient channel structure (copy → workspace, fill in real addresses) |
| `audit-env.additions.example` | SMTP credentials to append to `/root/.audit-env` |

## One-shot deploy

From your laptop:

```bash
# 1. Copy the bootstrap script + systemd units to the VPS
scp -i ~/.ssh/percolator_vps deploy/* <user>@<host>:~/

# 2. SSH in
ssh -i ~/.ssh/percolator_vps <user>@<host>

# 3. Run bootstrap
bash bootstrap.sh

# 4. Install + start the daemons
sudo cp ~/jelleo-shadow.service /etc/systemd/system/
sudo cp ~/jelleo-watch.service  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jelleo-shadow.service
sudo systemctl enable --now jelleo-watch.service

# 5. Verify
systemctl status jelleo-shadow jelleo-watch

# 6. Set up the Gist sync
gh auth login   # if not already
gh gist create ~/audit_runs/percolator-live/shadow/state.json --public --filename STATUS.md
# Note the GIST_ID from the URL, then:
echo "0 * * * * GIST_ID=<gist-id> WORKSPACE=$HOME/audit_runs/percolator-live $HOME/audit-pipeline-cli/deploy/sync_to_gist.sh" | crontab -
```

After this, the public Gist URL becomes the live deliverable link in your investor / customer outreach.

## Sprint 3 deploy — adding cadence + signing + snapshot to a running VPS

If the VPS is already running shadow + watch (Sprint 0 deployment), add the new units like this:

```bash
# 1. Pull the latest code on the VPS
cd ~/audit-pipeline-cli && git pull

# 2. Append SMTP creds to the existing env file
sudo -e /root/.audit-env
# (paste the JELLEO_SMTP_* block from deploy/audit-env.additions.example,
#  fill in real values, save)

# 3. Re-run the systemd installer — idempotent. It will:
#      - install the 4 new units (3 scheduler timers + snapshot)
#      - ensure cryptography is installed via pip
#      - generate the Ed25519 keypair (only if absent)
#      - publish the public key to /var/www/jelleo.com/keys/ if that path exists
#      - copy notifier.example.json -> notifier.json (only if absent)
#      - enable + start all 4 new timers
sudo bash ~/audit-pipeline-cli/deploy/install_systemd.sh

# 4. Edit notifier.json with real recipients
sudo -e /root/audit_runs/percolator-live/notifier.json

# 5. Verify SMTP works
sudo audit-pipeline --workspace /root/audit_runs/percolator-live notify test --to YOU@example.com

# 6. Force one snapshot + one cadence dry-run to confirm wiring
sudo systemctl start jelleo-snapshot.service
sudo audit-pipeline --workspace /root/audit_runs/percolator-live scheduler tick --cadence 24h --dry-run --force

# 7. Watch it work
systemctl list-timers 'jelleo-*'
journalctl -u jelleo-snapshot.service -f
```

**Cadence schedule (UTC):**

| Cadence | Fires | Audience |
|---|---|---|
| 24h | Daily 09:00 UTC | `cadence_24h` channel |
| Weekly | Monday 09:15 UTC | `cadence_weekly` |
| Monthly | 1st of month 09:30 UTC | `cadence_monthly` |
| Snapshot | Every 60s | jelleo.com dashboard fetch |
| Critical | Immediate, on confirmed transition | `critical_oncall` + CC |

The 15/30-minute offsets between cadence timers avoid all three firing at the same instant on a Monday-the-1st.

**What `install_systemd.sh` does NOT do:**
- Does not configure nginx/caddy. Point your `jelleo.com` server at `/var/www/jelleo.com/`.
- Does not deploy the website source. Push `website/deploy/*` to `/var/www/jelleo.com/` separately.
- Does not configure your firewall. Outbound SMTP needs port 587 (or 465) open egress.

**Recovery — back these up off-VPS:**
- `/root/audit_runs/percolator-live/keys/jelleo.ed25519` — platform private key
- `/root/audit_runs/percolator-live/findings.db` — findings + cycles + transitions
- `/root/audit_runs/percolator-live/notifier.json` — customer recipients
- `/root/.audit-env` — SMTP + Anthropic API creds
