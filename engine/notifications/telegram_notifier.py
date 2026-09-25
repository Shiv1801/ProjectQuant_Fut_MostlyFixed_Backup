"""
PRD §7.4 gap — alerting on circuit breaker trips, mode auto-downgrades,
unusual model divergence. Telegram chosen as default channel; extend
with sms_notifier.py / push_notifier.py using the same interface if needed.
"""
import httpx
from loguru import logger

from config.settings import settings


async def send_telegram_alert(message: str):
    if settings.notify_channel != "telegram" or not settings.telegram_bot_token:
        logger.warning(f"Notification channel not configured, message dropped: {message}")
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": settings.telegram_chat_id, "text": message, "parse_mode": "Markdown"})


async def alert_circuit_breaker(reason: str, account_state: dict):
    await send_telegram_alert(f"🚨 *Circuit breaker tripped*: {reason}\nAccount: {account_state}")


async def alert_mode_downgrade(from_mode: str, to_mode: str, reason: str):
    await send_telegram_alert(f"⚠️ *Mode auto-downgrade*: {from_mode} → {to_mode}\nReason: {reason}")


async def alert_model_divergence(model_id: str, detail: dict):
    await send_telegram_alert(f"📉 *Model divergence flagged*: {model_id}\n{detail}")
