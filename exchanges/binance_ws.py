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
        self.active_positions = {}
        self.msg_buffer = defaultdict(list)

    async def _sync_initial_positions(self):
        """초기 포지션 동기화"""
        try:
            print("🔄 바이낸스 초기 포지션 동기화 중...")
            account_info = await self.client.futures_account()

            for position in account_info["positions"]:
                symbol = position["symbol"]
                # 📌 API에서 오는 문자열을 바로 Decimal로 변환
                amt = Decimal(str(position["positionAmt"]))

                if amt != Decimal("0"):
                    self.active_positions[symbol] = amt
                    print(f"   ✅ 보유 확인: {symbol} ({amt}개)")

            print("🆗 바이낸스 포지션 동기화 완료!")

        except Exception as e:
            logging.error(f"바이낸스 초기 포지션 동기화 실패: {e}")

    async def start(self):
        """
        비동기 방식(BinanceSocketManager)으로 웹소켓 연결
        """
        # 1. AsyncClient 생성 (await 필요)
        self.client = await AsyncClient.create(
            api_key=self.api_key, api_secret=self.secret_key
        )

        await self._sync_initial_positions()

        # 2. 소켓 매니저 초기화
        self.bm = BinanceSocketManager(self.client)

        # 3. 선물(Futures) 유저 데이터 소켓 가져오기
        ts = self.bm.futures_user_socket()

        print("🤖 바이낸스 웹소켓(Async) 연결 성공! 데이터 수신 대기 중...")

        # 4. 소켓 연결 및 메시지 루프 (async with)
        async with ts as tscm:
            while True:
                try:
                    # 메시지 수신 대기 (여기서 멈춰 있다가 메시지 오면 실행됨)
                    res = await tscm.recv()

                    # 메시지 처리 핸들러 호출
                    self._handle_socket_message(res)

                except Exception as e:
                    logging.error(f"웹소켓 수신 중 에러: {e}")
                    # 에러 발생 시 잠시 대기 후 재시도 or 루프 유지
                    await asyncio.sleep(1)

    def _update_wallet(self, msg):
        """ACCOUNT_UPDATE 이벤트 처리: 지갑 정보 최신화"""
        data = msg.get("a", {})
        for p in data.get("P", []):
            symbol = p["s"]  # Symbol
            amt = Decimal(str(p["pa"]))  # 수량
            ep = Decimal(str(p["ep"]))  # 평단가

            self.active_positions[symbol] = {"amt": amt, "price": ep}

    def _handle_socket_message(self, msg):
        """
        메시지 파싱 및 로직 분기
        """
        try:
            with open("b.out", "a", encoding="utf-8") as f:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n\n\n")

            event_type = msg.get("e")

            if event_type == "error":
                logging.error(f"바이낸스 WebSocket Error: {msg}")
                return

            if event_type == "ACCOUNT_UPDATE":
                self._update_wallet(msg)

            if event_type == "ORDER_TRADE_UPDATE":
                self._process_order_update(msg)

        except Exception as e:
            logging.error(f"메시지 처리 로직 에러: {e}")

    def _process_order_update(self, msg):
        """주문 데이터를 버퍼에 넣고 타이머를 시작하는 함수"""
        order_data = msg.get("o", {})

        # 필터링 (체결된 것만)
        if order_data.get("X") not in ["FILLED", "PARTIALLY_FILLED"]:
            return
        if order_data.get("x") != "TRADE":
            return

        symbol = order_data.get("s")

        # 1. 버퍼에 데이터 추가 (보내지 않고 저장만 함)
        self.msg_buffer[symbol].append(order_data)

        # 2. 해당 코인에 대해 이미 돌아가는 타이머가 없다면, 새 타이머 시작
        if symbol not in self.flush_tasks:
            self.flush_tasks[symbol] = asyncio.create_task(self._flush_buffer(symbol))

    async def _flush_buffer(self, symbol):
        """
        1초 대기 후 데이터를 취합해서 알림을 보내는 함수 (Decimal 적용)
        """
        # 1초 버퍼링
        await asyncio.sleep(1)

        orders = self.msg_buffer.pop(symbol, [])
        if symbol in self.flush_tasks:
            del self.flush_tasks[symbol]

        if not orders:
            return

        total_qty = Decimal("0")
        total_value = Decimal("0")
        total_pnl = Decimal("0")

        side = orders[0]["S"]
        is_reduce_only = any(o.get("R", False) for o in orders)

        for o in orders:
            q = Decimal(str(o.get("l", "0")))  # 체결 수량
            p = Decimal(str(o.get("ap", "0")))  # 체결 가격
            rp = Decimal(str(o.get("rp", "0")))  # 실현 손익

            total_qty += q
            total_value += p * q
            total_pnl += rp

        # 평균 체결가 계산 (ZeroDivisionError 방지)
        if total_qty > Decimal("0"):
            avg_price = total_value / total_qty
        else:
            avg_price = Decimal("0")

        # --- 메시지 생성 및 전송 ---
        # Case A: 청산 (익절/손절) - PnL이 0이 아니거나 ReduceOnly인 경우
        if total_pnl != Decimal("0") or is_reduce_only:
            event_type = "청산"
            emoji = "⚖️"

            if total_pnl > Decimal("0"):
                event_type = "익절"
                emoji = "💰"
            elif total_pnl < Decimal("0"):
                event_type = "손절"
                emoji = "💧"

            print(f"{emoji} [{event_type}] {symbol} {side} (합산)")
            print(f" - 총 수량: {total_qty:,.4f}")
            print(f" - 평균 매도가: {avg_price:,.4f}")
            print(f" - 확정 손익: ${total_pnl:,.2f}")

        # Case B: 진입 (신규/물타기)
        else:
            position_side = "롱" if side == "BUY" else "숏"

            print(f"🚀 [포지션 진입/추가] {symbol} {position_side} (합산)")
            print(f" - 평균 진입가: {avg_price:,.4f}")
            print(f" - 총 수량: {total_qty:,.4f}")

        print("-" * 30)

    async def stop(self):
        """종료 처리"""
        if self.client:
            await self.client.close_connection()
