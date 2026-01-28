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
    def __init__(self):
        super().__init__(
            api_key=get_required_env("BINANCE_API_KEY"),
            secret_key=get_required_env("BINANCE_SECRET_KEY"),
        )
        self.client = None
        self.bm = None

        # 📌 [지갑] 실시간 포지션 정보 (수량, 평단가) 저장소
        # 구조: { "BTCUSDT": { "amt": Decimal("0.5"), "price": Decimal("60100.5") } }
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
        """1초 뒤에 모아서 알림 전송"""

        # ⏳ 1초 대기: 이 사이에 ACCOUNT_UPDATE가 도착해서 self.active_positions를 갱신해줌!
        await asyncio.sleep(1)

        orders = self.msg_buffer.pop(symbol, [])
        if symbol in self.flush_tasks:
            del self.flush_tasks[symbol]
        if not orders:
            return

        # --- 데이터 계산 ---
        total_qty = Decimal("0")
        total_value = Decimal("0")
        total_pnl = Decimal("0")

        side = orders[0]["S"]
        is_reduce = any(o.get("R", False) for o in orders)

        for o in orders:
            q = Decimal(str(o.get("l", "0")))
            p = Decimal(str(o.get("ap", "0")))
            rp = Decimal(str(o.get("rp", "0")))

            total_qty += q
            total_value += p * q
            total_pnl += rp

        # 이번 체결들의 평균 가격
        trade_avg_price = total_value / total_qty if total_qty > 0 else Decimal("0")

        # 📌 [핵심] 갱신된 지갑 정보 가져오기 (최신 평단가)
        wallet = self.active_positions.get(
            symbol, {"amt": Decimal("0"), "price": Decimal("0")}
        )
        final_entry_price = wallet["price"]
        final_amt = wallet["amt"]

        # --- 알림 출력 ---

        # Case A: 청산 (수익/손실 확정)
        if total_pnl != Decimal("0") or is_reduce:
            event = "익절" if total_pnl > 0 else "손절" if total_pnl < 0 else "청산"
            emoji = "💰" if total_pnl > 0 else "💧" if total_pnl < 0 else "⚖️"

            print(f"{emoji} [{event}] {symbol} {side} (합산)")
            print(f" - 수익금: ${total_pnl:,.2f}")
            print(f" - 매도량: {total_qty:,.4f} (평단: {trade_avg_price:,.4f})")

            if final_amt != Decimal("0"):
                print(f" ✨ 남은 물량 평단: {final_entry_price:,.4f}")
            else:
                print(" ✨ 포지션 완전 종료")

        # Case B: 진입 (신규/물타기)
        else:
            pos_side = "롱" if side == "BUY" else "숏"
            print(f"🚀 [진입/추가] {symbol} {pos_side} (합산)")
            print(f" - 체결가: {trade_avg_price:,.4f}")
            print(f" - 수량: {total_qty:,.4f}")

            # 여기서 물타기가 반영된 최종 평단가가 나옴!
            print(f" ✨ 최종 평단: {final_entry_price:,.4f}")

        print("-" * 30)

    async def stop(self):
        if self.client:
            await self.client.close_connection()
