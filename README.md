# dune-imax-watch

A personal ticket-monitoring tool for **Dune: Part Three** IMAX 70mm screenings in
London. It watches for new listings, format announcements, ticket-sale
announcements, new showtime batches, and availability flipping from
unavailable/sold-out to bookable - then alerts you (macOS notification, [ntfy.sh](https://ntfy.sh)
push, and/or email) with the cinema, date/time, format, booking link, and an
urgency label. It never enters payment details or completes a purchase; on
HIGH/CRITICAL alerts it optionally auto-opens the booking link in your browser so
it's already loaded when you look.

## Architecture at a glance

Two venues, two different adapters, one shared diff engine:

- **Science Museum IMAX** (`science_museum_imax`, `html_page_diff` adapter) - polls
  the public `sciencemuseum.org.uk` listing page directly. Its robots.txt allows
  content pages and there's no bot wall.
- **BFI IMAX** (`bfi_imax`, `imap_newsletter` adapter) - BFI's actual ticketing site
  (`whatson.bfi.org.uk`) is Cloudflare-challenge-protected and BFI's terms prohibit
  automated access, so **it is never scraped or automated**. Instead, this adapter
  reads your own mailbox (read-only IMAP) for the official "BFI IMAX emails" alert
  you sign up for once, and turns matching emails into alerts.

Both adapters return a normalized `RawListing`; a pure diff function
(`engine/diff.py`) classifies each one against SQLite-persisted state into an
event type + urgency; a `Dispatcher` fans alerts out to every enabled notification
channel independently. Adding a third venue later is one config block (plus a new
adapter file only if it needs a genuinely new fetch strategy).

## Prerequisites

- Python 3.9+
- A mailbox with IMAP access (e.g. Gmail with an [app password](https://myaccount.google.com/apppasswords))
- Sign up for BFI IMAX's own email alert **before** enabling the `bfi_imax` venue:
  [bfi.org.uk/bfi-imax](https://www.bfi.org.uk/bfi-imax) -> "Sign up to BFI IMAX emails"

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

## Deploy - macOS (launchd)

```bash
./scripts/install_launchd.sh
```

This copies `deploy/com.gilikazzaz.dune-watch.plist` to
`~/Library/LaunchAgents/`, loads it, and starts one run immediately. It re-invokes
the process every 20 minutes (`StartInterval`); each venue still respects its own
`poll_interval_minutes` inside the app. Logs land in `~/Library/Logs/dune-watch/`.

To stop: `launchctl unload ~/Library/LaunchAgents/com.gilikazzaz.dune-watch.plist`

## Deploy - GitHub Actions (works even when your computer is off, free)

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

This and the macOS/systemd deployments aren't mutually exclusive, but running the
same poller in two places at once just means double notifications for the same
event (deduped independently per state file) - pick one primary.

## Deploy - cloud VM (optional, systemd)

An alternative to GitHub Actions if you want sub-minute timing or don't want state
committed to the repo: a small always-on VM, using the same SQLite state file
locally instead of committing it anywhere.

```bash
sudo useradd -r -s /usr/sbin/nologin dunewatch
sudo mkdir -p /opt/dune-imax-watch && sudo chown dunewatch:dunewatch /opt/dune-imax-watch
# rsync/git-clone the project to /opt/dune-imax-watch, then as the dunewatch user:
python3 -m venv /opt/dune-imax-watch/.venv
/opt/dune-imax-watch/.venv/bin/pip install -r /opt/dune-imax-watch/requirements.txt
cp deploy/dune-watch.env.example /opt/dune-imax-watch/deploy/dune-watch.env
chmod 600 /opt/dune-imax-watch/deploy/dune-watch.env  # fill in real secrets

sudo cp deploy/dune-watch.service deploy/dune-watch.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dune-watch.timer
journalctl -u dune-watch.service -f   # tail logs
```

On a headless server, set `notifications.channels.macos_native.enabled: false` and
`notifications.auto_open_booking_link.enabled: false` in `config.yaml` - there's no
display or default browser there.

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
