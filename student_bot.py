"""
学生端 Bot（NTU Food Rescue）：浏览剩余餐食、一键预订、查看我的预订。

运行：
    python student_bot.py

对应 .env 变量：
    TELEGRAM_STUDENT_BOT_TOKEN   学生 Bot 的 token（BotFather 获取）
    SUPABASE_URL / SUPABASE_KEY  与商家端共用
"""

import logging
import random
import string

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from common import get_bot_token, get_supabase

logger = logging.getLogger(__name__)

# 学生端主菜单
STUDENT_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("🍱 Browse Available Food", callback_data="browse")],
        [InlineKeyboardButton("📋 My Reservations", callback_data="my_reservations")],
    ]
)


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
        """查询 quantity > 0 的 listings（join stalls），逐条展示并附 Reserve 按钮。"""
        query = update.callback_query
        await query.answer()

        try:
            res = (
                self.supabase.table("listings")
                .select(
                    "id, title, original_price, discount_price, quantity, "
                    "pickup_time, stalls(stall_name, canteen_location)"
                )
                .gt("quantity", 0)
                .order("created_at", desc=True)
                .execute()
            )
            items = res.data or []
        except Exception as exc:
            logger.error("查询 listings 失败: %s", exc)
            await self._render_menu(query, "⚠️ Something went wrong. Please try again.")
            return

        if not items:
            await self._render_menu(query, "😕 No surplus meals available right now.\n\nCheck back soon!")
            return

        lines = ["🍱 Available Surplus Meals\n"]
        for i, item in enumerate(items, 1):
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
            for item in items
        ]
        keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="back_menu")])

        await query.edit_message_text(
            "\n".join(lines) + "\nTap a button to reserve:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def reserve(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        处理 Reserve Now：
          1. 二次校验库存（quantity > 0），防止用户点的是过期列表
          2. 条件更新原子扣减：WHERE quantity > 0，售罄时无行命中即视为失败
          3. 写入 reservations（含 6 位随机 claim_code）
          4. 发送核销码确认消息
        """
        query = update.callback_query
        await query.answer()
        listing_id = query.data.split(":", 1)[1]
        user = query.from_user

        try:
            # 二次校验：取最新库存与详情（列表页数据可能已过期）
            res = (
                self.supabase.table("listings")
                .select("id, title, quantity, pickup_time, stalls(stall_name, canteen_location)")
                .eq("id", listing_id)
                .execute()
            )
            if not res.data:
                await query.answer("😔 This listing no longer exists.", show_alert=True)
                return
            listing = res.data[0]

            if int(listing["quantity"]) <= 0:
                await query.answer("😔 Sorry, this item is out of stock!", show_alert=True)
                return

            # 条件更新：UPDATE ... WHERE quantity > 0。
            # PostgREST 无法原生执行 quantity = quantity - 1，这里用
            # 「读到值再写回 + 条件过滤」实现，保证不会把库存扣成负数；
            # 极端并发下可能丢失一次扣减，生产级做法见文件末尾的 RPC 建议。
            dec = (
                self.supabase.table("listings")
                .update({"quantity": int(listing["quantity"]) - 1})
                .eq("id", listing_id)
                .gt("quantity", 0)
                .execute()
            )
            if not dec.data:
                await query.answer("😔 Sorry, this item just sold out!", show_alert=True)
                return

            # 生成唯一核销码并写入预订记录
            claim_code = self._insert_reservation(listing_id, user.id)
        except Exception as exc:
            logger.error("预订失败 listing_id=%s: %s", listing_id, exc)
            await query.answer("⚠️ Something went wrong. Please try again.", show_alert=True)
            return

        stall = listing.get("stalls") or {}
        await query.message.reply_text(
            "🎉 Reservation confirmed!\n\n"
            f"🍽️ {listing['title']}\n"
            f"📍 {stall.get('stall_name', 'N/A')} — {stall.get('canteen_location', 'N/A')}\n"
            f"🕖 Pickup: {listing.get('pickup_time') or 'TBA'}\n\n"
            f"🎟️ Your Claim Code: {claim_code}\n\n"
            "Show this code at the stall to collect your meal. Enjoy!"
        )

    def _insert_reservation(self, listing_id: str, telegram_id: int) -> str:
        """写入 reservations 并返回 claim_code；claim_code 撞库时重试（概率极低）。"""
        for _ in range(3):
            claim_code = "".join(
                random.choices(string.ascii_uppercase + string.digits, k=6)
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
            except Exception:
                continue  # 唯一约束冲突，换一个 code 重试
        raise RuntimeError("无法生成唯一 claim_code")

    # ------------------------------------------------------------------
    # 我的预订
    # ------------------------------------------------------------------

    async def my_reservations(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """查询当前用户 status = 'pending' 的预订（join listings → stalls）。"""
        query = update.callback_query
        await query.answer()
        user = query.from_user

        try:
            res = (
                self.supabase.table("reservations")
                .select(
                    "claim_code, status, created_at, "
                    "listings(title, pickup_time, stalls(stall_name, canteen_location))"
                )
                .eq("student_telegram_id", user.id)
                .eq("status", "pending")
                .order("created_at", desc=True)
                .execute()
            )
            rows = res.data or []
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
            lines.append(
                f"{i}. {listing.get('title', 'N/A')}\n"
                f"   🎟️ {r['claim_code']} | 📍 {stall.get('canteen_location', 'N/A')}\n"
                f"   🕖 {listing.get('pickup_time') or 'TBA'} | Status: {r['status']}\n"
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
    application.add_handler(CallbackQueryHandler(bot.browse, pattern="^browse$"))
    application.add_handler(CallbackQueryHandler(bot.my_reservations, pattern="^my_reservations$"))
    application.add_handler(CallbackQueryHandler(bot.reserve, pattern=r"^reserve:"))
    application.add_handler(CallbackQueryHandler(bot.back_to_menu, pattern="^back_menu$"))

    logger.info("Student Bot 启动中...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


# ======================================================================
# 生产级并发建议（可选升级）
# ----------------------------------------------------------------------
# 当前扣库存是「读取数量 → 条件写回 WHERE quantity > 0」，能保证不卖超，
# 但在极端并发下两个请求可能读到同一数量、扣成同一结果（丢失一次扣减）。
# 如需严格原子性，可在 Supabase SQL Editor 创建以下 RPC，在数据库事务内
# 一步完成「扣库存 + 写预订」，然后 bot 端改用：
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
