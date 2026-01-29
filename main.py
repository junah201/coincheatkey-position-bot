import asyncio
from decimal import Decimal

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from exchanges.binance_ws import BinanceWebSocket
from utils import get_required_env

TOKEN = get_required_env("TELEGRAM_TOKEN")

binance_ws = BinanceWebSocket()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("봇이 실행 중입니다! /pos 을 입력해보세요.")


async def position_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pos  명령어 처리
    메모리에 있는 active_positions를 즉시 읽어서 반환
    """
    positions = binance_ws.active_positions

    if not positions:
        await update.message.reply_text("🤷‍♂️ 현재 보유 중인 포지션이 없습니다.")
        return

    msg_lines = ["📊 *현재 포지션 현황*"]

    for symbol, data in positions.items():
        amt = data["amt"] * BinanceWebSocket.SIMULATION_MULTIPLIER
        price = data["price"]

        # 수량이 0이면(청산됨) 건너뛰기
        if amt == Decimal("0"):
            continue

        side = "🟢 롱" if amt > 0 else "🔴 숏"
        msg_lines.append(f"\n*{symbol}* {side}")
        msg_lines.append(f"• 수량: `{amt}`")
        msg_lines.append(f"• 평단: `{price:,.4f}`")

    if len(msg_lines) == 1:
        await update.message.reply_text("🤷‍♂️ 현재 보유 중인 포지션이 없습니다.")
        return

    await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")


async def post_init(application):
    """
    텔레그램 봇이 켜진 직후 실행되는 함수.
    여기서 바이낸스 웹소켓을 백그라운드 태스크로 실행합니다.
    """
    print("🚀 텔레그램 봇 시작됨 & 바이낸스 소켓 연결 시도...")

    asyncio.create_task(binance_ws.start())


def main():
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pos", position_command))

    print("봇 폴링 시작...")
    application.run_polling()


if __name__ == "__main__":
    main()
