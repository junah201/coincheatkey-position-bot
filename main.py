import asyncio
from decimal import Decimal

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from exchanges.binance_ws import BinanceWebSocket
from utils import get_required_env
from utils.string import f

TOKEN = get_required_env("TELEGRAM_TOKEN")

binance_ws = BinanceWebSocket()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("봇이 실행 중입니다! /pos 을 입력해보세요.")


async def position_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pos 명령어 처리
    현재가 조회 및 PnL 계산 포함
    """
    # 웹소켓 클래스에 새로 만든 메서드 호출
    positions_info = await binance_ws.get_positions_with_pnl()

    if not positions_info:
        await update.message.reply_text("🤷‍♂️ 현재 보유 중인 포지션이 없습니다.")
        return

    msg_lines = ["📊 *현재 포지션 현황*"]

    total_pnl = Decimal("0")

    for p in positions_info:
        symbol = p["symbol"]
        side = p["side"]
        amt = p["amount"]
        entry_price = p["entry_price"]
        current_price = p["current_price"]
        pnl = p["pnl"]
        roe = p["roe"]

        total_pnl += pnl

        # 이모지 결정 (수익이면 축하, 손실이면 눈물)
        pnl_icon = "🔥" if pnl > 0 else "💧"

        msg_lines.append(f"\n*{symbol}* {side}")
        msg_lines.append(f"• 수량: `{amt:,}`")  # 천단위 콤마
        msg_lines.append(f"• 평단: `{f(entry_price)}`")
        msg_lines.append(f"• 현재: `{f(current_price)}`")
        msg_lines.append(f"• 손익: {pnl_icon} `{pnl:,.2f}` USDT ({roe:+.2f}%)")

    # 총 손익 표시
    total_icon = "💰" if total_pnl >= 0 else "💸"
    msg_lines.append(f"\n{total_icon} *총 미실현 손익:* `{total_pnl:,.2f}` USDT")

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
