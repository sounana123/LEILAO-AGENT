# Run the scanner 24/7 on Oracle Cloud (Always Free)

Goal: the 08:00 digest leaves **even when your laptop is off**. The app, the
Playwright scan and the WhatsApp bridge all run on a small always-on server in
São Paulo, and the schedule is pinned to `America/Sao_Paulo`.

```
08:00 America/Sao_Paulo
   └─ Oracle VM
        ├─ uvicorn  ->  scheduler -> scan -> WhatsApp digest
        ├─ node     ->  WhatsApp bridge (linked device)
        └─ data/    ->  leiloes.db, config.json, whatsapp_auth
```

Cost: **R$ 0** within the Always Free limits (a card is required at signup, see
[Caveats](#caveats)).

---

## 1. Create the Oracle account

1. Go to <https://www.oracle.com/cloud/free/> → **Start for free**.
2. Country, name, e-mail, mobile number, then verify the e-mail.
3. **Home region: `Brazil East (São Paulo)`** (or `Brazil Southeast (Vinhedo)`).

   > This choice is effectively permanent for the account. Pick Brazil — it
   > keeps the auction sites behaving the way they do from your home network.

4. Verify the card (a temporary ~US$ 1 hold, refunded). Always Free resources
   are not charged.
5. Wait for the "your account is ready" e-mail (a few minutes, sometimes hours).

## 2. Create the instance

Console → menu → **Compute → Instances → Create instance**.

| Field | Value |
|---|---|
| Name | `leilao` |
| Image | **Canonical Ubuntu 24.04** (the arm64 build is the only one offered for A1) |
| Shape | **Ampere → VM.Standard.A1.Flex** |
| OCPUs / memory | **2 OCPU / 12 GB** (the full Always Free allowance) |
| Boot volume | 50 GB (default) |
| Networking | Create new VCN → public subnet → **Assign a public IPv4: Yes** |
| SSH keys | Paste your public key (see below) |

Generate a key on the laptop (PowerShell):

```powershell
ssh-keygen -t ed25519 -C "leilao-vps" -f "$env:USERPROFILE\.ssh\leilao"
Get-Content "$env:USERPROFILE\.ssh\leilao.pub"
```

Paste the printed line into **Add SSH keys → Paste public keys**.

> **"Out of host capacity"** is common on A1 in Brazil. Try another availability
> domain (AD-1 / AD-2 / AD-3), retry later, or try **1 OCPU / 6 GB** — plenty for
> this app. Upgrading the account to *Pay As You Go* also fixes capacity and
> keeps Always Free usage free (see [Caveats](#caveats)).

Note the public IP once the instance is running. Stopping/starting the instance
can change it — reserve it (Networking → Reserved public IPs) if you plan to
stop it, otherwise just update the SSH command.

Keep the shape at 2 OCPU / 12 GB: anything larger is outside the free allowance
and is billed.

## 3. Do not open any ports

The server listens on `127.0.0.1` only. SSH (22) is already open; leave it that
way and **do not add 8000 or 8099** to the security list. You will reach the UI
through an SSH tunnel (step 6), or a Cloudflare Tunnel (step 11).

## 4. Send the project to the server

On the laptop, stop the local copy so two schedulers are never running against
the same WhatsApp session:

```powershell
Get-Process pythonw -ErrorAction SilentlyContinue | Stop-Process
```

Then pack and upload (built-in `tar` and `scp` on Windows 10+):

```powershell
cd "D:\ai projects\New folder"
tar -czf "$env:TEMP\leilao.tgz" `
  --exclude='*node_modules*' `
  --exclude='*.venv*' `
  --exclude='*__pycache__*' `
  --exclude='*whatsapp_auth*' `
  --exclude='*whatsapp_bridge.log' `
  --exclude='*leiloes.db*' `
  .
scp -i "$env:USERPROFILE\.ssh\leilao" "$env:TEMP\leilao.tgz" ubuntu@<IP>:~/
```

`whatsapp_auth` is deliberately excluded: the server links WhatsApp itself, and
copying the keys would make the two machines fight over one device session.
`leiloes.db` is excluded too — the server builds a fresh database on the first
scan, which means the first digest sends the current lots instead of reporting
"no new deals". The FIPE cache is kept.

On the server:

```powershell
ssh -i "$env:USERPROFILE\.ssh\leilao" ubuntu@<IP>
```

```bash
sudo mkdir -p /opt/leilao
sudo tar -xzf ~/leilao.tgz -C /opt/leilao
sudo chown -R ubuntu:ubuntu /opt/leilao
rm ~/leilao.tgz
ls /opt/leilao/app.py /opt/leilao/data
```

## 5. Install

```bash
cd /opt/leilao
bash deploy/install.sh
```

It sets the timezone, installs Python + Node 22, downloads Chromium, installs
the bridge dependencies, and enables two services:

| Service | What it does |
|---|---|
| `leilao-web` | FastAPI + the 08:00 scheduler |
| `leilao-bridge` | WhatsApp Web session, restarts on crash/boot |

Both start on boot and restart automatically. Expect 5–10 minutes on the first
run.

## 6. Open the UI

From the laptop (keep this window open):

```powershell
ssh -i "$env:USERPROFILE\.ssh\leilao" -N -L 8000:127.0.0.1:8000 ubuntu@<IP>
```

Browse to <http://127.0.0.1:8000/leilao>.

## 7. Link WhatsApp

In **WhatsApp digest**:

1. Press **Connect / show QR** and wait for the code.
2. Phone → **WhatsApp → Settings → Linked devices → Link a device** → scan.
3. If there is no camera, use **Link with a phone number** → **Get code**.
4. The status line becomes *Linked to +…*.
5. Check **Send to (optional)** — the number the digest goes to. Empty means
   your own "Message yourself" chat.

The session lives in `/opt/leilao/data/whatsapp_auth`; the QR is only needed
once. WhatsApp allows up to four linked devices, so the old laptop session can
stay or be removed from the phone.

## 8. Prove the whole path works

Send a single text first:

```bash
curl -s -X POST http://127.0.0.1:8000/api/leilao/whatsapp/test
```

Then let the **scheduler itself** send the real digest. Enable it with a time two
minutes from now — in the UI: tick **Send daily**, set that time, press
**Save WhatsApp settings**. Or with curl:

```bash
date +%H:%M
curl -s -X POST http://127.0.0.1:8000/api/leilao/config \
  -H 'Content-Type: application/json' \
  -d "{\"alerts\":{\"whatsapp\":{\"enabled\":true,\"daily_time\":\"$(date -d '+2 minutes' +%H:%M)\"}}}"
```

Within ~2.5 minutes you should receive the header plus one photo card per car.
Check that the slot was claimed and what the outcome was:

```bash
cd /opt/leilao && .venv/bin/python - <<'PY'
import json, sqlite3
c = sqlite3.connect("data/leiloes.db")
for key in ("last_digest_date", "last_digest_result"):
    row = c.execute("select value from meta where key=?", (key,)).fetchone()
    value = row[0] if row else None
    if key == "last_digest_result" and value:
        data = json.loads(value)
        value = {k: data.get(k) for k in ("sent", "deals", "images", "skipped", "error")}
    print(key, "=", value)
PY
```

Expect `sent: True`. Watch it live in another SSH session with
`sudo journalctl -u leilao-web -f`:

```
leilao.scheduler: daily digest: sending (slot ..., target +55...)
leilao.scheduler: daily digest: sent=True deals=8 images=8 skipped=None error=None
```

If you see `skipped: no new deals for today`, those lots had already been sent
and the trigger still worked. Force a full resend with **Send digest now**, or:

```bash
curl -s -X POST http://127.0.0.1:8000/api/leilao/whatsapp/digest \
  -H 'Content-Type: application/json' -d '{"force":true}'
```

To repeat the scheduler test the same day, clear the claim, then set the time
again:

```bash
cd /opt/leilao && .venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/leiloes.db")
c.execute("delete from meta where key='last_digest_date'")
c.commit()
PY
```

## 9. Set the 08:00 schedule

In the UI: keep **Send daily** ticked, set the time to **08:00**, press
**Save WhatsApp settings**. Or:

```bash
curl -s -X POST http://127.0.0.1:8000/api/leilao/config \
  -H 'Content-Type: application/json' \
  -d '{"alerts":{"whatsapp":{"daily_time":"08:00"}}}'
```

Today's slot was claimed by the test above, so nothing else is sent today.
Tomorrow the digest leaves at 08:00 on its own, laptop off or not.

> **Catch-up:** if the server is down at 08:00, the digest is sent as soon as it
> is back the same day. If you enable the digest after 08:00, it sends
> immediately — expected, not a bug.

## 10. Day-2 operations

```bash
sudo systemctl status leilao-web leilao-bridge
sudo journalctl -u leilao-web -n 100 --no-pager
sudo journalctl -u leilao-bridge -n 50 --no-pager
sudo systemctl restart leilao-web        # after a code change
sudo systemctl restart leilao-bridge     # after bridge code changes; reconnects itself
timedatectl                               # must show America/Sao_Paulo
```

The scheduler logs one line per daily run:

```
leilao.scheduler: daily digest: sending (slot 2026-10-28 08:00, target +55…)
leilao.scheduler: daily digest: sent=True deals=8 skipped=None error=None
```

Health check without opening the UI:

```bash
curl -s http://127.0.0.1:8000/api/leilao/status | python3 -m json.tool | head -n 40
```

**Backup** (optional, nightly at 04:00, kept 14 days):

```bash
crontab -e
```

```cron
0 4 * * * cd /opt/leilao && tar czf /home/ubuntu/leilao-$(date +\%F).tgz agents/leilao/config.json data/leiloes.db data/fipe_cache.json && find /home/ubuntu -maxdepth 1 -name 'leilao-*.tgz' -mtime +14 -delete
```

**Update** after changing code on the laptop: repeat step 4, then

```bash
cd /opt/leilao && bash deploy/install.sh
```

`data/` is never touched by the installer.

## 11. Optional: public URL with Cloudflare Tunnel

Only if you want the UI from any device without SSH. `cloudflared` opens an
outbound connection — no inbound ports.

```bash
curl -L -o /tmp/cloudflared.deb \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
sudo dpkg -i /tmp/cloudflared.deb
cloudflared tunnel login          # opens a URL, pick your domain
cloudflared tunnel create leilao
cloudflared tunnel route dns leilao leilao.example.com
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: <UUID printed by "tunnel create">
credentials-file: /home/ubuntu/.cloudflared/<UUID>.json
ingress:
  - hostname: leilao.example.com
    service: http://127.0.0.1:8000
  - service: http_status:404
```

Then install it as a service. `service install` reads `/etc/cloudflared/config.yml`
(not `~/.cloudflared/config.yml`), so copy it first:

```bash
sudo mkdir -p /etc/cloudflared
sudo cp ~/.cloudflared/config.yml /etc/cloudflared/config.yml
sudo cloudflared service install
```

```bash
sudo systemctl status cloudflared
sudo journalctl -u cloudflared -n 30 --no-pager
```

> ⚠️ **The app has no login.** Anyone with the hostname could run scans and send
> WhatsApp messages. Put the hostname behind **Cloudflare Access** (Zero Trust →
> Access → Applications, free for a few users) before sharing it. Otherwise keep
> the SSH tunnel.

## Caveats

**Idle reclamation (Always Free).** Oracle may reclaim an Always Free instance
if, over 7 days, CPU p95 is under 20%, network under 20% **and** memory under
20%. This app is light, so a free-tier instance can qualify as idle. Options:

- Upgrade the account to **Pay As You Go**. Always Free usage stays free; the
  upgrade also fixes "out of host capacity". Set a budget alert. Oracle does not
  publish an explicit exemption for upgraded accounts, so keep the backup either
  way.
- Accept the risk and keep the backup above — if it is reclaimed, re-run this
  guide (~20 minutes) and re-link WhatsApp.

**Auction sites from a datacenter IP.** The server scans from Oracle's network,
not your home connection. If a source starts returning 403 / zero lots, check
the scan report in the UI. A Brazilian region helps; a residential proxy would
be the next step.

**"Out of host capacity."** See step 2. Retry, another AD, 1 OCPU / 6 GB, or
upgrade to Pay As You Go.

**WhatsApp.** This is an unofficial client (Baileys), as before. A linked device
that stays offline for ~14 days gets unlinked — the server is always on, so that
should not happen. If the phone is lost, unlink from another device and re-scan.

**Time zone.** Everything uses the server clock. `install.sh` sets
`America/Sao_Paulo` and the units pin `TZ`. If you move to another host, keep
both.

**Free allowance.** 2 OCPU / 12 GB Ampere, 200 GB block storage, 10 TB egress
per month — far more than this app needs. Only the boot volume (50 GB) is used.
