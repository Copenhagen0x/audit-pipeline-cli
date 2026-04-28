# Sentinel deployment artifacts

VPS deployment scripts for the audit-pipeline running 24/7 against Percolator.

## Files

| File | Purpose |
|---|---|
| `bootstrap.sh` | Idempotent VPS setup — clones repos, installs CLI, inits workspace, pulls target repos, smoke-tests both daemons |
| `sentinel-shadow.service` | systemd unit for the 24/7 mainnet shadow audit |
| `sentinel-watch.service` | systemd unit for the source-code watcher |
| `sync_to_gist.sh` | Hourly cron — pushes alert log + state to a public Gist |

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
sudo cp ~/sentinel-shadow.service /etc/systemd/system/
sudo cp ~/sentinel-watch.service  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel-shadow.service
sudo systemctl enable --now sentinel-watch.service

# 5. Verify
systemctl status sentinel-shadow sentinel-watch

# 6. Set up the Gist sync
gh auth login   # if not already
gh gist create ~/audit_runs/percolator-live/shadow/state.json --public --filename STATUS.md
# Note the GIST_ID from the URL, then:
echo "0 * * * * GIST_ID=<gist-id> WORKSPACE=$HOME/audit_runs/percolator-live $HOME/audit-pipeline-cli/deploy/sync_to_gist.sh" | crontab -
```

After this, the public Gist URL becomes the live deliverable link in your investor / customer outreach.
