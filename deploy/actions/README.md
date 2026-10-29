# Run the digest for free with GitHub Actions (08:00 São Paulo)

This works with **no Oracle account, no card, and no always-on machine at home**. GitHub runs the scan + WhatsApp send in the cloud at ~11:00 UTC (08:00 BRT). 

> **Caveats (important):**
> - **Timing**: GitHub's cron can start a few minutes late under load. Don't rely on it to the second.
> - **IP rotation**: Every run comes from a fresh GitHub-hosted runner IP. WhatsApp can be stricter about new IPs logging in repeatedly. Keeping the linked session cached (`data/whatsapp_auth`) helps a lot, but there's still a small risk of the number being limited/unlinked.
> - **No live UI**: You won't have the web UI running 24/7. You'll trigger/link via Actions or let it run on schedule. You can still download `digest-result.json` from each run's artifacts.
> - **Keep-alive**: GitHub disables scheduled workflows on repos with no activity for 60 days. The `keepalive.yml` workflow commits once a month to prevent this.


## 1) Push this repo to GitHub
If you haven't already, create a new repo (private is fine) and push everything:

```bash
git init
git add .
git commit -m "leilao scanner"
git branch -M main
git remote add origin https://github.com/<YOUR_USER>/<YOUR_REPO>.git
git push -u origin main
```

The workflows live in `.github/workflows/`. 


## 2) Link WhatsApp (one-time)
The session must be created from Actions and cached. You cannot copy `whatsapp_auth` from your laptop for this (it fights sessions) — do it fresh here.

1. Go to your repo → **Actions → leilao-link**
2. Click **Run workflow**
3. Fill `phone` with your number in international format, **digits only** with country code. e.g. `5511999998888` (Brazil)
4. Run it. Watch the logs live.
5. The logs will print a **PAIRING CODE** like `ABCD-EFGH`. On your phone: **WhatsApp → Settings → Linked devices → Link a device → Link with phone number** and enter that code.
6. The job waits up to 4 minutes. When it says `LINKED as ...` you're done.
7. The `data/` directory (containing `whatsapp_auth/`) is saved as the Actions cache by the workflow. The next digest run will restore it.

> If it times out, just run `leilao-link` again. If WhatsApp says "try again later", wait a few minutes.


## 3) Set the recipient (optional)
By default it sends to "Message yourself" (empty `bridge_recipient`). To send to a different number:

Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- Name: `WHATSAPP_RECIPIENT`
- Value: `+5511999999999` (with + and country code)

The digest workflow passes this through `WHATSAPP_BRIDGE_RECIPIENT` and overrides `alerts.whatsapp.bridge_recipient` for that run.


## 4) Test the digest
1. Go to **Actions → leilao-digest → Run workflow**
2. Check **Build the digest and print it without sending** (`dry_run`) first to see what would go out (uses cached session if linked)
3. Uncheck it and run again to actually send. Look at **Artifacts → digest-result.json** and the logs.
4. On your phone you should get the header + one photo card per lot (same format as before).


## 5) Let it run automatically at 08:00
The schedule is already set: `cron: "0 11 * * *"` (UTC) which is 08:00 America/Sao_Paulo.

GitHub will run it daily. The `keepalive.yml` runs on the 1st of each month to prevent the repo from going idle.

**To change the time:** edit `.github/workflows/leilao-digest.yml` and adjust the cron. Remember it's UTC.


## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `the bridge did not start` | Check `data/whatsapp_bridge.log` in the run's artifact. Usually missing `node_modules` or Chromium issues. The workflow installs both. |
| `not linked - run the 'leilao-link' workflow once` | The cache was never saved/restored, or you ran on a fresh repo. Run `leilao-link` again. |
| `every source returned zero lots` / all errors | GitHub IPs get blocked sometimes. Try again in a few hours, or narrow filters. This is the main risk of datacenter IPs. |
| `skipped - no new deals for today` | Same as before — those lots were already alerted. Run with `dry_run` to see eligible deals, or clear `data/leiloes.db` cache key by deleting old caches (Actions → Caches). |
| Session unlinked after a few days | Baileys sessions can expire. Re-run `leilao-link` to get a new pairing code and re-link. |


## Notes on the code
- `python -m agents.leilao.ci` is the entrypoint. It scans via `runner.search_all()`, then calls `runner.whatsapp_digest(force=False)`.
- WhatsApp auth is stored under `data/whatsapp_auth/` and controlled by `LEILAO_WA_AUTH_DIR` (used by `agents/leilao/bridge.py`).
- Playwright uses `--no-sandbox --disable-dev-shm-usage` for CI containers.
- State (`data/`) is cached between runs via `actions/cache`. The first run will have no cache (fresh DB) — the first digest will send current lots, which is correct. 
