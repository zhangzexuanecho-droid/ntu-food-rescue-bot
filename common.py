"""
两个 Bot 共享的基础设施：.env 加载、Supabase 客户端工厂、日志配置、数据库保活。

学生端运行：python student_bot.py
商家端运行：python vendor_bot.py
"""

import logging
import os
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from supabase import Client, create_client
from telegram.ext import Application, ContextTypes

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx 在 INFO 级别会打印每个请求的完整 URL，而 Bot API 的 URL 里含有 bot token；
# 调到 WARNING，避免 token 出现在终端和日志文件里。
logging.getLogger("httpx").setLevel(logging.WARNING)
# JobQueue 底层的 APScheduler 每次执行定时任务都会打两行 INFO，同样调到 WARNING
logging.getLogger("apscheduler").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Supabase 免费版一周内数据库活动太少就会自动暂停项目（官方说法：通常每天有几次请求就够）。
# 每个 bot 每 6 小时查一次库，没人使用 bot 时数据库也保持活跃。
KEEPALIVE_INTERVAL = timedelta(hours=6)

# 「今天」按新加坡时间算（每人每天限订一份、SAME_DAY_ONLY 都用它）
SG_TZ = ZoneInfo("Asia/Singapore")


# 食品安全提醒：SFA 要求熟食在煮好后 4 小时内吃完。bot 不知道真正的煮好时间，按「发布时刚煮好」估算，
# 再留 1 小时余量，所以提醒学生在发布后 3 小时内吃完。
EAT_WITHIN = timedelta(hours=3)


def is_demo_stall(stall: dict | None) -> bool:
    """没有绑定商家（vendor_telegram_id 为空）的摊位算 demo，它的餐品不受日期限制。"""
    return bool(stall) and stall.get("vendor_telegram_id") is None


def parse_ts(value: str) -> datetime:
    """解析数据库返回的 timestamptz 字符串（兼容结尾的 'Z'）。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def listed_today_or_demo(created_at: str, stall: dict | None) -> bool:
    """SAME_DAY_ONLY 打开时，只有今天发布的或 demo 摊位的才可用；开关关闭时一律可用。"""
    if not same_day_only() or is_demo_stall(stall):
        return True
    return parse_ts(created_at) >= parse_ts(today_start_utc())


def eat_by(listing_created_at: str, reserved_at: datetime, stall: dict | None) -> datetime:
    """学生应在什么时间之前吃完。

    真实餐品：发布时间 + EAT_WITHIN。demo 餐品的发布时间可能是很多天前，改用预订时间 + EAT_WITHIN，
    这样每次演示看到的都是一个合理的时间。
    """
    start = reserved_at if is_demo_stall(stall) else parse_ts(listing_created_at)
    return start + EAT_WITHIN


def fmt_sg_time(dt: datetime) -> str:
    """按新加坡时间显示，例如 '11:00 PM'；不是今天的再加上日期，例如 'Nov 5, 11:00 PM'。"""
    local = dt.astimezone(SG_TZ)
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == datetime.now(SG_TZ).date():
        return clock
    return f"{local.strftime('%b')} {local.day}, {clock}"


def same_day_only() -> bool:
    """SAME_DAY_ONLY=true 时，只有当天发布的餐品可以浏览、预订、核销（食品安全：只卖当天做的食物）。

    默认关闭：demo 阶段要能看到示例数据和之前发布的 listing；正式 pilot 时在 .env 里打开。
    """
    return os.getenv("SAME_DAY_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def today_start_utc() -> str:
    """新加坡时间今天 00:00 对应的 UTC 时间字符串，用作 created_at >= 的筛选条件。

    用 UTC + 'Z' 而不是 '+08:00'：'+' 放进 URL 查询参数可能被解析成空格，导致 PostgREST 报错。
    """
    sg_midnight = datetime.combine(datetime.now(SG_TZ).date(), time.min, tzinfo=SG_TZ)
    return sg_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_supabase() -> Client:
    """创建 Supabase 客户端（service_role key，绕过 RLS）。"""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not url or not key:
        raise RuntimeError("请在 .env 中配置 SUPABASE_URL 和 SUPABASE_KEY")
    return create_client(url, key)


def get_bot_token(env_var: str) -> str:
    """读取指定 bot 的 token，未配置时给出明确报错。"""
    token = os.getenv(env_var)
    if not token:
        raise RuntimeError(f"请在 .env 中配置 {env_var}")
    return token


def get_admin_ids() -> set[int]:
    """读取商家端管理员 Telegram ID 列表（.env 中逗号分隔）。

    管理员在 vendor bot 中可以看到并操作所有摊位；
    普通 vendor 只能看到自己名下的摊位。
    """
    raw = os.getenv("VENDOR_ADMIN_TELEGRAM_IDS", "")
    return {int(part) for part in raw.split(",") if part.strip()}


def schedule_db_keepalive(application: Application, supabase: Client) -> None:
    """注册定时保活任务：启动 1 分钟后开始，每隔 KEEPALIVE_INTERVAL 对数据库做一次轻量查询。"""
    if application.job_queue is None:
        raise RuntimeError("缺少 JobQueue 依赖，请运行 pip install -r requirements.txt")

    async def ping(context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            supabase.table("stalls").select("id").limit(1).execute()
            logger.info("Supabase 保活查询成功")
        except Exception as exc:
            logger.warning("Supabase 保活查询失败（项目可能已被暂停）: %s", exc)

    application.job_queue.run_repeating(
        ping, interval=KEEPALIVE_INTERVAL, first=60, name="db-keepalive"
    )
