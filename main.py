import asyncio
from decimal import Decimal

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from exchanges.binance_ws import BinanceWebSocket
from utils import get_required_env
from utils.string import f

TOKEN = get_required_env("TELEGRAM_TOKEN")
CHAT_ID = get_required_env("TELEGRAM_CHAT_ID")

binance_ws = BinanceWebSocket()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("봇이 실행 중입니다! /pos 을 입력해보세요.")


async def position_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /pos 명령어 처리
    [실현 손익], [평가 손익], [레버리지], [시드 비중]을 모두 표시
    """
    current_chat_id = update.effective_chat.id
    current_topic_id = update.effective_message.message_thread_id
    if str(current_chat_id) != CHAT_ID or str(current_topic_id) != '355034':
        # 권한이 없는 곳에서 명령어를 치면 무시 (또는 안내 메시지 전송)
        # 봇 스팸 방지를 위해 아무 대답도 하지 않고 return 하는 것을 추천합니다.
        return

    # 1. 포지션 정보 가져오기 (레버리지, 비중 포함)
    positions_info = await binance_ws.get_positions_with_pnl()

    if not positions_info:
        await update.message.reply_text("🤷‍♂️ 현재 보유 중인 포지션이 없습니다.")
        return

    msg_lines = ["📊 *현재 포지션 현황*"]

    # 합계 변수 분리
    total_unrealized_pnl = Decimal("0")
    total_realized_pnl = Decimal("0")
    total_seed_usage = Decimal("0")  # [추가] 총 시드 사용 비중

    for p in positions_info:
        symbol = p["symbol"]
        side = p["side"]
        amt = p["amount"]
        entry_price = p["entry_price"]
        current_price = p["current_price"]

        # 데이터 추출
        pnl = p["pnl"]  # 평가 손익 (Unrealized)
        realized_pnl = p.get("realized_pnl", Decimal("0"))  # 실현 손익 (Realized)

        # [추가] 레버리지 및 시드 비중 추출
        leverage = p.get("leverage", Decimal("1"))
        seed_usage_percent = p.get("seed_usage_percent", Decimal("0"))

        # 합계 누적
        total_unrealized_pnl += pnl
        total_realized_pnl += realized_pnl
        total_seed_usage += seed_usage_percent  # 총 비중 누적

        # 이모지 결정
        u_icon = "🔥" if pnl > 0 else "💧"
        r_icon = "💰" if realized_pnl > 0 else "💸"

        msg_lines.append(f"\n*{symbol} ({leverage}x)* {side}")
        msg_lines.append(f"• 수량: `{f(amt)}`")
        msg_lines.append(
            f"• 비중: `{f(seed_usage_percent, '0.01')}%`"
        )  # [추가] 비중 표시
        msg_lines.append(f"• 평단: `{f(entry_price)}`")
        msg_lines.append(f"• 현재: `{f(current_price)}`")

        # 🔥 [핵심] 실현 손익이 있을 때만 한 줄 더 보여줌
        if realized_pnl != Decimal("0"):
            msg_lines.append(
                f"• 실현손익:{r_icon} `{f(realized_pnl, '0.001')}` USDT (확정)"
            )

        msg_lines.append(f"• 평가손익:{u_icon}`{f(pnl, '0.001')}` USDT")

    # 하단 요약 (구분선 추가)
    msg_lines.append("\n──────────────")

    # [추가] 총 시드 사용 비중 표시 (0% 초과일 때만)
    if total_seed_usage > Decimal("0"):
        msg_lines.append(f"⚖️ *총 시드비중:* `{f(total_seed_usage, '0.01')}%`")

    # 총 실현 손익이 있으면 표시
    if total_realized_pnl != Decimal("0"):
        total_r_icon = "💰" if total_realized_pnl > 0 else "💸"
        msg_lines.append(
            f"{total_r_icon} *총 실현손익:* `{f(total_realized_pnl, '0.001')}` USDT"
        )

    # 총 평가 손익 표시
    total_u_icon = "🔥" if total_unrealized_pnl >= 0 else "💧"
    msg_lines.append(
        f"{total_u_icon}*총 평가손익:* `{f(total_unrealized_pnl, '0.001')}` USDT"
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
