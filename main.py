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
    [실현 손익]과 [평가 손익]을 동시에 표시
    """
    # 1. 포지션 정보 가져오기 (여기서 realized_pnl도 같이 옴)
    positions_info = await binance_ws.get_positions_with_pnl()

    if not positions_info:
        await update.message.reply_text("🤷‍♂️ 현재 보유 중인 포지션이 없습니다.")
        return

    msg_lines = ["📊 *현재 포지션 현황*"]

    # 합계 변수 분리
    total_unrealized_pnl = Decimal("0")
    total_realized_pnl = Decimal("0")

    for p in positions_info:
        symbol = p["symbol"]
        side = p["side"]
        amt = p["amount"]
        entry_price = p["entry_price"]
        current_price = p["current_price"]

        # 데이터 추출
        pnl = p["pnl"]  # 평가 손익 (Unrealized)
        realized_pnl = p.get(
            "realized_pnl", Decimal("0")
        )  # 실현 손익 (Realized) - 새로 추가된 부분
        roe = p["roe"]

        # 합계 누적
        total_unrealized_pnl += pnl
        total_realized_pnl += realized_pnl

        # 이모지 결정
        u_icon = "🔥" if pnl > 0 else "💧"
        r_icon = "💰" if realized_pnl > 0 else "💸"

        msg_lines.append(f"\n*{symbol}* {side}")
        msg_lines.append(f"• 수량: `{amt:,}`")
        msg_lines.append(f"• 평단: `{f(entry_price)}`")
        msg_lines.append(f"• 현재: `{f(current_price)}`")

        # 🔥 [핵심] 실현 손익이 있을 때만 한 줄 더 보여줌
        if realized_pnl != Decimal("0"):
            msg_lines.append(f"• 실현손익: {r_icon} `{realized_pnl:,.2f}` USDT (확정)")

        msg_lines.append(f"• 평가손익: {u_icon} `{pnl:,.2f}` USDT ({roe:+.2f}%)")

    # 하단 요약 (구분선 추가)
    msg_lines.append("\n──────────────")

    # 총 실현 손익이 있으면 표시
    if total_realized_pnl != Decimal("0"):
        total_r_icon = "💰" if total_realized_pnl > 0 else "💸"
        msg_lines.append(
            f"{total_r_icon} *총 실현 손익:* `{total_realized_pnl:,.2f}` USDT"
        )

    # 총 평가 손익 표시
    total_u_icon = "🔥" if total_unrealized_pnl >= 0 else "💧"
    msg_lines.append(
        f"{total_u_icon} *총 평가 손익:* `{total_unrealized_pnl:,.2f}` USDT"
    )

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
