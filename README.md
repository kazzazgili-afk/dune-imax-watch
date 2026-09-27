# dune-imax-watch

A personal ticket-monitoring tool for **Dune: Part Three** IMAX 70mm screenings in
London. It watches for new listings, format announcements, ticket-sale
announcements, new showtime batches, and availability flipping from
unavailable/sold-out to bookable - then alerts you (macOS notification, [ntfy.sh](https://ntfy.sh)
push, and/or email) with the cinema, date/time, format, booking link, and an
urgency label. It never enters payment details or completes a purchase; on
HIGH/CRITICAL alerts it optionally auto-opens the booking link in your browser so
it's already loaded when you look.

## Why this is a detection tool, not a purchase bot

Worth knowing before adding "just buy it automatically", because the shape of the
problem is counter-intuitive:

- **The Science Museum's booking app sits behind Queue-it.** `my.sciencemuseum.org.uk`
  redirects to `sciencemuseum.queue-it.net`. Queue-it's pre-queue collects everyone who
  arrives before the sale starts and then **randomises them like a raffle** when the
  countdown hits zero; only people arriving *after* that are served
  first-come-first-served, at the back. Being fast at the moment of the drop wins
  nothing. Being *already in the waiting room* is the only thing that helps - which is
  what `prearm` is for.
- **Queue-it runs Akamai Bot Manager during the pre-queue**, and sessions it flags as
  "Aggressive" are hard-blocked or sent to the back of the line when the queue opens. A
  stealth browser with rotating proxies is not an edge here; it is how you lose.
- **BFI IMAX is behind an active Cloudflare challenge** (`HTTP 403`,
  `cf-mitigated: challenge`) and its robots.txt disallows every Tessitura transaction
  path. It is never scraped - only its newsletter is read.
- **BFI Membership is the one real edge.** "Member Row" is 24 seats per screening,
  usually central, reserved for members for the **first 24 hours of general sale**, at
  standard price, with no limit on how many you book. That turns BFI from a
  seconds-long scramble into a day-long window, for £44/year. No automation can
  replicate it.

Accordingly this tool automates *detection and readiness* only, and deliberately
contains no queue bypass, no CAPTCHA solving, no proxies, no fingerprint spoofing, and
no automated payment.

## Architecture at a glance

Three venues, five venue *entries* (some venues have several independent channels),
one shared diff engine:

- **Science Museum IMAX**
  - `science_museum_imax` (`html_page_diff` adapter) - polls the public
    `sciencemuseum.org.uk` listing page directly. Its robots.txt allows content
    pages and there's no Cloudflare-style bot wall - **but the site does return
    403 to GitHub Actions' IP ranges specifically** (confirmed: works fine from a
    home IP, fails from CI). Left enabled in `config.yaml` (local/launchd), left
    **disabled** in `config.github.yaml` since it can't work from there.
  - `science_museum_imax_email` (`imap_newsletter` adapter) - backs it up via the
    museum's own "Register for email alerts" signup on the same page. Works from
    anywhere, including GitHub Actions, since it's just reading a mailbox.
- **BFI IMAX** (`bfi_imax`, `imap_newsletter` adapter) - BFI's actual ticketing site
  (`whatson.bfi.org.uk`) is Cloudflare-challenge-protected and BFI's terms prohibit
  automated access, so **it is never scraped or automated**. Instead, this adapter
  reads your own mailbox (read-only IMAP) for the official "BFI IMAX emails" alert
  you sign up for once, and turns matching emails into alerts.

- **Science Museum IMAX (Bluesky)** (`science_museum_bluesky`, `bluesky_feed` adapter) -
  polls the museum's public Bluesky feed through `public.api.bsky.app`, which needs no
  account, key or scraping. Worth having because the museum announces 70mm on-sales
  there in the same breath as the newsletter; its real wording for the current film was
  *"New screenings of The Odyssey in magnificent IMAX 70mm are now on sale."* (BFI has
  no Bluesky presence as of Sept 2026.)
- **Vue Manchester Printworks** (`vue_manchester_email`, `imap_newsletter`, disabled
  until you sign up) - the third and only non-London UK 70mm screen. Newsletter-only:
  `www.myvue.com` answers browser-UA requests with `HTTP 403` (Cloudflare) and its
  robots.txt disallows `/book-tickets/` and `/screening/`.

All adapters return a normalized `RawListing`; a pure diff function
(`engine/diff.py`) classifies each one against SQLite-persisted state into an
event type + urgency; a `Dispatcher` fans alerts out to every enabled notification
channel independently. Adding another venue is one config block (plus a new
adapter file only if it needs a genuinely new fetch strategy).

### Polling cadence and robots.txt

`util/robots.py` enforces robots.txt at runtime rather than describing it in a comment:
a disallowed path is refused outright, and every venue's poll interval is clamped to
`max(configured, the site's Crawl-delay)`. That is what makes fast polling defensible -
`sciencemuseum.org.uk` publishes `Crawl-Delay: 20`, so a 20-**second** cadence during an
on-sale is explicitly within what the site asks for.

Each venue has a routine cadence and a **hot** cadence. Hot is entered automatically
within `hot_hours_before_onsale` of a venue's `expected_onsale`, for
`hot_hours_after_signal` after any on-sale/queue/batch alert, or on demand with
`run --hot`. Audit all of it with:

```bash
.venv/bin/python -m dune_watch robots-check --config config/config.yaml
```

## Prerequisites

- Python 3.9+
- A mailbox with IMAP access (e.g. Gmail with an [app password](https://myaccount.google.com/apppasswords))
- Sign up for **both** official email alerts before enabling their venues:
  - BFI IMAX: [bfi.org.uk/bfi-imax](https://www.bfi.org.uk/bfi-imax) -> "Sign up to BFI IMAX emails"
  - Science Museum IMAX: [sciencemuseum.org.uk/see-and-do/dune-part-three](https://www.sciencemuseum.org.uk/see-and-do/dune-part-three) -> "Register for email alerts"

## Setup

```bash
cd dune-imax-watch
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config/config.example.yaml config/config.yaml
cp config/secrets.example.env config/secrets.env
chmod 600 config/secrets.env
# edit config/config.yaml and config/secrets.env with your real values

.venv/bin/python -m dune_watch init-db
```

Edit `config/config.yaml`:
- Confirm the two venues' URLs and keywords.
- Once you've signed up for BFI's newsletter, check what address it actually
  arrives from and adjust `venues[bfi_imax].imap.sender_filter` if needed.
- Adjust `opening_window` if the release date changes.

## Verify notifications before going live

```bash
.venv/bin/python -m dune_watch test-notify --config config/config.yaml --channel all
.venv/bin/python -m dune_watch run --dry-run --config config/config.yaml
```

`test-notify` sends one synthetic alert through your real, configured channels (so
you can confirm ntfy/SMTP/macOS actually work). `run --dry-run` exercises the full
fetch -> diff -> notify pipeline against synthetic data, without making any network
or IMAP call, and writes to a throwaway in-memory state DB.

## Run manually / once

```bash
.venv/bin/python -m dune_watch run --once --config config/config.yaml
.venv/bin/python -m dune_watch status --config config/config.yaml
```

## Be in the queue before it opens (`prearm`)

Because Queue-it randomises everyone already waiting when a sale opens, the single most
useful thing automation can do is make sure you are in the waiting room *before* the
countdown ends, signed in, with payment already on file.

```bash
# Optional extra - not installed by default, so the headless VM stays browser-free
.venv/bin/pip install -r requirements-assist.txt
.venv/bin/python -m playwright install chromium

# One-time: sign in by hand in the dedicated profile, and it is remembered after that
.venv/bin/python -m dune_watch prearm --venue science_museum_imax

# On the day: sit in the pre-queue from 08:45 for a 09:00 on-sale
.venv/bin/python -m dune_watch prearm --venue science_museum_imax --at 08:45

# See exactly what it would do, without launching anything
.venv/bin/python -m dune_watch prearm --venue science_museum_imax --at 08:45 --dry-run
```

It opens an ordinary headful Chromium holding your own session, navigates to the venue's
official booking URL once, brings the window to the front, and then **stops** - it never
refreshes (reloading a waiting-room page can forfeit your place), never picks seats and
never touches payment. Run it from your laptop, not the VM: that is where your browser
profile lives.

## Deploy - macOS (launchd)

Secondary. Keep this machine for `prearm` (it holds your browser profile); it is not a
reliable watcher, because nothing runs while the laptop is asleep or off the network.

```bash
./scripts/install_launchd.sh
```

This copies `deploy/com.gilikazzaz.dune-watch.plist` to
`~/Library/LaunchAgents/`, loads it, and starts one run immediately. It re-invokes
the process every 20 minutes (`StartInterval`); each venue still respects its own
`poll_interval_minutes` inside the app. Logs land in `~/Library/Logs/dune-watch/`.

To stop: `launchctl unload ~/Library/LaunchAgents/com.gilikazzaz.dune-watch.plist`

## Deploy - GitHub Actions (free backup)

Actions runners are stateless between runs, so `.github/workflows/poll.yml` uses
the same "git scraping" pattern your prior Odyssey tracker used: each run reads
`config/config.github.yaml` (headless-safe: `macos_native` and
`auto_open_booking_link` are off), polls once, and commits `state/github_state.db`
back to the repo only if it changed. Caveat: GitHub can delay or occasionally skip
scheduled runs under high platform load, so it's best-effort on timing, not
sub-minute-precise like launchd/systemd.

1. Create a **private** GitHub repo (recommended - it holds no secrets itself, but
   there's no reason to make a personal tracker public) and push this project to it
   (see below - ask me and I'll do the `git init`/commit/push once you've created
   the repo and given me its URL).
2. In the repo's **Settings -> Secrets and variables -> Actions**, add these seven
   repository secrets yourself (add them in GitHub's UI directly - don't paste real
   passwords into chat or a terminal command for this):
   `DUNE_WATCH_NTFY_TOPIC`, `DUNE_WATCH_SMTP_USER`, `DUNE_WATCH_SMTP_PASS`,
   `DUNE_WATCH_IMAP_HOST`, `DUNE_WATCH_IMAP_PORT`, `DUNE_WATCH_IMAP_USER`,
   `DUNE_WATCH_IMAP_PASS` - same values as your local `config/secrets.env`.
3. The workflow runs on its `schedule` cron automatically once pushed. You can also
   trigger a run immediately from the repo's **Actions** tab -> "Poll Dune IMAX
   watch" -> **Run workflow**.

Run this *alongside* the VM as an independent backup. Each deployment keeps its own
state file, so while both are enabled you will get duplicate alerts for the same event.
That is a deliberate trade: for a one-shot on-sale, a duplicate notification costs far
less than a missed one. `config/config.github.yaml` leaves the Science Museum *page*
venue disabled (CI can't reach it) and runs the mailbox and Bluesky channels only.

## Deploy - cloud VM (PRIMARY, systemd)

This is the deployment that matters. A small always-on UK VM (~£4/month) is the only
place that can do all three of:

- poll `sciencemuseum.org.uk` at all (GitHub Actions IP ranges get `403`),
- poll it at the 20-second cadence its robots.txt permits during an on-sale,
- keep running while your laptop is asleep - the launchd logs show 14 consecutive DNS
  failures from exactly that, during which every notification channel failed too, so the
  silence was indistinguishable from "nothing has happened yet".

```bash
sudo useradd -r -s /usr/sbin/nologin dunewatch
sudo mkdir -p /opt/dune-imax-watch && sudo chown dunewatch:dunewatch /opt/dune-imax-watch
# rsync/git-clone the project to /opt/dune-imax-watch, then as the dunewatch user:
python3 -m venv /opt/dune-imax-watch/.venv
/opt/dune-imax-watch/.venv/bin/pip install -r /opt/dune-imax-watch/requirements.txt
cp deploy/dune-watch.env.example /opt/dune-imax-watch/deploy/dune-watch.env
chmod 600 /opt/dune-imax-watch/deploy/dune-watch.env  # fill in real secrets

sudo cp deploy/dune-watch.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dune-watch.service
journalctl -u dune-watch -f   # tail logs
```

The service runs `run --loop` as a long-lived `Type=simple` unit with `Restart=always`,
because per-venue scheduling (including the hot cadence) lives inside the process and a
systemd timer cannot express it. `deploy/dune-watch.timer` is deprecated and kept only
for the old one-shot style - do not enable both, or you get two pollers and doubled
alerts.

On a headless server set `notifications.channels.macos_native.enabled: false` and
`notifications.auto_open_booking_link.enabled: false` - there's no display or browser.
Keep `heartbeat_stale_after_minutes` non-zero here; this is the deployment that should
notice its own silence.

Check the secrets took:

```bash
/opt/dune-imax-watch/.venv/bin/python -m dune_watch run --once --venue bfi_imax
```

A `[AUTHENTICATIONFAILED]` here means the Gmail app password is wrong. Use the **same**
16-character app password for `DUNE_WATCH_IMAP_PASS` and `DUNE_WATCH_SMTP_PASS`.

## Running tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

All tests run against local fixtures/mocks - no network or IMAP calls are made.

## Adding a new venue

1. Re-check that venue's `robots.txt` and terms of use first - "official site"
   doesn't mean "scrapeable" (see the Vue Manchester example already in
   `config.example.yaml`, whose robots.txt disallows the exact booking paths).
2. If it fits the existing pattern (a public page, or a newsletter you can read via
   IMAP), reuse `html_page_diff` or `imap_newsletter` - just add a new block under
   `venues:` and set `enabled: true`.
3. Otherwise, add a new adapter class in `dune_watch/adapters/` implementing
   `Adapter.fetch() -> list[RawListing]`, and register it in
   `dune_watch/adapters/registry.py`.

## Troubleshooting

- **Logs**: launchd -> `~/Library/Logs/dune-watch/`; systemd -> `journalctl -u dune-watch`.
- **A venue keeps failing**: check `status` output for `consecutive_failures`; after
  `polling.failing_source_alert_after_cycles` (default 6) you'll get one INFO alert
  about it, not repeated noise.
- **Reset dedup state** (re-alerts on everything currently live - use sparingly):
  ```bash
  rm state.db && .venv/bin/python -m dune_watch init-db
  ```

## Scope and ethics note

This is a single-user personal tool, not a general scraping service.
`whatson.bfi.org.uk` is never scraped or browser-automated - it's
Cloudflare-challenge-protected and BFI's terms prohibit automated access, so BFI is
monitored purely by reading a newsletter you legitimately signed up for. The
Science Museum page is polled politely (a low-frequency, identifying User-Agent,
respecting its permissive robots.txt). The tool never fills forms, enters payment
details, or completes a purchase - alerts get you to the booking page fast; you
still do the booking.
