# NTU Food Rescue Bot

A campus version of Too Good To Go for NTU: food stalls list surplus meals at a discount, and students reserve them via Telegram and collect with a claim code.

## Architecture

- **Two Telegram bots** built with `python-telegram-bot` v22 (async):
  - `student_bot.py` — browse available meals (5 per page), reserve with one tap (1 meal per student per day), view my reservations
  - `vendor_bot.py` — publish surplus meals via a five-step wizard (stall → title → prices → quantity → pickup window);
    every step offers tappable options and still accepts typing: meals the stall listed before (one tap reuses name and
    prices), discount buttons (30–60% off, rounded to 10 cents), quantity 1/2/3/5/10, and pickup windows starting now
    and verify a student's claim code at pickup (button or `/verify CODE`), which marks the reservation as completed
    and see **My Listings** (button or `/listings`): per listing how many were listed / reserved / collected / left, a summary line on top, and a 🛑 Stop selling button (with confirmation) that hides the rest from students
- **Supabase (PostgreSQL)** — three tables: `stalls`, `listings`, `reservations` (`schema.sql`)
- **Role-based access** — each vendor account is linked to its own stall via `vendor_telegram_id`; admin Telegram IDs (configured in `.env`) can manage all stalls
- Reservations decrement stock with a compare-and-swap update (`WHERE quantity = <value just read>`, retried on conflict), so concurrent reservations can't oversell; if saving the reservation fails, the portion is put back. Unique 6-character claim codes for collection
- **Same-day only (optional)** — with `SAME_DAY_ONLY=true` in `.env`, only listings published today (Singapore time) can be browsed or reserved, and only today's reservations can be collected; earlier ones expire. Off by default so demo data stays visible; turn it on for a real pilot. Stalls not linked to a vendor (`vendor_telegram_id` empty) count as demo stalls and are always exempt
- **Eat-by reminder** — the reservation confirmation, My Reservations and the vendor's verify reply show when the meal should be eaten: listing time + 3 hours (food is assumed freshly cooked when listed; 1 hour of margin under SFA's 4-hour rule). For demo stalls it is reservation time + 3 hours. After a vendor verifies a code, the student bot also messages the student this reminder
- **Auto-release** — a reservation not collected within 30 minutes is cancelled, its portion goes back on sale and the student is notified (the student can reserve again). A stopped listing (`stopped = true`) is never restocked, neither by a release nor by a failed-reservation rollback
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

## Database migrations

Existing projects created from an older `schema.sql` need these, run once in the Supabase SQL Editor **before** deploying the matching code:

```sql
-- 2026-11: stop-selling flag (auto-release must not restock stopped listings)
alter table public.listings add column if not exists stopped boolean not null default false;

-- 2026-11: how many portions were originally listed (quantity changes with reservations / stop selling)
alter table public.listings add column if not exists initial_quantity integer check (initial_quantity >= 0);
update public.listings l set initial_quantity = l.quantity + (
    select count(*) from public.reservations r where r.listing_id = l.id and r.status <> 'cancelled'
) where l.initial_quantity is null;
```

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
