import asyncio
import json
import logging
from collections import defaultdict
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
            print("🔄 초기 포지션 정보 로딩 중...")
            account_info = await self.client.futures_account()
            for position in account_info["positions"]:
                symbol = position["symbol"]
                amt = Decimal(str(position["positionAmt"]))
                ep = Decimal(str(position["entryPrice"]))

                if amt != Decimal("0"):
                    self.active_positions[symbol] = {"amt": amt, "price": ep}
                    print(f"   ✅ 보유중: {symbol} (평단: {ep})")
            print("🆗 동기화 완료!")
        except Exception as e:
            logging.error(f"동기화 실패: {e}")

    async def start(self):
        self.client = await AsyncClient.create(self.api_key, self.secret_key)
        await self._sync_initial_positions()

        self.bm = BinanceSocketManager(self.client)
        ts = self.bm.futures_user_socket()

        print("🤖 바이낸스 봇 연결 완료. 감시 시작...")

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
            # 전체 로그 저장 (디버깅용)
            with open("b.out", "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n\n\n")

            event_type = msg.get("e")

            # 📌 1. [실시간 업데이트] 계좌 변동이 오면 내 지갑(메모리)을 즉시 갱신
            if event_type == "ACCOUNT_UPDATE":
                self._update_wallet(msg)

            # 📌 2. [알림 대기] 주문 체결이 오면 버퍼에 넣고 타이머 시작
            elif event_type == "ORDER_TRADE_UPDATE":
                self._buffer_order(msg)

        except Exception as e:
            logging.error(f"처리 중 에러: {e}")

    def _update_wallet(self, msg):
        """ACCOUNT_UPDATE 이벤트 처리: 지갑 정보 최신화"""
        data = msg.get("a", {})
        for p in data.get("P", []):
            symbol = p["s"]
            # 📌 바이낸스가 계산해준 '최신 평단가'와 '수량'을 저장
            amt = Decimal(str(p["pa"]))
            ep = Decimal(str(p["ep"]))

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

    async def _flush_buffer(self, symbol):
        """0.5초 대기 후 데이터를 취합해서 알림 전송"""

        await asyncio.sleep(0.5)

        orders = self.msg_buffer.pop(symbol, [])
        if symbol in self.flush_tasks:
            del self.flush_tasks[symbol]
        if not orders:
            return

        # --- 데이터 계산 ---
        total_qty = Decimal("0")
        total_val = Decimal("0")
        total_pnl = Decimal("0")

        side = orders[0]["S"]
        is_reduce = any(o.get("R", False) for o in orders)

        for o in orders:
            q = Decimal(str(o.get("l", "0"))) * self.SIMULATION_MULTIPLIER
            p = Decimal(str(o.get("ap", "0"))) * self.SIMULATION_MULTIPLIER
            rp = Decimal(str(o.get("rp", "0"))) * self.SIMULATION_MULTIPLIER

            total_qty += q
            total_val += p * q
            total_pnl += rp

        exec_avg_price = total_val / total_qty if total_qty > 0 else Decimal("0")

        wallet = self.active_positions.get(
            symbol, {"amt": Decimal("0"), "price": Decimal("0")}
        )
        final_ep = wallet["price"]
        final_amt = abs(wallet["amt"])

        if total_pnl == 0:
            pos_type = "롱" if side == "BUY" else "숏"
            color = "🟢" if side == "BUY" else "🔴"
        else:
            pos_type = "롱" if side == "SELL" else "숏"
            color = ""

        msg = ""

        # =========================================================
        # Case A: 청산 (익절 / 손절)
        # =========================================================
        if total_pnl != Decimal("0") or is_reduce:
            if total_pnl > 0:
                icon = "💰"
                pnl_type = "익절"
            elif total_pnl < 0:
                icon = "💧"
                pnl_type = "손절"
            else:
                icon = "⚖️"
                pnl_type = "청산"

            # 📌 [수정됨] 수량 표시 로직 변경
            if final_amt < Decimal("0.00001"):
                # 전량 청산일 때
                trade_type = f"{pnl_type}"
                detail_txt = f"/ 수량: {total_qty:,.4f} (전량 청산)"
            else:
                # 부분 청산일 때 (요청하신 부분!)
                trade_type = f"부분 {pnl_type}"
                detail_txt = f"/ 수량: {total_qty:,.4f} / 남은수량: {final_amt:,.4f}"

            # 예: 💰 [부분 익절] RIVERUSDT 롱 / 평단: xxx / 수량: 1.5 / 남은수량: 2.7
            msg = f"{icon} [{trade_type}] {symbol} {pos_type} / 평단: {exec_avg_price:,.4f} {detail_txt}\n"
            msg += f"확정손익: ${total_pnl:,.2f}"

        # =========================================================
        # Case B: 진입 (신규 / 추가 매수)
        # =========================================================
        else:
            prev_amt = final_amt - total_qty

            if prev_amt < Decimal("0.00001"):
                # 신규 진입
                msg = f"{color}[진입] {symbol} {pos_type} / 평단: {exec_avg_price:,.4f} / 수량: {total_qty:,.4f}"
            else:
                # 추가 매수
                msg = f"{color}[추가매수] {symbol} {pos_type} / 평단: {exec_avg_price:,.4f} / 수량: {total_qty:,.4f}\n"
                msg += f"➡️ 최종평단: {final_ep:,.4f} / 누적수량: {final_amt:,.4f}"

        print(msg)
        print("-" * 30)
        asyncio.create_task(send_telegram_message(msg))

    async def stop(self):
        if self.client:
            await self.client.close_connection()
