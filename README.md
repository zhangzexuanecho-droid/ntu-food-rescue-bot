# NTU Food Rescue Bot

A campus version of Too Good To Go for NTU: food stalls list surplus meals at a discount, and students reserve them via Telegram and collect with a claim code.

## Architecture

- **Two Telegram bots** built with `python-telegram-bot` v22 (async):
  - `student_bot.py` — browse available meals (5 per page), reserve with one tap (1 meal per student per day), view my reservations
  - `vendor_bot.py` — publish surplus meals via a five-step wizard (stall → title → prices → quantity → pickup window)
    and verify a student's claim code at pickup (button or `/verify CODE`), which marks the reservation as completed
- **Supabase (PostgreSQL)** — three tables: `stalls`, `listings`, `reservations` (`schema.sql`)
- **Role-based access** — each vendor account is linked to its own stall via `vendor_telegram_id`; admin Telegram IDs (configured in `.env`) can manage all stalls
- Reservations decrement stock with a compare-and-swap update (`WHERE quantity = <value just read>`, retried on conflict), so concurrent reservations can't oversell; if saving the reservation fails, the portion is put back. Unique 6-character claim codes for collection
- **Same-day only (optional)** — with `SAME_DAY_ONLY=true` in `.env`, only listings published today (Singapore time) can be browsed or reserved, and only today's reservations can be collected; earlier ones expire. Off by default so demo data stays visible; turn it on for a real pilot
- **Daily limit** — each student can reserve 1 meal per day (pending or collected); admin Telegram IDs are exempt so the bots can be tested and demoed

## Project layout

```
├── schema.sql         # database schema (run in Supabase SQL Editor)
├── common.py          # shared: .env loading, Supabase client, admin whitelist, DB keep-alive
├── student_bot.py     # student-facing bot
├── vendor_bot.py      # vendor-facing bot
├── requirements.txt   # Python dependencies
├── .env.example       # template for .env
├── deploy/            # systemd service for running 24/7 on Linux
└── PROPOSAL.md        # project proposal text
```

## Setup

1. Create the two bots with **BotFather** (student bot + vendor bot) and note their tokens.
2. Create a Supabase project and run `schema.sql` in the SQL Editor.
3. Create a `.env` file: `cp .env.example .env`, then fill it in (see template below).
4. Install dependencies into a virtual environment (Python 3.10+):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

5. Run the bots (two terminals, each with `source .venv/bin/activate` first):

   ```bash
   python student_bot.py
   python vendor_bot.py
   ```

   Only one instance of each bot may run at a time — a second one fails with a Telegram `Conflict` error.

> Free-tier Supabase projects are paused after a week of low database activity. While running, each bot queries the database every 6 hours to keep the project active (Supabase doesn't guarantee that automated traffic counts — only paid plans are exempt from pausing). If the bots log DNS errors such as `nodename nor servname provided`, restore the project from the Supabase dashboard; the bots reconnect on their own.

## Run 24/7 on a Linux machine

Both bots can run unattended as systemd user services that restart on crash and start at boot.

1. Clone and install (on Debian/Ubuntu, `sudo apt install python3-venv` first):

   ```bash
   git clone https://github.com/zhangzexuanecho-droid/ntu-food-rescue-bot.git ~/Dev/ntu-food-rescue-bot
   cd ~/Dev/ntu-food-rescue-bot
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```

2. Put the `.env` file in `~/Dev/ntu-food-rescue-bot` (copy it from your other machine, or `cp .env.example .env` and fill it in).
3. Install and start the services:

   ```bash
   mkdir -p ~/.config/systemd/user
   cp deploy/ntu-food-rescue@.service ~/.config/systemd/user/
   systemctl --user daemon-reload
   systemctl --user enable --now ntu-food-rescue@student ntu-food-rescue@vendor
   sudo loginctl enable-linger "$USER"   # keep running after logout and start at boot
   ```

4. Check status and logs:

   ```bash
   systemctl --user status ntu-food-rescue@student ntu-food-rescue@vendor
   journalctl --user -u ntu-food-rescue@student -f
   ```

To deploy new code: `git pull`, rerun `.venv/bin/pip install -r requirements.txt` if it changed, then `systemctl --user restart ntu-food-rescue@student ntu-food-rescue@vendor`.

On a laptop, keep it from sleeping: set `HandleLidSwitch=ignore` and `HandleLidSwitchExternalPower=ignore` in `/etc/systemd/logind.conf` (takes effect after a reboot) and turn off automatic suspend in the power settings.

### `.env` template

```
TELEGRAM_STUDENT_BOT_TOKEN=<from BotFather>
TELEGRAM_VENDOR_BOT_TOKEN=<from BotFather>
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_KEY=<service_role key>
VENDOR_ADMIN_TELEGRAM_IDS=123456789,987654321
SAME_DAY_ONLY=            # true for a real pilot; empty/false for the demo
```

> ⚠️ Never commit `.env` — it contains live secrets. It is already covered by `.gitignore`.
