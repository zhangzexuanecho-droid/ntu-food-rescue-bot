"""Handler-level tests for the student / vendor bots, using an in-memory fake of the Supabase query builder.

No Telegram or Supabase connection needed. Put this file at tests/test_bots.py and run from the repo root:

    uv run python tests/test_bots.py                      # SAME_DAY_ONLY off (demo mode): 5 + 6 eat-by + 6 my-listings + 6 publish scenarios
    SAME_DAY_ONLY=true uv run python tests/test_bots.py   # SAME_DAY_ONLY on: 16 + 6 eat-by + 6 my-listings + 6 publish scenarios
"""

import asyncio
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["VENDOR_ADMIN_TELEGRAM_IDS"] = "999"

from postgrest import APIError  # noqa: E402

import common  # noqa: E402
import student_bot  # noqa: E402
import vendor_bot  # noqa: E402
from telegram.ext import ConversationHandler  # noqa: E402


def ts(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


TODAY_START = datetime.strptime(common.today_start_utc(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
NOW = ts(datetime.now(timezone.utc))
YESTERDAY = ts(TODAY_START - timedelta(hours=2))  # 22:00 SGT the previous day


class FakeDB:
    def __init__(self):
        self.tables = {"stalls": [], "listings": [], "reservations": []}

    def table(self, name):
        return Query(self, name)

    def embed(self, name, row):
        row = dict(row)
        if name == "listings":
            row["stalls"] = next((s for s in self.tables["stalls"] if s["id"] == row["stall_id"]), None)
        if name == "reservations":
            listing = next((l for l in self.tables["listings"] if l["id"] == row["listing_id"]), None)
            row["listings"] = self.embed("listings", listing) if listing else None
        return row


class Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.filters, self.op, self.payload, self._limit = [], "select", None, None

    def select(self, *_):
        self.op = "select"
        return self

    def update(self, data):
        self.op, self.payload = "update", data
        return self

    def insert(self, data):
        self.op, self.payload = "insert", data
        return self

    def _f(self, fn):
        self.filters.append(fn)
        return self

    def eq(self, c, v): return self._f(lambda r: r.get(c) == v)
    def neq(self, c, v): return self._f(lambda r: r.get(c) != v)
    def gt(self, c, v): return self._f(lambda r: r.get(c) > v)
    def gte(self, c, v): return self._f(lambda r: r.get(c) >= v)
    def in_(self, c, vals): return self._f(lambda r: r.get(c) in vals)
    def order(self, *a, **k): return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = self.db.tables[self.name]
        if self.op == "insert":
            row = {"id": str(uuid.uuid4()), "created_at": NOW, **self.payload}
            if self.name == "reservations":
                row.setdefault("status", "pending")
                if any(r["claim_code"] == row["claim_code"] for r in rows):
                    raise APIError({"code": "23505", "message": "duplicate key"})
            rows.append(row)
            return SimpleNamespace(data=[row])
        matched = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in matched:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in matched])
        data = [self.db.embed(self.name, r) for r in matched]
        return SimpleNamespace(data=data[: self._limit] if self._limit else data)


def seed():
    db = FakeDB()
    db.tables["stalls"] = [
        {"id": "s1", "stall_name": "Chicken Rice", "canteen_location": "Canteen 2", "vendor_telegram_id": 101},
        {"id": "s2", "stall_name": "Mini Wok", "canteen_location": "Canteen 11", "vendor_telegram_id": 102},
    ]
    db.tables["listings"] = [
        {"id": "today1", "stall_id": "s1", "title": "Chicken Rice Set", "original_price": 4.8, "discount_price": 2.0,
         "quantity": 2, "pickup_time": "8-9pm", "created_at": NOW},
        {"id": "today2", "stall_id": "s2", "title": "Hor Fun", "original_price": 5.5, "discount_price": 2.8,
         "quantity": 3, "pickup_time": "8-9pm", "created_at": NOW},
        {"id": "old", "stall_id": "s1", "title": "Stale Curry", "original_price": 5, "discount_price": 2,
         "quantity": 4, "pickup_time": "8-9pm", "created_at": YESTERDAY},
    ]
    db.tables["reservations"] = [
        {"id": "r_old", "listing_id": "old", "student_telegram_id": 7, "claim_code": "OLD123",
         "status": "pending", "created_at": YESTERDAY},
    ]
    return db


def cb_update(data, user_id):
    q = MagicMock()
    q.data, q.from_user.id = data, user_id
    q.answer, q.edit_message_text, q.message.reply_text = AsyncMock(), AsyncMock(), AsyncMock()
    return SimpleNamespace(callback_query=q, effective_user=q.from_user, effective_message=q.message)


def msg_update(text, user_id):
    m = MagicMock()
    m.text, m.reply_text = text, AsyncMock()
    return SimpleNamespace(callback_query=None, message=m, effective_message=m,
                           effective_user=SimpleNamespace(id=user_id))


def ctx(args=None):
    return SimpleNamespace(args=args, user_data={})


def alert_text(u):
    return u.callback_query.answer.call_args.args[0] if u.callback_query.answer.call_args.args else ""


async def main():
    db = seed()
    student_bot.get_supabase = lambda: db
    vendor_bot.get_supabase = lambda: db
    sb, vb = student_bot.StudentBot(), vendor_bot.VendorBot()
    vb._notify_student = AsyncMock()  # never message real Telegram users from tests
    listings = {l["id"]: l for l in db.tables["listings"]}

    # 1. browse shows only today's listings
    u = cb_update("browse", 1)
    await sb.browse(u, ctx())
    text = u.callback_query.edit_message_text.call_args.args[0]
    assert "Chicken Rice Set" in text and "Hor Fun" in text and "Stale Curry" not in text, text
    print("✓ browse hides yesterday's listing")

    # 2. reserving yesterday's listing is refused, stock untouched
    u = cb_update("reserve:old", 1)
    await sb.reserve(u, ctx())
    assert "no longer available" in alert_text(u) and listings["old"]["quantity"] == 4
    print("✓ yesterday's listing can't be reserved")

    # 3. first reservation today succeeds
    u = cb_update("reserve:today1", 1)
    await sb.reserve(u, ctx())
    reply = u.callback_query.message.reply_text.call_args.args[0]
    assert "Reservation confirmed" in reply and listings["today1"]["quantity"] == 1, reply
    code1 = reply.split("Claim Code: ")[1].split("\n")[0]
    print("✓ first reservation ok, code", code1)

    # 4. second reservation the same day is refused, stock untouched
    u = cb_update("reserve:today2", 1)
    await sb.reserve(u, ctx())
    assert "already reserved a meal today" in alert_text(u) and listings["today2"]["quantity"] == 3
    print("✓ second reservation same day refused")

    # 5. a reservation from yesterday doesn't count towards today's limit (student 7)
    u = cb_update("reserve:today2", 7)
    await sb.reserve(u, ctx())
    reply7 = u.callback_query.message.reply_text.call_args.args[0]
    assert "Reservation confirmed" in reply7
    print("✓ yesterday's reservation doesn't block today")

    # 6. admin is exempt from the daily limit
    for lid in ("today1", "today2"):
        u = cb_update(f"reserve:{lid}", 999)
        await sb.reserve(u, ctx())
        assert "Reservation confirmed" in u.callback_query.message.reply_text.call_args.args[0]
    print("✓ admin can reserve more than once")

    # 7. sold out still works (today1 now 0)
    u = cb_update("reserve:today1", 2)
    await sb.reserve(u, ctx())
    assert listings["today1"]["quantity"] == 0 and "sold out" in alert_text(u)
    print("✓ sold-out handling intact")

    # 8. My Reservations shows only today's pending (student 7 has an old one + a new one)
    u = cb_update("my_reservations", 7)
    await sb.my_reservations(u, ctx())
    text = u.callback_query.edit_message_text.call_args.args[0]
    assert "Hor Fun" in text and "Stale Curry" not in text and "OLD123" not in text, text
    print("✓ My Reservations hides yesterday's")

    # 9. vendor of another stall can't see / verify the code
    u = msg_update(code1, 102)
    state = await vb._handle_code(u, code1)
    assert state == vendor_bot.VERIFY_CODE and "not found" in u.message.reply_text.call_args.args[0]
    print("✓ other stall's vendor gets not-found")

    # 10. bad format stays in VERIFY_CODE
    u = msg_update("abc", 101)
    assert await vb._handle_code(u, "abc") == vendor_bot.VERIFY_CODE
    print("✓ bad format re-asks")

    # 11. correct vendor verifies (lower-case + spaces accepted)
    messy = " ".join(code1.lower())
    u = msg_update(messy, 101)
    state = await vb._handle_code(u, messy)
    reply = u.message.reply_text.call_args.args[0]
    r1 = next(r for r in db.tables["reservations"] if r["claim_code"] == code1)
    assert state == ConversationHandler.END and "verified" in reply and "Chicken Rice Set" in reply
    assert r1["status"] == "completed"
    print("✓ vendor verifies own stall's code -> completed")

    # 12. same code again -> already used
    u = msg_update(code1, 101)
    await vb._handle_code(u, code1)
    assert "already been used" in u.message.reply_text.call_args.args[0]
    print("✓ code can't be used twice")

    # 13. completed reservation still counts for the daily limit
    u = cb_update("reserve:today2", 1)
    await sb.reserve(u, ctx())
    assert "already reserved a meal today" in alert_text(u)
    print("✓ completed reservation still counts toward limit")

    # 14. yesterday's code -> expired, status unchanged
    u = msg_update("OLD123", 101)
    await vb._handle_code(u, "OLD123")
    assert "expired" in u.message.reply_text.call_args.args[0]
    assert next(r for r in db.tables["reservations"] if r["id"] == "r_old")["status"] == "pending"
    print("✓ yesterday's code expired")

    # 15. admin can verify any stall's code
    code7 = reply7.split("Claim Code: ")[1].split("\n")[0]
    u = msg_update(code7, 999)
    await vb._handle_code(u, code7)
    assert "verified" in u.message.reply_text.call_args.args[0]
    print("✓ admin verifies any stall")

    # 16. entry points: unlinked user locked out; linked vendor prompted; /verify CODE verifies directly
    u = cb_update("verify", 555)
    assert await vb.start_verify(u, ctx()) == ConversationHandler.END
    assert "not linked" in u.callback_query.edit_message_text.call_args.args[0]
    u = cb_update("verify", 101)
    assert await vb.start_verify(u, ctx()) == vendor_bot.VERIFY_CODE
    assert "Enter the student's" in u.callback_query.edit_message_text.call_args.args[0]
    u = msg_update("/verify OLD123", 101)
    assert await vb.start_verify(u, ctx(["OLD123"])) == ConversationHandler.END
    assert "expired" in u.message.reply_text.call_args.args[0]
    print("✓ verify entry points")

    print("\nALL PASSED")


async def demo_mode():
    """SAME_DAY_ONLY off (default): earlier listings/reservations stay usable, daily limit still applies."""
    db = seed()
    student_bot.get_supabase = lambda: db
    vendor_bot.get_supabase = lambda: db
    sb, vb = student_bot.StudentBot(), vendor_bot.VendorBot()
    vb._notify_student = AsyncMock()  # never message real Telegram users from tests
    listings = {l["id"]: l for l in db.tables["listings"]}

    u = cb_update("browse", 1)
    await sb.browse(u, ctx())
    text = u.callback_query.edit_message_text.call_args.args[0]
    assert "Stale Curry" in text and "Chicken Rice Set" in text, text
    print("✓ browse shows earlier listing in demo mode")

    u = cb_update("reserve:old", 1)
    await sb.reserve(u, ctx())
    assert "Reservation confirmed" in u.callback_query.message.reply_text.call_args.args[0]
    assert listings["old"]["quantity"] == 3
    print("✓ earlier listing reservable")

    u = cb_update("reserve:today1", 1)
    await sb.reserve(u, ctx())
    assert "already reserved a meal today" in alert_text(u)
    print("✓ daily limit still on")

    u = cb_update("my_reservations", 7)
    await sb.my_reservations(u, ctx())
    assert "OLD123" in u.callback_query.edit_message_text.call_args.args[0]
    print("✓ My Reservations shows earlier pending")

    u = msg_update("OLD123", 101)
    await vb._handle_code(u, "OLD123")
    assert "verified" in u.message.reply_text.call_args.args[0]
    print("✓ earlier code verifiable")

    print("\nALL PASSED (demo mode)")


async def eat_by_checks():
    """Eat-by reminder (listing time + 3h, demo: reservation time + 3h) and demo-stall exemption. Both modes."""
    db = seed()
    db.tables["stalls"].append(
        {"id": "s3", "stall_name": "Demo Stall", "canteen_location": "Demo", "vendor_telegram_id": None})
    db.tables["listings"].append(
        {"id": "demo_old", "stall_id": "s3", "title": "Demo Laksa", "original_price": 5, "discount_price": 2,
         "quantity": 5, "pickup_time": "8-9pm", "created_at": YESTERDAY})
    student_bot.get_supabase = lambda: db
    vendor_bot.get_supabase = lambda: db
    sb, vb = student_bot.StudentBot(), vendor_bot.VendorBot()
    vb._notify_student = AsyncMock()

    # fmt_sg_time: today -> time only, other day -> with date
    now_sg = datetime.now(common.SG_TZ)
    assert ":" in common.fmt_sg_time(now_sg) and "," not in common.fmt_sg_time(now_sg)
    assert "," in common.fmt_sg_time(now_sg - timedelta(days=2))
    print("✓ fmt_sg_time shows the date only when it isn't today")

    # real listing: eat by = listing time + 3h
    u = cb_update("reserve:today1", 21)
    await sb.reserve(u, ctx())
    reply = u.callback_query.message.reply_text.call_args.args[0]
    expected = common.fmt_sg_time(common.parse_ts(NOW) + common.EAT_WITHIN)
    assert f"Please eat by {expected}" in reply, reply
    code_real = reply.split("Claim Code: ")[1].split("\n")[0]
    print("✓ reservation shows eat-by =", expected)

    # demo listing from yesterday: visible and reservable even with SAME_DAY_ONLY on
    u = cb_update("browse", 22)
    await sb.browse(u, ctx())
    assert "Demo Laksa" in u.callback_query.edit_message_text.call_args.args[0]
    u = cb_update("reserve:demo_old", 22)
    before = datetime.now(timezone.utc)
    await sb.reserve(u, ctx())
    reply = u.callback_query.message.reply_text.call_args.args[0]
    assert "Reservation confirmed" in reply, reply
    shown = reply.split("Please eat by ")[1].split(" (")[0]
    assert shown in {common.fmt_sg_time(before + common.EAT_WITHIN),
                     common.fmt_sg_time(before + common.EAT_WITHIN + timedelta(minutes=1))}, shown
    code_demo = reply.split("Claim Code: ")[1].split("\n")[0]
    print("✓ demo listing from yesterday reservable, eat-by = now + 3h:", shown)

    # My Reservations shows eat-by
    u = cb_update("my_reservations", 22)
    await sb.my_reservations(u, ctx())
    assert "Eat by" in u.callback_query.edit_message_text.call_args.args[0]
    print("✓ My Reservations shows eat-by")

    # verify: vendor reminded, student notified via student bot
    u = msg_update(code_real, 101)
    await vb._handle_code(u, code_real)
    assert f"eat it by {expected}" in u.message.reply_text.call_args.args[0]
    tg_id, text = vb._notify_student.call_args.args
    assert tg_id == 21 and f"eat it by {expected}" in text and "Chicken Rice Set" in text, text
    print("✓ verify reminds vendor and notifies student")

    # demo reservation made yesterday is not expired, even with SAME_DAY_ONLY on
    r = next(r for r in db.tables["reservations"] if r["claim_code"] == code_demo)
    r["created_at"] = YESTERDAY
    vb._notify_student.reset_mock()
    u = msg_update(code_demo, 999)
    await vb._handle_code(u, code_demo)
    assert "verified" in u.message.reply_text.call_args.args[0] and r["status"] == "completed"
    assert vb._notify_student.await_count == 1
    print("✓ demo reservation from yesterday still verifiable")

    print("\nALL PASSED (eat-by)")


async def my_listings_checks():
    """Vendor My Listings: own stall only, counts, summary, stop selling with confirmation. Both modes."""
    db = seed()
    db.tables["reservations"] += [
        {"id": f"r_{st}", "listing_id": "today1", "student_telegram_id": 40 + i, "claim_code": f"CODE{i}0",
         "status": st, "created_at": NOW}
        for i, st in enumerate(("pending", "completed", "cancelled"))
    ]
    student_bot.get_supabase = lambda: db
    vendor_bot.get_supabase = lambda: db
    sb, vb = student_bot.StudentBot(), vendor_bot.VendorBot()
    listings = {l["id"]: l for l in db.tables["listings"]}

    # own stall via /listings: counts (cancelled ignored), summary line, other stall hidden
    u = msg_update("/listings", 101)
    await vb.my_listings(u, ctx())
    text = u.message.reply_text.call_args.args[0]
    assert "Chicken Rice Set" in text and "Hor Fun" not in text, text
    assert "Listed 4 · 🟡 Reserved 1 · ✅ Collected 1 · Left 2" in text, text
    if common.same_day_only():
        assert "Today: 4 listed · 1 reserved · 1 collected" in text and "Stale Curry" not in text, text
    else:
        assert "All listings: 9 listed · 2 reserved · 1 collected" in text and "Stale Curry" in text, text
    print("✓ My Listings: own stall only, counts and summary")

    # admin sees all stalls; unlinked account is told to link
    u = msg_update("/listings", 999)
    await vb.my_listings(u, ctx())
    assert "Hor Fun" in u.message.reply_text.call_args.args[0]
    u = cb_update("mylist", 555)
    await vb.my_listings(u, ctx())
    assert "not linked" in u.callback_query.edit_message_text.call_args.args[0]
    print("✓ admin sees all stalls, unlinked vendor locked out")

    # another stall's vendor can't open the confirmation or stop it (forged callback data)
    u = cb_update("stop:today1:0", 102)
    await vb.confirm_stop(u, ctx())
    assert "isn't available" in u.callback_query.edit_message_text.call_args.args[0]
    assert u.callback_query.answer.await_count == 1
    u = cb_update("stopyes:today1:0", 102)
    await vb.do_stop(u, ctx())
    assert listings["today1"]["quantity"] == 2
    print("✓ other stall's vendor can't stop selling")

    # own vendor: confirmation first, then stop -> quantity 0, students no longer see it
    u = cb_update("stop:today1:0", 101)
    await vb.confirm_stop(u, ctx())
    assert "Stop selling Chicken Rice Set?" in u.callback_query.edit_message_text.call_args.args[0]
    assert listings["today1"]["quantity"] == 2 and u.callback_query.answer.await_count == 1
    u = cb_update("stopyes:today1:0", 101)
    await vb.do_stop(u, ctx())
    text = u.callback_query.edit_message_text.call_args.args[0]
    rows = u.callback_query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
    assert listings["today1"]["quantity"] == 0 and text.startswith("🛑 Stopped selling Chicken Rice Set")
    assert "Left 0" in text and not any("Chicken Rice Set" in b.text for row in rows for b in row)
    assert u.callback_query.answer.await_count == 1
    u = cb_update("browse", 50)
    await sb.browse(u, ctx())
    assert "Chicken Rice Set" not in u.callback_query.edit_message_text.call_args.args[0]
    print("✓ stop selling: confirm, quantity 0, hidden from students")

    # the pending reservation can still be verified after stopping
    u = msg_update("CODE00", 101)
    vb._notify_student = AsyncMock()
    await vb._handle_code(u, "CODE00")
    assert "verified" in u.message.reply_text.call_args.args[0]
    print("✓ existing reservation still collectable after stop")

    print("\nALL PASSED (my listings)")


async def publish_checks():
    """Publish flow quick options: recent meals, discount %, quantity and pickup-window buttons. Both modes."""
    db = seed()
    student_bot.get_supabase = lambda: db
    vendor_bot.get_supabase = lambda: db
    vb = vendor_bot.VendorBot()
    new = lambda: [l for l in db.tables["listings"] if l["id"] not in {"today1", "today2", "old"}]

    def buttons(u):
        call = u.callback_query.edit_message_text.call_args if u.callback_query else u.message.reply_text.call_args
        markup = call.kwargs.get("reply_markup")
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] if markup else []

    assert vb._discounted(6.5, 30) == 4.6 and vb._discounted(5, 50) == 2.5
    print("✓ discount rounds to 10 cents")

    # tap path: recent meal -> quantity button -> pickup window button
    c = ctx()
    u = cb_update("add_meal", 101)
    assert await vb.start_add_meal(u, c) == vendor_bot.MEAL_TITLE
    recent = {text: data for text, data in buttons(u)}
    label = next(t for t in recent if "Chicken Rice Set" in t)
    assert "$4.80 → $2.00" in label and any("Stale Curry" in t for t in recent)
    assert await vb.pick_recent(cb_update(f"recent:{c.user_data['flow']}:99", 101), c) == vendor_bot.MEAL_TITLE  # forged index
    u = cb_update(recent[label], 101)
    assert await vb.pick_recent(u, c) == vendor_bot.QUANTITY
    assert "Step 4/5" in u.callback_query.edit_message_text.call_args.args[0]
    assert [t for t, _ in buttons(u)] == ["1", "2", "3", "5", "10"]
    print("✓ recent meal fills title + prices and skips to step 4")

    assert await vb.pick_quantity(cb_update(f"qty:{c.user_data['flow']}:7", 101), c) == vendor_bot.QUANTITY  # not an option
    u = cb_update(f"qty:{c.user_data['flow']}:5", 101)
    assert await vb.pick_quantity(u, c) == vendor_bot.PICKUP_WINDOW
    windows = [t.removeprefix("🕖 ") for t, _ in buttons(u)]
    assert len(windows) == 3 and len(set(windows)) == 3 and all(" - " in w for w in windows), windows
    assert await vb.pick_window(cb_update(f"win:{c.user_data['flow']}:5", 101), c) == vendor_bot.PICKUP_WINDOW  # forged index
    old_flow_win = f"win:{c.user_data['flow']}:0"
    u = cb_update(f"win:{c.user_data['flow']}:1", 101)
    assert await vb.pick_window(u, c) == ConversationHandler.END
    row = new()[-1]
    assert (row["title"], row["original_price"], row["discount_price"], row["quantity"], row["pickup_time"]) == \
        ("Chicken Rice Set", 4.8, 2.0, 5, windows[1]), row
    assert "Listing published" in u.callback_query.edit_message_text.call_args.args[0]
    print("✓ quantity + pickup buttons publish:", windows[1])

    # typed path: title -> original price -> discount button -> typed quantity -> typed pickup
    c = ctx()
    await vb.start_add_meal(cb_update("add_meal", 101), c)
    assert await vb.meal_title(msg_update("Nasi Lemak", 101), c) == vendor_bot.PRICES
    u = msg_update("5.00", 101)
    assert await vb.prices(u, c) == vendor_bot.PRICES
    assert ("50% off · $2.50", f"disc:{c.user_data['flow']}:50") in buttons(u), buttons(u)
    assert await vb.pick_discount(cb_update(f"disc:{c.user_data['flow']}:45", 101), c) == vendor_bot.PRICES  # not an option
    assert "original_price" not in c.user_data
    await vb.prices(msg_update("5.00", 101), c)
    assert await vb.pick_discount(cb_update(f"disc:{c.user_data['flow']}:50", 101), c) == vendor_bot.QUANTITY
    assert await vb.quantity(msg_update("3", 101), c) == vendor_bot.PICKUP_WINDOW
    assert await vb.pickup_window(msg_update("7:30 PM - 8:30 PM", 101), c) == ConversationHandler.END
    row = new()[-1]
    assert (row["title"], row["discount_price"], row["quantity"], row["pickup_time"]) == \
        ("Nasi Lemak", 2.5, 3, "7:30 PM - 8:30 PM"), row
    print("✓ typed title + discount button + typed quantity/pickup")

    # buttons from an earlier publish flow are ignored (flow id mismatch), even if the index is in range
    c = ctx()
    await vb.start_add_meal(cb_update("add_meal", 101), c)
    first_flow_recent = f"recent:{c.user_data['flow']}:0"
    await vb.start_add_meal(cb_update("add_meal", 101), c)          # vendor restarts the flow
    assert await vb.pick_recent(cb_update(first_flow_recent, 101), c) == vendor_bot.MEAL_TITLE
    assert "title" not in c.user_data
    c.user_data.update(title="X", original_price=5.0, discount_price=2.0, quantity=1, windows=["a", "b"])
    before = len(db.tables["listings"])
    assert await vb.pick_window(cb_update(old_flow_win, 101), c) == vendor_bot.PICKUP_WINDOW
    assert len(db.tables["listings"]) == before
    print("✓ stale buttons from an earlier flow are ignored")

    # typed discounted price, invalid input, and the old one-line format
    c = ctx()
    await vb.start_add_meal(cb_update("add_meal", 101), c)
    await vb.meal_title(msg_update("Mee Goreng", 101), c)
    assert await vb.prices(msg_update("6, 9", 101), c) == vendor_bot.PRICES
    assert await vb.prices(msg_update("abc", 101), c) == vendor_bot.PRICES
    await vb.prices(msg_update("5", 101), c)
    assert await vb.prices(msg_update("6", 101), c) == vendor_bot.PRICES  # discount above original
    assert await vb.prices(msg_update("1.80", 101), c) == vendor_bot.QUANTITY
    assert c.user_data["discount_price"] == 1.8
    c = ctx()
    await vb.start_add_meal(cb_update("add_meal", 101), c)
    await vb.meal_title(msg_update("Mee Goreng", 101), c)
    assert await vb.prices(msg_update("6.50, 3.00", 101), c) == vendor_bot.QUANTITY
    print("✓ typed discount, invalid prices re-asked, 'orig, disc' still works")

    print("\nALL PASSED (publish)")


async def run():
    await (main() if common.same_day_only() else demo_mode())
    await eat_by_checks()
    await my_listings_checks()
    await publish_checks()


asyncio.run(run())
