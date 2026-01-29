import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from binance import AsyncClient, BinanceSocketManager

from exchanges.base import ExchangeWebSocket
from utils import get_required_env
from utils.telegram import send_telegram_message


class BinanceWebSocket(ExchangeWebSocket):
    SIMULATION_MULTIPLIER = Decimal("100")

    def __init__(self):
        super().__init__(
            api_key=get_required_env("BINANCE_API_KEY"),
            secret_key=get_required_env("BINANCE_SECRET_KEY"),
        )
        self.client = None
        self.bm = None

        self.active_positions = defaultdict(
            lambda: {"amt": Decimal("0"), "price": Decimal("0")}
        )

        self.msg_buffer = defaultdict(list)
        self.flush_tasks = {}

    async def _sync_initial_positions(self):
        """봇 시작 시 현재 포지션 상태 동기화"""
        try:
            account_info = await self.client.futures_account()
            for position in account_info["positions"]:
                symbol = position["symbol"]
                amt = Decimal(str(position["positionAmt"]))
                ep = Decimal(str(position["entryPrice"]))

                if amt != Decimal("0"):
                    self.active_positions[symbol] = {"amt": amt, "price": ep}
        except Exception:
            pass

    async def start(self):
        self.client = await AsyncClient.create(self.api_key, self.secret_key)
        await self._sync_initial_positions()

        self.bm = BinanceSocketManager(self.client)
        ts = self.bm.futures_user_socket()

        async with ts as tscm:
            while True:
                try:
                    res = await tscm.recv()
                    self._handle_socket_message(res)
                except Exception as e:
                    logging.error(f"소켓 에러: {e}")
                    await asyncio.sleep(1)

    def _handle_socket_message(self, msg):
        try:
            event_type = msg.get("e")

            # 1. 계좌 변동이 오면 내 지갑(메모리)을 즉시 갱신
            if event_type == "ACCOUNT_UPDATE":
                self._update_wallet(msg)

            # 2. 주문 체결이 오면 버퍼에 넣고 타이머 시작
            elif event_type == "ORDER_TRADE_UPDATE":
                self._buffer_order(msg)

        except Exception as e:
            logging.error(f"처리 중 에러: {e}")

    def _update_wallet(self, msg):
        """ACCOUNT_UPDATE 이벤트 처리: 지갑 정보 최신화"""
        data = msg.get("a", {})
        for p in data.get("P", []):
            symbol = p["s"]
            amt = Decimal(str(p["pa"]))  # 포지션 수량
            ep = Decimal(str(p["ep"]))  # 최신 평단가

            self.active_positions[symbol] = {"amt": amt, "price": ep}

    def _buffer_order(self, msg):
        """ORDER_TRADE_UPDATE 이벤트 처리"""
        order_data = msg.get("o", {})

        # 체결된 것만 처리 (FILLED, PARTIALLY_FILLED)
        if order_data.get("X") not in ["FILLED", "PARTIALLY_FILLED"]:
            return
        if order_data.get("x") != "TRADE":
            return

        symbol = order_data.get("s")
        self.msg_buffer[symbol].append(order_data)

        # 타이머가 없으면 시작
        if symbol not in self.flush_tasks:
            self.flush_tasks[symbol] = asyncio.create_task(self._flush_buffer(symbol))

    def aggregate_order_buffer(
        self, orders: list[dict[str, any]], multiplier: Decimal
    ) -> dict[str, any]:
        """
        주문 목록을 받아 집계된 데이터를 반환하는 순수 함수
        """
        total_qty = Decimal("0")  # 총 체결 수량
        total_val = Decimal("0")  # 총 체결 금액 (평단 계산용)
        total_pnl = Decimal("0")  # 총 실현 손익
        total_fee = Decimal("0")  # 총 수수료

        # 첫 주문의 Side를 기준 (보통 버퍼 내 주문은 같은 방향이라고 가정)
        side = orders[0]["S"]
        # 하나라도 Reduce(R) 속성이 있으면 청산으로 간주
        is_reduce = any(o.get("R", False) for o in orders)

        for o in orders:
            q = Decimal(str(o.get("l", "0"))) * multiplier
            p = Decimal(str(o.get("ap", "0")))  # 체결 가격
            rp = Decimal(str(o.get("rp", "0"))) * multiplier
            fee = Decimal(str(o.get("n", "0"))) * multiplier

            total_qty += q
            total_val += p * q
            total_pnl += rp
            total_fee += fee

        # 실행 평단가 계산 (0으로 나누기 방지)
        exec_avg_price = total_val / total_qty if total_qty > 0 else Decimal("0")

        return {
            "total_qty": total_qty,
            "total_pnl": total_pnl,
            "total_fee": total_fee,
            "exec_avg_price": exec_avg_price,
            "side": side,
            "is_reduce": is_reduce,
        }

    async def _flush_buffer(self, symbol):
        """0.5초 대기 후 데이터를 취합해서 알림 전송"""

        await asyncio.sleep(0.5)

        orders = self.msg_buffer.pop(symbol, [])
        if symbol in self.flush_tasks:
            del self.flush_tasks[symbol]

        if not orders:
            return

        agg_data = self.aggregate_order_buffer(orders, self.SIMULATION_MULTIPLIER)

        total_qty = agg_data["total_qty"]
        total_pnl = agg_data["total_pnl"]
        exec_avg_price = agg_data["exec_avg_price"]
        side = agg_data["side"]
        is_reduce = agg_data["is_reduce"]

        # 지갑 상태 조회
        wallet = self.active_positions.get(
            symbol, {"amt": Decimal("0"), "price": Decimal("0")}
        )
        final_ep = wallet["price"]
        final_amt = abs(wallet["amt"]) * self.SIMULATION_MULTIPLIER

        # 포지션 방향 및 색상 결정
        if is_reduce or total_pnl != 0:
            # 청산 주문의 경우: BUY면 숏을 청산한 것, SELL이면 롱을 청산한 것
            pos_side = "SHORT" if side == "BUY" else "LONG"
            side_color = "🔴" if pos_side == "SHORT" else "🟢"  # 숏은 빨강, 롱은 초록
        else:
            # 진입 주문의 경우: BUY면 롱 진입, SELL이면 숏 진입
            pos_side = "LONG" if side == "BUY" else "SHORT"
            side_color = "🟢" if pos_side == "LONG" else "🔴"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S KST")

        msg = ""

        # =========================================================
        # Case A: 청산 (익절 / 손절 / 본절)
        # =========================================================
        if total_pnl != Decimal("0") or is_reduce:
            # 손익 아이콘
            if total_pnl > 0:
                pnl_icon = "🎉"
            elif total_pnl < 0:
                pnl_icon = "💧"
            else:
                pnl_icon = "⚖️"

            # 전체 청산 vs 부분 청산
            if final_amt < Decimal("0.00001"):
                msg = (
                    f"❎ 전체 청산 ({pos_side})\n\n"
                    f"{side_color} 종목: {symbol}\n"
                    f"📦 수량: {total_qty:,}\n"
                    f"💲 가격: {exec_avg_price:,.8f}\n"
                    f"{pnl_icon} 손익: {total_pnl:,.2f} USDT\n"
                    f"🕒 시간: {now_str}"
                )
            else:
                msg = (
                    f"⚠️ 부분 청산 ({pos_side})\n\n"
                    f"{side_color} 종목: {symbol}\n"
                    f"📦 수량: {total_qty:,}\n"
                    f"📦 남은 수량: {final_amt:,}\n"
                    f"💲 가격: {exec_avg_price:,.8f}\n"
                    f"{pnl_icon} 손익: {total_pnl:,.2f} USDT\n"
                    f"🕒 시간: {now_str}"
                )

        # =========================================================
        # Case B: 진입 (신규 / 추가)
        # =========================================================
        else:
            prev_amt = final_amt - total_qty

            if prev_amt < Decimal("0.00001"):
                header_title = "신규 진입"
                msg = (
                    f"💥 {header_title} ({pos_side})\n\n"
                    f"{side_color} 종목: {symbol}\n"
                    f"📦 수량: {total_qty:,}\n"
                    f"💲 가격: {exec_avg_price:,}\n"
                    f"🕒 시간: {now_str}"
                )
            else:
                header_title = "추가 진입"
                msg = (
                    f"💥 {header_title} ({pos_side})\n\n"
                    f"{side_color} 종목: {symbol}\n"
                    f"📦 수량: {total_qty:,}\n"
                    f"💲 가격: {exec_avg_price:,}\n"
                    f"💲 최종 평단가: {final_ep:,} USDT\n"
                    f"📦 최종 수량: {final_amt:,}\n"
                    f"🕒 시간: {now_str}"
                )

        print(msg)
        print("-" * 30)
        asyncio.create_task(send_telegram_message(msg))

    async def stop(self):
        if self.client:
            await self.client.close_connection()
