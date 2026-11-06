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

# 「今天」按新加坡时间算：只有当天发布的餐品可以浏览、预订、核销（食品安全：只卖当天做的食物）
SG_TZ = ZoneInfo("Asia/Singapore")


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
