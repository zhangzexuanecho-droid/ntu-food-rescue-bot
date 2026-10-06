"""
两个 Bot 共享的基础设施：.env 加载、Supabase 客户端工厂、日志配置。

学生端运行：python student_bot.py
商家端运行：python vendor_bot.py
"""

import logging
import os

from dotenv import load_dotenv
from supabase import Client, create_client

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# httpx 在 INFO 级别会打印每个请求的完整 URL，而 Bot API 的 URL 里含有 bot token；
# 调到 WARNING，避免 token 出现在终端和日志文件里。
logging.getLogger("httpx").setLevel(logging.WARNING)


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
