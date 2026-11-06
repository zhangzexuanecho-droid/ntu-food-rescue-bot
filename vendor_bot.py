"""
商家端 Bot（NTU Food Rescue）：五步发布剩余餐食；学生取餐时核销 claim code。

运行：
    python vendor_bot.py

对应 .env 变量：
    TELEGRAM_VENDOR_BOT_TOKEN   商家 Bot 的 token（用 BotFather 单独创建一个）
    SUPABASE_URL / SUPABASE_KEY 与学生端共用
"""

import logging
import math
from warnings import filterwarnings

from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

from common import (
    eat_by,
    fmt_eat_by,
    get_admin_ids,
    get_bot_token,
    get_supabase,
    listed_today_or_demo,
    parse_ts,
    schedule_db_keepalive,
)

logger = logging.getLogger(__name__)

# 发布流程混用 MessageHandler 与 CallbackQueryHandler，只能按 chat/user 跟踪对话状态
# （per_message=False），这正是预期行为；屏蔽 PTB 针对这种组合的提示性警告。
filterwarnings("ignore", message=r"If 'per_message=False'", category=PTBUserWarning)

# 对话状态：发布流程的五步 + 核销时等待输入 claim code
(SELECT_STALL, MEAL_TITLE, PRICES, QUANTITY, PICKUP_WINDOW, VERIFY_CODE) = range(6)

# 与学生端生成规则一致：6 位大写字母 + 数字
CLAIM_CODE_LENGTH = 6

# 输入上限：标题、取餐时间太长会撑大学生端列表；价格、数量过大会超出数据库
# numeric(10,2) / integer 的范围，要到最后一步保存时才报错
MAX_TITLE_LEN = 60
MAX_PICKUP_LEN = 40
MAX_PRICE = 1000
MAX_QUANTITY = 999

# 商家端主菜单
VENDOR_MENU = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("➕ Add Surplus Meal", callback_data="add_meal")],
        [InlineKeyboardButton("🎟️ Verify Claim Code", callback_data="verify")],
    ]
)


class VendorBot:
    """商家端全部业务逻辑。"""

    def __init__(self) -> None:
        self.supabase = get_supabase()
        # 核销后通知学生要用学生端 bot 发：商家端 bot 不能主动给没和它聊过天的学生发消息
        self._student_bot: Bot | None = None

    async def _notify_student(self, telegram_id: int, text: str) -> None:
        """用学生端 bot 给学生发一条消息；失败（比如学生屏蔽了 bot）只记日志，不影响核销结果。"""
        try:
            if self._student_bot is None:
                bot = Bot(get_bot_token("TELEGRAM_STUDENT_BOT_TOKEN"))
                await bot.initialize()
                self._student_bot = bot
            await self._student_bot.send_message(telegram_id, text)
        except Exception as exc:
            logger.warning("通知学生失败 telegram_id=%s: %s", telegram_id, exc)

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
        """流程入口失败时按入口类型（按钮/命令）选择回复方式；按钮已在入口处 answer。"""
        query = update.callback_query
        if query is not None:
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
        if update.callback_query is not None:
            await update.callback_query.answer()  # 回调必须 answer，否则客户端按钮会一直转圈
        context.user_data.clear()  # 允许中途重新进入流程，先清掉上次没走完的数据
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

        # 名下多个摊位：列出供选择，并记下可选范围供 select_stall 校验
        # （callback_data 可被恶意客户端伪造，不能直接信任按钮里的 stall id）
        context.user_data["stalls"] = {s["id"]: s for s in stalls}
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
        """校验并记录所选摊位，询问餐品名称。"""
        query = update.callback_query
        await query.answer()
        stall = context.user_data.get("stalls", {}).get(query.data.split(":", 1)[1])
        if stall is None:
            context.user_data.clear()
            await query.edit_message_text(
                "🔒 You can't list meals for this stall.", reply_markup=VENDOR_MENU
            )
            return ConversationHandler.END

        context.user_data["stall_id"] = stall["id"]
        await query.edit_message_text(
            f"✅ Stall: {stall['stall_name']} ({stall['canteen_location']})\n\n"
            "🍽️ Step 2/5: What's the meal title?"
        )
        return MEAL_TITLE

    async def meal_title(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 2/5：记录餐品名称。"""
        title = update.message.text.strip()
        if len(title) > MAX_TITLE_LEN:
            await update.message.reply_text(
                f"❌ Title is too long (max {MAX_TITLE_LEN} characters). Please try again."
            )
            return MEAL_TITLE

        context.user_data["title"] = title
        await update.message.reply_text(
            "💰 Step 3/5: Enter the original price and discount price, "
            "separated by a comma.\ne.g. `6.50, 3.00`",
            parse_mode=ParseMode.MARKDOWN,
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
            # float() 也接受 "nan" / "inf"，需显式排除
            if not (math.isfinite(original) and math.isfinite(discount)):
                raise ValueError
            if original <= 0 or original > MAX_PRICE or discount < 0 or discount > original:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid format. Please enter like `6.50, 3.00` "
                f"(discount must not exceed original price; max ${MAX_PRICE}).",
                parse_mode=ParseMode.MARKDOWN,
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
            if not 0 < qty <= MAX_QUANTITY:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"❌ Please enter a whole number from 1 to {MAX_QUANTITY}, e.g. `5`.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return QUANTITY

        context.user_data["quantity"] = qty
        await update.message.reply_text(
            "🕖 Step 5/5: What's the pickup window?\ne.g. `7:30 PM - 8:30 PM`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return PICKUP_WINDOW

    async def pickup_window(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Step 5/5：保存 listing 到 Supabase 并通知 vendor。"""
        pickup = update.message.text.strip()
        if len(pickup) > MAX_PICKUP_LEN:
            await update.message.reply_text(
                f"❌ Too long (max {MAX_PICKUP_LEN} characters). e.g. `7:30 PM - 8:30 PM`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return PICKUP_WINDOW

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

    # ------------------------------------------------------------------
    # 核销 claim code（学生到摊位出示核销码，vendor 输入后标记为已领取）
    # ------------------------------------------------------------------

    async def start_verify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """核销入口：按钮或 /verify。/verify ABC123 带参数时直接核销，否则提示输入核销码。"""
        if update.callback_query is not None:
            await update.callback_query.answer()
        context.user_data.clear()
        vendor_id = update.effective_user.id

        if vendor_id not in get_admin_ids():
            try:
                linked = (
                    self.supabase.table("stalls")
                    .select("id")
                    .eq("vendor_telegram_id", vendor_id)
                    .limit(1)
                    .execute()
                ).data
            except Exception as exc:
                logger.error("查询 stalls 失败 vendor_id=%s: %s", vendor_id, exc)
                await self._fail(update, "⚠️ Something went wrong. Please try again.")
                return ConversationHandler.END
            if not linked:
                await self._fail(
                    update,
                    "🔒 You are not linked to any stall yet.\n\n"
                    f"Your vendor ID is {vendor_id} — send it to the admin "
                    "so they can link your stall to your account.",
                )
                return ConversationHandler.END

        if context.args:
            return await self._handle_code(update, " ".join(context.args))

        await self._reply_step(
            update,
            "🎟️ Enter the student's 6-character claim code:\n(/cancel to exit)",
        )
        return VERIFY_CODE

    async def verify_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """VERIFY_CODE 状态下收到的文本即核销码。"""
        return await self._handle_code(update, update.message.text)

    async def _handle_code(self, update: Update, raw: str) -> int:
        """校验格式 → 核销 → 回复结果。码不存在或格式不对时停留在 VERIFY_CODE 让 vendor 重输。"""
        message = update.effective_message
        code = "".join(raw.split()).upper()
        if len(code) != CLAIM_CODE_LENGTH or not (code.isascii() and code.isalnum()):
            await message.reply_text(
                f"❌ A claim code has {CLAIM_CODE_LENGTH} letters/digits, e.g. `K7Q2XM`. "
                "Please try again, or /cancel.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return VERIFY_CODE

        vendor_id = update.effective_user.id
        try:
            result, reservation = self._verify_claim(
                code, vendor_id, vendor_id in get_admin_ids()
            )
        except Exception as exc:
            logger.error("核销失败 code=%s: %s", code, exc)
            await message.reply_text(
                "⚠️ Something went wrong. Please try again.", reply_markup=VENDOR_MENU
            )
            return ConversationHandler.END

        if result == "not_found":
            await message.reply_text(
                f"❌ Code {code} was not found for your stall.\n\n"
                "Please check the code and try again, or /cancel."
            )
            return VERIFY_CODE

        listing = reservation.get("listings") or {}
        stall = listing.get("stalls") or {}
        deadline = fmt_eat_by(eat_by(
            listing.get("created_at") or reservation["created_at"],
            parse_ts(reservation["created_at"]),
            stall,
        ))
        replies = {
            "ok": (
                f"✅ Code {code} verified!\n\n"
                f"Please hand over: 🍽️ {listing.get('title', 'N/A')}\n"
                f"🏪 {stall.get('stall_name', 'N/A')}\n\n"
                f"⏰ Remind the student to eat it by {deadline}.\n"
                "Marked as collected."
            ),
            "already_completed": (
                f"⚠️ Code {code} has already been used — this meal was collected earlier."
            ),
            "cancelled": f"⚠️ The reservation for code {code} was cancelled.",
            "expired": (
                f"⌛ Code {code} is from a previous day and has expired.\n"
                "Only meals reserved today can be collected."
            ),
        }
        await message.reply_text(replies[result], reply_markup=VENDOR_MENU)
        if result == "ok":
            await self._notify_student(
                reservation["student_telegram_id"],
                f"✅ Meal collected: {listing.get('title', 'N/A')} from {stall.get('stall_name', 'N/A')}\n\n"
                f"⏰ Please eat it by {deadline} for food safety. Enjoy! 🌏",
            )
        return ConversationHandler.END

    def _verify_claim(self, code: str, vendor_id: int, is_admin: bool) -> tuple[str, dict | None]:
        """核销一个 claim code，返回 (结果, 预订记录)。

        结果取值：ok / not_found / already_completed / cancelled / expired。
        普通 vendor 只能核销自己摊位的码；别的摊位的码一律当作 not_found，不泄露它是否存在。
        """
        res = (
            self.supabase.table("reservations")
            .select(
                "id, status, created_at, student_telegram_id, "
                "listings(title, created_at, stalls(stall_name, vendor_telegram_id))"
            )
            .eq("claim_code", code)
            .execute()
        )
        if not res.data:
            return "not_found", None
        reservation = res.data[0]
        stall = (reservation.get("listings") or {}).get("stalls") or {}
        if not is_admin and stall.get("vendor_telegram_id") != vendor_id:
            return "not_found", None
        if reservation["status"] == "completed":
            return "already_completed", reservation
        if reservation["status"] != "pending":
            return "cancelled", reservation

        # SAME_DAY_ONLY 打开时，之前日子的预订已过期（demo 摊位的除外）
        if not listed_today_or_demo(reservation["created_at"], stall):
            return "expired", reservation

        # 条件更新：WHERE 带上 status = 'pending'，同一个码被重复提交时只有第一次生效，不会被领两次
        updated = (
            self.supabase.table("reservations")
            .update({"status": "completed"})
            .eq("id", reservation["id"])
            .eq("status", "pending")
            .execute()
        )
        if updated.data:
            return "ok", reservation
        return "already_completed", reservation

    async def cancel_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """/cancel 命令退出当前流程（发布或核销）。"""
        context.user_data.clear()
        await update.message.reply_text("❌ Cancelled.", reply_markup=VENDOR_MENU)
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

    # 发布与核销放在同一个 ConversationHandler 里：任一流程中途点另一个入口时直接切换过去，
    # 否则旧流程的状态还在，vendor 输入的核销码会被当成餐品名之类处理
    vendor_conv = ConversationHandler(
        # 由主菜单按钮触发；同时保留 /add、/verify 命令作为快捷入口
        entry_points=[
            CallbackQueryHandler(bot.start_add_meal, pattern="^add_meal$"),
            CommandHandler("add", bot.start_add_meal),
            CallbackQueryHandler(bot.start_verify, pattern="^verify$"),
            CommandHandler("verify", bot.start_verify),
        ],
        states={
            SELECT_STALL: [CallbackQueryHandler(bot.select_stall, pattern=r"^stall:")],
            MEAL_TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.meal_title)],
            PRICES: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.prices)],
            QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.quantity)],
            PICKUP_WINDOW: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.pickup_window)],
            VERIFY_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, bot.verify_code)],
        },
        fallbacks=[
            CommandHandler("cancel", bot.cancel_add),
            CallbackQueryHandler(bot.cancel_add_cb, pattern="^cancel_meal$"),
        ],
        # 流程中途再点「Add Surplus Meal」或发 /add 时重新开始，而不是卡在旧状态里没反应
        allow_reentry=True,
    )

    application = Application.builder().token(
        get_bot_token("TELEGRAM_VENDOR_BOT_TOKEN")
    ).build()
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(vendor_conv)
    schedule_db_keepalive(application, bot.supabase)

    logger.info("Vendor Bot 启动中...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
