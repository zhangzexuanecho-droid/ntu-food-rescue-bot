"""
学生端 Bot（NTU Food Rescue）：浏览剩余餐食、一键预订、查看我的预订。

运行：
    python student_bot.py

对应 .env 变量：
    TELEGRAM_STUDENT_BOT_TOKEN   学生 Bot 的 token（BotFather 获取）
    SUPABASE_URL / SUPABASE_KEY  与商家端共用
"""

import logging
import secrets
import string
from datetime import datetime, timezone

from postgrest import APIError
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from common import (
    eat_by,
    fmt_sg_time,
    get_admin_ids,
    get_bot_token,
    get_supabase,
    listed_today_or_demo,
    parse_ts,
    schedule_db_keepalive,
    today_start_utc,
)

logger = logging.getLogger(__name__)

# 学生端主菜单
STUDENT_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("🍱 Browse Available Food", callback_data="browse")],
        [InlineKeyboardButton("📋 My Reservations", callback_data="my_reservations")],
    ]
)

# 浏览页每页的餐品数：整页内容在一条消息里，Telegram 单条消息上限 4096 字符，必须分页
PAGE_SIZE = 5

# 核销码：6 位大写字母 + 数字，用 secrets 生成，不可预测
CLAIM_CODE_ALPHABET = string.ascii_uppercase + string.digits
CLAIM_CODE_LENGTH = 6

# PostgreSQL unique_violation 错误码
UNIQUE_VIOLATION = "23505"


class StudentBot:
    """学生端全部业务逻辑。"""

    def __init__(self) -> None:
        self.supabase = get_supabase()

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------

    async def _render_menu(self, query: CallbackQuery, text: str) -> None:
        """在回调所在消息上渲染主菜单；编辑失败（如内容未变）则另发新消息。"""
        try:
            await query.edit_message_text(text, reply_markup=STUDENT_MENU)
        except Exception:
            await query.message.reply_text(text, reply_markup=STUDENT_MENU)

    async def _edit(self, query: CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
        """编辑回调所在消息；用户连点同一按钮时内容不变，忽略 Telegram 的 not modified 报错。"""
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise

    # ------------------------------------------------------------------
    # /start 与返回菜单
    # ------------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """发送欢迎语与学生端主菜单。"""
        await update.message.reply_text(
            "👋 Welcome to NTU Food Rescue!\n\n"
            "Rescue surplus canteen meals at a discount before they go to waste 🌏\n\n"
            "What would you like to do?",
            reply_markup=STUDENT_MENU,
        )

    async def back_to_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """列表页的「返回主菜单」按钮。"""
        query = update.callback_query
        await query.answer()
        await self._render_menu(query, "What would you like to do?")

    # ------------------------------------------------------------------
    # 浏览 + 预订
    # ------------------------------------------------------------------

    async def browse(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """查询 quantity > 0 的 listings（join stalls），分页展示并附 Reserve 按钮。

        SAME_DAY_ONLY 打开时只显示今天发布的：之前日子没卖完的不再显示，只卖当天做的食物。
        demo 摊位（没绑定商家）的餐品不受这个限制，随时都能演示，所以这里在 Python 里筛选。

        callback_data 为 "browse"（第一页）或 "browse:<页码>"（翻页，页码从 0 开始）。
        """
        query = update.callback_query
        await query.answer()
        _, _, page_arg = query.data.partition(":")
        page = int(page_arg) if page_arg.isdigit() else 0

        try:
            res = (
                self.supabase.table("listings")
                .select(
                    "id, title, original_price, discount_price, quantity, pickup_time, created_at, "
                    "stalls(stall_name, canteen_location, vendor_telegram_id)"
                )
                .gt("quantity", 0)
                .order("created_at", desc=True)
                .execute()
            )
            items = [
                item for item in res.data or []
                if listed_today_or_demo(item["created_at"], item.get("stalls"))
            ]
        except Exception as exc:
            logger.error("查询 listings 失败: %s", exc)
            await self._render_menu(query, "⚠️ Something went wrong. Please try again.")
            return

        if not items:
            await self._render_menu(query, "😕 No surplus meals available right now.\n\nCheck back soon!")
            return

        # 翻页期间可能有餐品售罄、总页数变少，页码越界时落到最后一页
        pages = (len(items) + PAGE_SIZE - 1) // PAGE_SIZE
        page = min(page, pages - 1)
        start = page * PAGE_SIZE
        page_items = items[start : start + PAGE_SIZE]

        lines = [f"🍱 Available Surplus Meals (page {page + 1}/{pages})\n"]
        for i, item in enumerate(page_items, start + 1):
            stall = item.get("stalls") or {}
            lines.append(
                f"{i}. {item['title']}\n"
                f"   📍 {stall.get('stall_name', 'N/A')} — {stall.get('canteen_location', 'N/A')}\n"
                f"   💰 ${float(item['original_price']):.2f} → ${float(item['discount_price']):.2f}\n"
                f"   📦 {item['quantity']} left | 🕖 {item.get('pickup_time') or 'TBA'}\n"
            )

        keyboard = [
            [
                InlineKeyboardButton(
                    f"✅ Reserve: {item['title'][:40]}",
                    callback_data=f"reserve:{item['id']}",
                )
            ]
            for item in page_items
        ]
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ Prev", callback_data=f"browse:{page - 1}"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("Next ▶️", callback_data=f"browse:{page + 1}"))
        if nav:
            keyboard.append(nav)
        keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_menu")])

        await self._edit(
            query,
            "\n".join(lines) + "\nTap a button to reserve:",
            InlineKeyboardMarkup(keyboard),
        )

    async def reserve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        处理 Reserve Now：
          1. 取最新详情（列表页数据可能已过期；SAME_DAY_ONLY 打开时只接受今天发布的或 demo 摊位的 listing）
          2. 每个学生每天限订一份（管理员不受限，方便测试和演示）
          3. CAS 扣减一份库存，售罄则弹窗提示
          4. 写入 reservations（含 6 位随机 claim_code），写入失败则回补库存
          5. 发送核销码确认消息

        PTB 默认逐个处理 update（未开启 concurrent_updates），同一 bot 进程内不会有两次预订并发执行，
        所以第 2 步「先查再写」不会被同一学生的连点绕过。

        一个 callback query 只能 answer 一次：失败分支用弹窗 answer，成功后再空 answer，
        不能在开头先 answer()，否则后面的弹窗会失败、用户看不到提示。
        """
        query = update.callback_query
        listing_id = query.data.split(":", 1)[1]
        user = query.from_user

        try:
            res = (
                self.supabase.table("listings")
                .select(
                    "id, title, quantity, pickup_time, created_at, "
                    "stalls(stall_name, canteen_location, vendor_telegram_id)"
                )
                .eq("id", listing_id)
                .execute()
            )
            if not res.data or not listed_today_or_demo(
                res.data[0]["created_at"], res.data[0].get("stalls")
            ):
                await query.answer("😔 This listing is no longer available.", show_alert=True)
                return
            listing = res.data[0]

            if user.id not in get_admin_ids() and self._has_reservation_today(user.id):
                await query.answer(
                    "🙏 You've already reserved a meal today.\n\n"
                    "To keep things fair, each student can reserve 1 meal per day.",
                    show_alert=True,
                )
                return

            if not self._change_stock(listing_id, -1):
                await query.answer("😔 Sorry, this item is sold out!", show_alert=True)
                return

            try:
                claim_code = self._insert_reservation(listing_id, user.id)
            except Exception:
                # 库存已扣但预订没写进去：把这一份还回去，避免库存凭空减少
                self._release_stock(listing_id)
                raise
        except Exception as exc:
            logger.error("预订失败 listing_id=%s: %s", listing_id, exc)
            await query.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
            return

        await query.answer()
        stall = listing.get("stalls") or {}
        deadline = eat_by(listing["created_at"], datetime.now(timezone.utc), stall)
        await query.message.reply_text(
            "🎉 Reservation confirmed!\n\n"
            f"🍽️ {listing['title']}\n"
            f"📍 {stall.get('stall_name', 'N/A')} — {stall.get('canteen_location', 'N/A')}\n"
            f"🕖 Pickup: {listing.get('pickup_time') or 'TBA'}\n"
            f"⏰ Please eat by {fmt_sg_time(deadline)} (food safety)\n\n"
            f"🎟️ Your Claim Code: {claim_code}\n\n"
            "Show this code at the stall to collect your meal. Enjoy!"
        )

    def _has_reservation_today(self, telegram_id: int) -> bool:
        """该学生今天是否已有未取消的预订（pending 或 completed 都算）。"""
        res = (
            self.supabase.table("reservations")
            .select("id")
            .eq("student_telegram_id", telegram_id)
            .neq("status", "cancelled")
            .gte("created_at", today_start_utc())
            .limit(1)
            .execute()
        )
        return bool(res.data)

    def _change_stock(self, listing_id: str, delta: int) -> bool:
        """以 CAS（compare-and-swap）方式把库存改变 delta，返回是否成功。

        PostgREST 无法原生执行 quantity = quantity + delta，所以先读到当前值 q，再执行
        UPDATE ... SET quantity = q + delta WHERE id = ? AND quantity = q。
        期间若被其他请求改过，WHERE 不命中，重读后重试，因此不会丢失扣减，也不会超卖。
        listing 不存在或库存不足时返回 False。
        """
        for _ in range(5):
            res = self.supabase.table("listings").select("quantity").eq("id", listing_id).execute()
            if not res.data:
                return False
            qty = int(res.data[0]["quantity"])
            if qty + delta < 0:
                return False
            updated = (
                self.supabase.table("listings")
                .update({"quantity": qty + delta})
                .eq("id", listing_id)
                .eq("quantity", qty)
                .execute()
            )
            if updated.data:
                return True
        raise RuntimeError("库存更新冲突次数过多")

    def _release_stock(self, listing_id: str) -> None:
        """预订写入失败时回补一份库存；回补本身失败只记日志，不覆盖原始异常。"""
        try:
            self._change_stock(listing_id, +1)
        except Exception as exc:
            logger.error("回补库存失败 listing_id=%s: %s", listing_id, exc)

    def _insert_reservation(self, listing_id: str, telegram_id: int) -> str:
        """写入 reservations 并返回 claim_code；仅在 claim_code 撞唯一约束时换码重试（概率极低）。"""
        for _ in range(3):
            claim_code = "".join(
                secrets.choice(CLAIM_CODE_ALPHABET) for _ in range(CLAIM_CODE_LENGTH)
            )
            try:
                self.supabase.table("reservations").insert(
                    {
                        "listing_id": listing_id,
                        "student_telegram_id": telegram_id,
                        "claim_code": claim_code,
                        # status / created_at 由数据库默认值填充
                    }
                ).execute()
                return claim_code
            except APIError as exc:
                if exc.code != UNIQUE_VIOLATION:
                    raise  # 网络、外键等其他错误重试也没用，直接抛出
        raise RuntimeError("无法生成唯一 claim_code")

    # ------------------------------------------------------------------
    # 我的预订
    # ------------------------------------------------------------------

    async def my_reservations(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """查询当前用户 status = 'pending' 的预订（join listings → stalls）。

        已核销（completed）的不显示；SAME_DAY_ONLY 打开时，之前日子没领的预订已过期，也不显示
        （demo 摊位的预订除外）。
        """
        query = update.callback_query
        await query.answer()
        user = query.from_user

        try:
            res = (
                self.supabase.table("reservations")
                .select(
                    "claim_code, status, created_at, listings(title, pickup_time, created_at, "
                    "stalls(stall_name, canteen_location, vendor_telegram_id))"
                )
                .eq("student_telegram_id", user.id)
                .eq("status", "pending")
                .order("created_at", desc=True)
                .execute()
            )
            rows = [
                r for r in res.data or []
                if listed_today_or_demo(r["created_at"], (r.get("listings") or {}).get("stalls"))
            ]
        except Exception as exc:
            logger.error("查询预订失败 telegram_id=%s: %s", user.id, exc)
            await self._render_menu(query, "⚠️ Something went wrong. Please try again.")
            return

        if not rows:
            await self._render_menu(query, "📋 You have no pending reservations.\n\nGo rescue a meal! 🍱")
            return

        lines = ["📋 Your Pending Reservations\n"]
        for i, r in enumerate(rows, 1):
            listing = r.get("listings") or {}
            stall = listing.get("stalls") or {}
            deadline = eat_by(listing.get("created_at") or r["created_at"], parse_ts(r["created_at"]), stall)
            lines.append(
                f"{i}. {listing.get('title', 'N/A')}\n"
                f"   🎟️ {r['claim_code']} | 📍 {stall.get('canteen_location', 'N/A')}\n"
                f"   🕖 {listing.get('pickup_time') or 'TBA'} | Status: {r['status']}\n"
                f"   ⏰ Eat by {fmt_sg_time(deadline)}\n"
            )

        await query.edit_message_text(
            "\n".join(lines) + "\nShow your claim code at the stall to collect your meal.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Back to Menu", callback_data="back_menu")]]
            ),
        )


# ----------------------------------------------------------------------
# Handler 装配与启动
# ----------------------------------------------------------------------

def main() -> None:
    """构建学生端 Application 并注册全部 handler。"""
    bot = StudentBot()

    application = Application.builder().token(
        get_bot_token("TELEGRAM_STUDENT_BOT_TOKEN")
    ).build()
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CallbackQueryHandler(bot.browse, pattern=r"^browse(:\d+)?$"))
    application.add_handler(CallbackQueryHandler(bot.my_reservations, pattern="^my_reservations$"))
    application.add_handler(CallbackQueryHandler(bot.reserve, pattern=r"^reserve:"))
    application.add_handler(CallbackQueryHandler(bot.back_to_menu, pattern="^back_menu$"))
    schedule_db_keepalive(application, bot.supabase)

    logger.info("Student Bot 启动中...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


# ======================================================================
# 生产级事务建议（可选升级）
# ----------------------------------------------------------------------
# 当前扣库存用 CAS（WHERE quantity = 刚读到的值，冲突时重试），不会丢失扣减、
# 也不会超卖；但「扣库存」与「写预订」是两次独立请求，写预订失败时靠回补库存
# 补偿。如需在一个数据库事务内一步完成两者，可在 Supabase SQL Editor 创建
# 以下 RPC，然后 bot 端改用：
#
#     self.supabase.rpc("reserve_listing", {
#         "p_listing_id": listing_id,
#         "p_telegram_id": user.id,
#         "p_claim_code": claim_code,
#     }).execute()
#
#   create or replace function public.reserve_listing(
#       p_listing_id uuid,
#       p_telegram_id bigint,
#       p_claim_code text
#   ) returns boolean
#   language plpgsql
#   as $$
#   begin
#       update public.listings
#          set quantity = quantity - 1
#        where id = p_listing_id and quantity > 0;
#       if not found then
#           return false;   -- 已售罄
#       end if;
#       insert into public.reservations (listing_id, student_telegram_id, claim_code)
#       values (p_listing_id, p_telegram_id, p_claim_code);
#       return true;
#   end;
#   $$;
# ======================================================================
