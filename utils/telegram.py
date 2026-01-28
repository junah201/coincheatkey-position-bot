from telegram import Bot

from utils import get_required_env

TELEGRAM_TOKEN = get_required_env("TELEGRAM_TOKEN")
CHAT_ID = get_required_env("TELEGRAM_CHAT_ID")

bot = Bot(token=TELEGRAM_TOKEN)


async def send_telegram_message(text: str):
    """실제 텔레그램 전송 함수"""
    global bot
    try:
        await bot.send_message(chat_id=CHAT_ID, text=text)
        print(f"[전송 완료] {text}")
    except Exception as e:
        print(f"[전송 실패] {e}")
