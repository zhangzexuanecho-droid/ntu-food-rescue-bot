# NTU Food Rescue Bot

A campus version of Too Good To Go for NTU: food stalls list surplus meals at a discount, and students reserve them via Telegram and collect with a claim code.

## Architecture

- **Two Telegram bots** built with `python-telegram-bot` v20+ (async):
  - `student_bot.py` — browse available meals (5 per page), reserve with one tap, view my reservations
  - `vendor_bot.py` — publish surplus meals via a five-step wizard (stall → title → prices → quantity → pickup window)
- **Supabase (PostgreSQL)** — three tables: `stalls`, `listings`, `reservations` (`schema.sql`)
- **Role-based access** — each vendor account is linked to its own stall via `vendor_telegram_id`; admin Telegram IDs (configured in `.env`) can manage all stalls
- Reservations decrement stock with a compare-and-swap update (`WHERE quantity = <value just read>`, retried on conflict), so concurrent reservations can't oversell; if saving the reservation fails, the portion is put back. Unique 6-character claim codes for collection

## Project layout

```
├── schema.sql       # database schema (run in Supabase SQL Editor)
├── common.py        # shared: .env loading, Supabase client, admin whitelist
├── student_bot.py   # student-facing bot
├── vendor_bot.py    # vendor-facing bot
└── PROPOSAL.md      # project proposal text
```

## Setup

1. Create the two bots with **BotFather** (student bot + vendor bot) and note their tokens.
2. Create a Supabase project and run `schema.sql` in the SQL Editor.
3. Create a `.env` file (see template below).
4. Install dependencies into a virtual environment (Python 3.10+):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install "python-telegram-bot>=20.0" "supabase>=2.0" python-dotenv
   ```

5. Run the bots (two terminals, each with `source .venv/bin/activate` first):

   ```bash
   python student_bot.py
   python vendor_bot.py
   ```

   Only one instance of each bot may run at a time — a second one fails with a Telegram `Conflict` error.

> Free-tier Supabase projects are paused after a week of inactivity. If the bots log DNS errors such as `nodename nor servname provided`, restore the project from the Supabase dashboard; the bots reconnect on their own.

### `.env` template

```
TELEGRAM_STUDENT_BOT_TOKEN=<from BotFather>
TELEGRAM_VENDOR_BOT_TOKEN=<from BotFather>
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_KEY=<service_role key>
VENDOR_ADMIN_TELEGRAM_IDS=123456789,987654321
```

> ⚠️ Never commit `.env` — it contains live secrets. It is already covered by `.gitignore`.
