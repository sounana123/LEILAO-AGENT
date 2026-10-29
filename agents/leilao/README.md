# Auction scanner (leilão)

Scans Brazilian vehicle auction sites, keeps only the conditions you care about,
values each lot against FIPE and shows the ones that clear your target margin.
Every number comes from a live scrape — there is no demo data anywhere.

* UI: <http://127.0.0.1:8000/leilao>
* API: `/api/leilao/status`, `/config`, `/search`, `/recalc`, `/lots`, `/lots.csv`

Requirements: Python 3.11+, Node.js 18+ (only for the optional WhatsApp bridge),
and the packages `fastapi`, `uvicorn`, `httpx`, `beautifulsoup4`, `pydantic`,
`playwright` (`playwright install chromium`).

Run the server:

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Then open `/leilao` and press **Search now**.

## What gets included

A lot is kept only when all of these hold:

| Filter | Default |
|---|---|
| Condition | `pequena monta`, `não aplicável`, `recuperado de financiamento` |
| Make | Toyota, BMW, Fiat, Volkswagen, Chevrolet, Ford, Honda |
| Current bid | ≤ R$ 300,000 (and ≥ the optional minimum bid) |
| Auction date | not in the past |
| State | all of Brazil |

The **minimum current bid** is a display filter, not a scan filter: it lives in
the Filters card (`filters.min_bid_price_brl`, `0` = off), applies to the lot
list and the CSV export, and is also a hard floor for the WhatsApp digest.

Margin is computed as `net_profit / total_investment × 100`. A lot is viable at
ROI ≥ 30%; between 30% and 60% it is shown normally, above 60% it is flagged
`above_target`. Because the live bid moves, the UI also shows **Max bid**: the
highest bid that still clears your target margin.

Costs (auction fee %, despachante, transfer, tow, repairs, contingency) are
editable in the UI. When an auctioneer publishes its own fee schedule for a
specific lot, that schedule overrides the generic assumptions.

## Sources

| Site | How it is read | Account needed |
|---|---|---|
| Copart Brazil | Public DataTables search endpoint | No |
| Superbid | Public event/offer APIs | No |
| Sodré Santoro | Public search API | No |
| Master Financiados / Maxter | Public listing pages | No |

---

## Copart: setup

**There is nothing to sign up for and no credentials to configure.** Copart
Brazil's public search page (`/lotSearchResults/`) is backed by an endpoint
that works anonymously and returns real lots with condition, FIPE value,
auction date, yard and current bid.

The scraper calls:

```
POST https://www.copart.com.br/public/lots/search
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
X-Requested-With: XMLHttpRequest
```

with a DataTables form body (`page`, `size`, `sort`, `filter[...]`,
`includeTagByField[...]`). It runs three filtered queries per scan and unions
them by lot number:

| Filter sent | Covers |
|---|---|
| `classificacaodano:"Pequena Monta"` | Pequena monta |
| `classificacaodano:"Não Aplicável"` | Não aplicável |
| `condicao:"FINANCIAMENTO"` | Recuperado de financiamento |

That is about 2,250 lots instead of the full 13,800-lot catalogue, so a scan
takes roughly 10–15 seconds on this source.

### Why JSON does not work

The same URL also accepts `Content-Type: application/json`. That path is
deliberately crippled for anonymous visitors: it ignores `page` and `filter`
and returns ten lots that are still awaiting inspection (`Aguardando
Classificação`), with no bid price. If you ever see Copart reporting `limited`,
something has changed the request back to JSON — the form body is the working
call.

### Options (`agents/leilao/config.json` → `scraper_options`)

| Key | Default | Meaning |
|---|---|---|
| `copart_page_size` | `1000` | Lots per request (server caps at 1000) |
| `copart_max_pages` | `3` | Safety cap per filtered query |
| `copart_headless` | `true` | Set `false` to watch the browser if Copart starts challenging |
| `copart_exclude_motorcycles` | `true` | Drops `vehicleType` containing "Moto" (Honda/BMW bikes) |
| `copart_max_bid_fipe_ratio` | `0.85` | Skips lots whose bid already exceeds 85% of the published FIPE value — no target margin is reachable |

### What Copart data is used for

* `hb` → current bid (lots with no bid are dropped; they cannot be valued)
* `mkn` / `lm` / `lcy` / `manufactureYear` → make, model, year
* `damageClassification` + `lossType` + `td` → condition
* `ad` → auction date (expired lots are dropped)
* `yn` → city and state
* `la` → **valuation**. Copart prints this value as "Valor FIPE" on every lot
  page, so it is used as the FIPE value for the lot and no official-FIPE lookup
  is spent on it. The stored valuation is tagged `source-published`.

Lots are also dropped when the document state is `IRRECUPERÁVEL`, the VIN is
`Remarcado`, the damage text says `Adulterado`, the classification is
`Média Monta` / `Grande Monta`, or the bid is already above
`copart_max_bid_fipe_ratio` (85%) of the published FIPE value — at that price no
target margin is reachable even before fees.

### Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Status `blocked` + note about a bot challenge | Incapsula/Imperva challenge | Retry later; `"copart_headless": false` to solve it manually |
| Status `empty` with `sampled: 0` | Endpoint shape changed | Inspect `/lotSearchResults/` in DevTools → Network → copy the `lots/search` form body |
| Lots with no price | Bidding has not opened | Expected: unpriced lots are skipped |
| Fewer lots than the website shows | Make/condition/motorcycle filters | Check `skipped_*` counters in the scan report |

Copart does not publish its buyer fee schedule per lot, so Copart lots use the
generic cost assumptions. Adjust them in the UI if you know your actual
commission and yard fees.

---

## WhatsApp digest

The scanner can send today's profitable lots to your own WhatsApp, either once a
day or on demand. There is no demo message: if the provider refuses, the UI says
exactly why.

### Default: local WhatsApp Web bridge (QR code, ~2 minutes)

The digest goes to your own **"Message yourself"** chat unless you put a number
in **Send to (optional)** — then it is sent from your linked WhatsApp to that
number instead. There is no account, no approval and no per-message cost.

1. Node.js 18+ must be installed (`node --version`). Dependencies are already in
   `agents/leilao/whatsapp_bridge`; if not, run `npm install` there once.
2. Open `/leilao` → **WhatsApp digest** → delivery
   `WhatsApp Web — scan a QR code`.
3. The server starts the bridge automatically and shows a QR code. On your
   phone: **WhatsApp → Settings → Linked devices → Link a device**, then scan.
   If scanning is awkward (no camera, remote desktop), type your number under
   **"Link with a phone number"**, press **Get code**, and enter the 8-letter
   code in **Linked devices → Link with phone number** instead.
4. The status line becomes *Linked to +55…*. Press **Send test message**.
5. Tick **Send daily**, pick a time, press **Save WhatsApp settings**.

The session is stored in `data/whatsapp_auth`, so the QR is only needed once.
**Unlink** wipes it and starts a fresh session. The bridge runs on
`127.0.0.1:8099` and is managed by `agents/leilao/bridge.py`; its log is
`data/whatsapp_bridge.log`. If the bridge is not linked, the digest fails with
*not linked - scan the QR code* instead of pretending to send. Expired QR codes
are discarded, a `515 restartRequired` reconnects immediately, and a stale
`401` session is cleared automatically so the next code is scannable.

> This uses the unofficial WhatsApp Web protocol (Baileys). It is a personal
> convenience, not an official API, and technically violates WhatsApp's terms —
> there is a small risk of the number being limited. Use your own number and
> keep sends to a handful per day.

### Running unattended (08:00 with the laptop off)

The scheduler lives inside the web server: it only ticks while `uvicorn` runs, so
a powered-off laptop cannot send anything. For a fixed 08:00 delivery, run the
app on an always-on host — [`deploy/README.md`](../../deploy/README.md) is a
step-by-step Oracle Cloud Always Free kit (Ubuntu + systemd + two always-on
services + WhatsApp re-link + timezone).

`daily_time` is the **server's local time**. On a VPS run
`sudo timedatectl set-timezone America/Sao_Paulo` (the installer does this, and
both units also export `TZ`). If the host was down at the slot, the digest goes
out as soon as it is back the same day; `last_digest_date` prevents a duplicate.

### What goes in the digest

| Setting | Default | Meaning |
|---|---|---|
| `mode` | `ending_today` | `ending_today`, `new_today` (first seen today) or `both` |
| `max_deals` | `8` | Lots per digest |
| `min_bid` | `0` | Extra digest floor (R$). The Filters minimum bid applies on top, so the higher of the two wins; `0` disables this extra floor |
| `min_margin_percent` | your ROI target | Only lots at or above this ROI |
| `daily_time` | `18:00` | Local server time |
| `scan_before_digest` | `true` | Run a fresh scan before sending |
| `notify_on_empty` | `false` | Send a "nothing today" message when there are no deals |
| `bridge_port` | `8099` | Local port of the Node bridge |
| `bridge_recipient` | empty | Empty sends to yourself; a number (e.g. `+5511...`) sends there |

The digest is a header followed by **one message per lot with the car photo**
when the source publishes one. Each card carries the title, city/state, source,
condition (pequena monta / não aplicável / recuperado de financiamento), current
bid, the **max bid** you can pay, FIPE, net profit, ROI, closing time and the lot
link. A lot without a usable photo (Master Financiados never publishes one, and a
CDN may refuse the download) is sent as the same card in text — it is never
silently dropped. Sodré Santoro photo URLs reject plain HTTP, so they are fetched
through a real browser context before falling back to text.

Lots already sent are recorded in the `alerts` table, so a second run the same
day does not repeat them — the **Send digest now** button forces a resend.

### Other providers

* `callmebot` — free, no account, but their bot is currently full
  (*"The bot is currently full. Please check back in a few days."*). If it
  reopens: save **+34 644 51 95 23**, send it
  `I allow callmebot to send me messages`, and paste the API key it replies with.
* `meta` (WhatsApp Cloud API) and `twilio` need a business account. Both only
  allow free-form text inside a 24-hour customer-service window, so a daily
  business-initiated message normally requires an approved template.

Error messages from every provider are shown verbatim in the panel.

Secrets are stored in `agents/leilao/config.json` on your machine. They can also
be supplied through environment variables so they never touch the file:
`WHATSAPP_PROVIDER`, `WHATSAPP_PHONE`, `WHATSAPP_APIKEY`,
`WHATSAPP_META_PHONE_ID`, `WHATSAPP_META_TOKEN`, `WHATSAPP_TWILIO_SID`,
`WHATSAPP_TWILIO_TOKEN`, `WHATSAPP_TWILIO_FROM`, `WHATSAPP_BRIDGE_PORT`,
`WHATSAPP_BRIDGE_RECIPIENT`, `WHATSAPP_MIN_BID`.

---

## Notes

* FIPE lookups are throttled (0.4 s minimum interval, 2 concurrent) with a
  positive-results cache in `data/fipe_cache.json`. Sources that publish their
  own FIPE value (Copart) skip the lookup entirely.
* Seen lots are tracked in `data/leiloes.db`, so alerts only fire for new ones.
* The daily WhatsApp digest runs inside the server process (`agents/leilao/scheduler.py`).
  If the server is stopped at the configured time, nothing is sent that day.
* The scanner makes a small number of requests per run. Keep it that way.
