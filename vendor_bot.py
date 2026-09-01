"""
商家端 Bot（NTU Food Rescue）：五步发布剩余餐食。

运行：
    python vendor_bot.py

对应 .env 变量：
    TELEGRAM_VENDOR_BOT_TOKEN   商家 Bot 的 token（用 BotFather 单独创建一个）
    SUPABASE_URL / SUPABASE_KEY 与学生端共用
"""

import logging

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from common import get_admin_ids, get_bot_token, get_supabase

logger = logging.getLogger(__name__)

# 发布流程的五个对话状态
(SELECT_STALL, MEAL_TITLE, PRICES, QUANTITY, PICKUP_WINDOW) = range(5)

# 商家端主菜单
VENDOR_MENU = InlineKeyboardMarkup(
    [[InlineKeyboardButton("➕ Add Surplus Meal", callback_data="add_meal")]]
)


class VendorBot:
    """商家端全部业务逻辑。"""

    def __init__(self) -> None:
        self.supabase = get_supabase()

    # ------------------------------------------------------------------
    # /start 与通用辅助
    # ------------------------------------------------------------------

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """发送欢迎语与商家端主菜单（附商家 ID，用于绑定摊位）。"""
        await update.message.reply_text(
            "👋 Welcome, vendor!\n\n"
            "Publish your unsold meals here so students can rescue them 🍽️\n\n"
            f"🆔 Your vendor ID: {update.effective_user.id}\n"
            "Send this ID to the admin so they can link your stall.\n\n"
            "What would you like to do?",
            reply_markup=VENDOR_MENU,
        )

    async def _fail(self, update: Update, text: str) -> None:
        """流程入口失败时按入口类型（按钮/命令）选择回复方式。"""
        query = update.callback_query
        if query is not None:
            await query.answer()
            try:
                await query.edit_message_text(text, reply_markup=VENDOR_MENU)
            except Exception:
                await query.message.reply_text(text, reply_markup=VENDOR_MENU)
        else:
            await update.message.reply_text(text, reply_markup=VENDOR_MENU)

    async def _reply_step(
        self, update: Update, text: str, keyboard: InlineKeyboardMarkup | None = None
    ) -> None:
        """按入口类型（按钮/命令）发送向导步骤消息。"""
        query = update.callback_query
        if query is not None:
            await query.edit_message_text(text, reply_markup=keyboard)
        else:
            await update.message.reply_text(text, reply_markup=keyboard)

    # ------------------------------------------------------------------
    # 五步发布流程（ConversationHandler）
    # ------------------------------------------------------------------

    async def start_add_meal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 1/5：加载摊位列表。普通 vendor 只见自己名下的摊位；管理员可见全部。"""
        vendor_id = update.effective_user.id
        is_admin = vendor_id in get_admin_ids()
        try:
            q = self.supabase.table("stalls").select("id, stall_name, canteen_location")
            if not is_admin:
                q = q.eq("vendor_telegram_id", vendor_id)
            res = q.order("stall_name").execute()
            stalls = res.data or []
        except Exception as exc:
            logger.error("查询 stalls 失败 vendor_id=%s: %s", vendor_id, exc)
            await self._fail(update, "⚠️ Something went wrong. Please try again.")
            return ConversationHandler.END

        if not stalls:
            # 普通 vendor 尚未绑定任何摊位
            await self._fail(
                update,
                "🔒 You are not linked to any stall yet.\n\n"
                f"Your vendor ID is {vendor_id} — send it to the admin "
                "so they can link your stall to your account.",
            )
            return ConversationHandler.END

        # 普通 vendor 名下只有一个摊位：自动选中，直接进入 Step 2
        if not is_admin and len(stalls) == 1:
            stall = stalls[0]
            context.user_data["stall_id"] = stall["id"]
            await self._reply_step(
                update,
                f"✅ Stall: {stall['stall_name']} ({stall['canteen_location']})\n\n"
                "🍽️ Step 2/5: What's the meal title?",
            )
            return MEAL_TITLE

        # 名下多个摊位：列出供选择
        keyboard = [
            [
                InlineKeyboardButton(
                    f"{s['stall_name']} ({s['canteen_location']})",
                    callback_data=f"stall:{s['id']}",
                )
            ]
            for s in stalls
        ]
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_meal")])
        step1_text = (
            "🏪 Step 1/5: Which stall are you listing for? (👑 Admin)"
            if is_admin
            else "🏪 Step 1/5: Which of your stalls are you listing for?"
        )
        await self._reply_step(update, step1_text, InlineKeyboardMarkup(keyboard))
        return SELECT_STALL

    async def select_stall(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """记录所选摊位，询问餐品名称。"""
        query = update.callback_query
        await query.answer()
        context.user_data["stall_id"] = query.data.split(":", 1)[1]

        await query.edit_message_text("✅ Stall selected.\n\n🍽️ Step 2/5: What's the meal title?")
        return MEAL_TITLE

    async def meal_title(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 2/5：记录餐品名称。"""
        context.user_data["title"] = update.message.text.strip()
        await update.message.reply_text(
            "💰 Step 3/5: Enter the original price and discount price, "
            "separated by a comma.\ne.g. `6.50, 3.00`"
        )
        return PRICES

    async def prices(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 3/5：解析并校验「原价, 折后价」，非法输入则停留在本步重问。"""
        parts = update.message.text.replace("$", "").split(",")
        try:
            if len(parts) != 2:
                raise ValueError
            original = float(parts[0].strip())
            discount = float(parts[1].strip())
            if original <= 0 or discount < 0 or discount > original:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid format. Please enter like `6.50, 3.00` "
                "(discount must not exceed original price)."
            )
            return PRICES

        context.user_data["original_price"] = original
        context.user_data["discount_price"] = discount
        await update.message.reply_text("📦 Step 4/5: How many portions are available?")
        return QUANTITY

    async def quantity(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 4/5：解析数量（正整数）。"""
        try:
            qty = int(update.message.text.strip())
            if qty <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Please enter a positive whole number, e.g. `5`.")
            return QUANTITY

        context.user_data["quantity"] = qty
        await update.message.reply_text(
            "🕖 Step 5/5: What's the pickup window?\ne.g. `7:30 PM - 8:30 PM`"
        )
        return PICKUP_WINDOW

    async def pickup_window(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 5/5：保存 listing 到 Supabase 并通知 vendor。"""
        pickup = update.message.text.strip()
        data = {
            "stall_id": context.user_data["stall_id"],
            "title": context.user_data["title"],
            "original_price": context.user_data["original_price"],
            "discount_price": context.user_data["discount_price"],
            "quantity": context.user_data["quantity"],
            "pickup_time": pickup,
        }
        try:
            self.supabase.table("listings").insert(data).execute()

            # 回查摊位名用于确认消息
            stall_res = (
                self.supabase.table("stalls")
                .select("stall_name, canteen_location")
                .eq("id", data["stall_id"])
                .execute()
            )
            stall = (stall_res.data or [{}])[0]
        except Exception as exc:
            logger.error("保存 listing 失败: %s", exc)
            await update.message.reply_text(
                "⚠️ Failed to save the listing. Please try again.",
                reply_markup=VENDOR_MENU,
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "✅ Listing published!\n\n"
            f"🍽️ {data['title']}\n"
            f"🏪 {stall.get('stall_name', 'N/A')} — {stall.get('canteen_location', 'N/A')}\n"
            f"💰 ${data['original_price']:.2f} → ${data['discount_price']:.2f}\n"
            f"📦 {data['quantity']} portions\n"
            f"🕖 {data['pickup_time']}\n\n"
            "Students can now find it under Browse Available Food.",
            reply_markup=VENDOR_MENU,
        )
        context.user_data.clear()
        return ConversationHandler.END

    async def cancel_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """/cancel 命令退出发布流程。"""
        context.user_data.clear()
        await update.message.reply_text("❌ Listing cancelled.", reply_markup=VENDOR_MENU)
        return ConversationHandler.END

    async def cancel_add_cb(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """摊位选择页的 Cancel 按钮退出发布流程。"""
        query = update.callback_query
        await query.answer()
        context.user_data.clear()
        try:
            await query.edit_message_text("❌ Listing cancelled.", reply_markup=VENDOR_MENU)
        except Exception:
            await query.message.reply_text("❌ Listing cancelled.", reply_markup=VENDOR_MENU)
        return ConversationHandler.END


# ----------------------------------------------------------------------
# Handler 装配与启动
# ----------------------------------------------------------------------

def main() -> None:
    """构建商家端 Application 并注册全部 handler。"""
    bot = VendorBot()

    vendor_conv = ConversationHandler(
        # 由主菜单按钮触发；同时保留 /add 命令作为快捷入口
        entry_points=[
            CallbackQueryHandler(bot.start_add_meal, pattern="^add_meal$"),
            CommandHandler("add", bot.start_add_meal),
        ],
        states={
            SELECT_STALL: [CallbackQueryHandler(bot.select_stall, pattern=r"^stall:")],
            MEAL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.meal_title)],
            PRICES: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.prices)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.quantity)],
            PICKUP_WINDOW: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.pickup_window)],
        },
        fallbacks=[
            CommandHandler("cancel", bot.cancel_add),
            CallbackQueryHandler(bot.cancel_add_cb, pattern="^cancel_meal$"),
        ],
    )

    application = Application.builder().token(
        get_bot_token("TELEGRAM_VENDOR_BOT_TOKEN")
    ).build()
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(vendor_conv)

    logger.info("Vendor Bot 启动中...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
