import asyncio
import logging

from binance import AsyncClient, BinanceSocketManager

from exchanges.base import ExchangeWebSocket
from utils import get_required_env


class BinanceWebSocket(ExchangeWebSocket):
    def __init__(self):
        super().__init__(
            api_key=get_required_env("BINANCE_API_KEY"),
            secret_key=get_required_env("BINANCE_SECRET_KEY"),
        )
        self.client = None
        self.bm = None

    async def start(self):
        """
        비동기 방식(BinanceSocketManager)으로 웹소켓 연결
        """
        # 1. AsyncClient 생성 (await 필요)
        self.client = await AsyncClient.create(
            api_key=self.api_key, api_secret=self.secret_key
        )

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

    def _handle_socket_message(self, msg):
        """
        메시지 파싱 및 로직 분기
        """
        try:
            # 1. 에러 메시지 처리
            if msg.get("e") == "error":
                logging.error(f"WebSocket Error: {msg}")
                return

            # 2. 주문/포지션 변동 이벤트 확인
            if msg.get("e") == "ORDER_TRADE_UPDATE":
                self._process_order_update(msg)

            # (참고) ListenKey 갱신 등은 BinanceSocketManager가 내부적으로 처리하려 시도함
            # 하지만 연결이 끊기면 위 while 루프에서 예외가 잡히고 다시 recv()를 시도해야 함

        except Exception as e:
            logging.error(f"메시지 처리 로직 에러: {e}")

    def _process_order_update(self, msg):
        """주문 체결 정보 분석 로직 (기존과 동일)"""
        order_data = msg.get("o", {})
        symbol = order_data.get("s")
        side = order_data.get("S")
        order_status = order_data.get("X")
        exec_type = order_data.get("x")
        price = float(order_data.get("ap", 0))
        qty = float(order_data.get("q", 0))
        realized_pnl = float(order_data.get("rp", 0))
        is_reduce_only = order_data.get("R", False)

        if order_status not in ["FILLED", "PARTIALLY_FILLED"]:
            return

        if exec_type != "TRADE":
            return

        # --- 출력 로그 (기존과 동일) ---
        if realized_pnl != 0 or is_reduce_only:
            if realized_pnl > 0:
                print(f"💰 익절 알림! {symbol} {side} / 수익금: {realized_pnl} USDT")
            elif realized_pnl < 0:
                print(f"💧 손절 알림... {symbol} {side} / 손실금: {realized_pnl} USDT")
            else:
                print(f"⚖️ 본절 알림... {symbol} {side} / 손익: {realized_pnl} USDT")
        else:
            print(f"🚀 포지션 진입! {symbol} {side} / 가격: {price} / 수량: {qty}")

    async def stop(self):
        """종료 처리"""
        if self.client:
            await self.client.close_connection()
