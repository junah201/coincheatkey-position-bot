import asyncio
import logging
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from binance import AsyncClient, BinanceSocketManager

from exchanges.base import ExchangeWebSocket
from utils import get_required_env
from utils.string import f, price_f
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

        # [변경 1] 누적 손익(cum_pnl) 필드 추가
        self.active_positions = defaultdict(
            lambda: {
                "amt": Decimal("0"),
                "price": Decimal("0"),
                "cum_pnl": Decimal("0"),
            }
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
                    # 초기화 시에는 과거 내역을 모르니 cum_pnl은 0으로 시작
                    self.active_positions[symbol].update({"amt": amt, "price": ep})
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

            # [변경 2] 딕셔너리를 통째로 덮어쓰지 않고, 수량과 평단만 업데이트
            # (이유: cum_pnl 기록을 유지하기 위함)
            self.active_positions[symbol]["amt"] = amt
            self.active_positions[symbol]["price"] = ep

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

        side = orders[0]["S"]
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

    async def get_positions_with_pnl(self):
        """현재 포지션 + 실현손익 조회"""
        if not self.active_positions or not self.client:
            return []

        active_symbols = [
            s
            for s, data in self.active_positions.items()
            if data["amt"] != Decimal("0")
        ]
        if not active_symbols:
            return []

        try:
            all_tickers = await self.client.futures_symbol_ticker()
            price_map = {t["symbol"]: Decimal(str(t["price"])) for t in all_tickers}
        except Exception:
            return []

        results = []
        for symbol in active_symbols:
            data = self.active_positions[symbol]
            entry_price = data["price"]
            raw_amt = data["amt"]

            sim_amt = raw_amt * self.SIMULATION_MULTIPLIER

            # [추가] 메모리에 누적된 실현 손익 가져오기
            realized_pnl = data.get("cum_pnl", Decimal("0"))

            current_price = price_map.get(symbol, entry_price)
            pnl = (current_price - entry_price) * sim_amt
            entry_value = entry_price * abs(sim_amt)
            roe = (pnl / entry_value) * 100 if entry_value > 0 else Decimal("0")

            results.append(
                {
                    "symbol": symbol,
                    "side": "🟢 롱" if raw_amt > 0 else "🔴 숏",
                    "amount": sim_amt,
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "pnl": pnl,  # 미실현
                    "realized_pnl": realized_pnl,  # 실현
                    "roe": roe,
                }
            )
        return results

    async def _flush_buffer(self, symbol):
        """0.5초 대기 후 데이터를 취합해서 알림 전송 (디자인 업그레이드 버전)"""

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
            symbol,
            {"amt": Decimal("0"), "price": Decimal("0"), "cum_pnl": Decimal("0")},
        )
        final_ep = wallet["price"]
        final_amt = abs(wallet["amt"]) * self.SIMULATION_MULTIPLIER

        # 손익 누적 (메모리 업데이트)
        if total_pnl != 0:
            self.active_positions[symbol]["cum_pnl"] += total_pnl

        cumulative_pnl = self.active_positions[symbol]["cum_pnl"]

        # 포지션 방향 및 색상
        if is_reduce or total_pnl != 0:
            pos_side = "SHORT" if side == "BUY" else "LONG"
            side_color = "🔴" if pos_side == "SHORT" else "🟢"
        else:
            pos_side = "LONG" if side == "BUY" else "SHORT"
            side_color = "🟢" if pos_side == "LONG" else "🔴"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 메시지 작성을 위한 리스트 (나중에 join으로 합침)
        lines = []

        # =========================================================
        # Case A: 청산 (익절 / 손절)
        # =========================================================
        if total_pnl != Decimal("0") or is_reduce:
            # 1. 전체 청산
            if final_amt < Decimal("0.00001"):
                lines.append(f"💵 *전체 청산 ({pos_side})*")
                lines.append("")
                lines.append(f"• *종목*:{side_color} `{symbol}`")
                lines.append("──────────────")
                lines.append(f"• *정리수량*: `{total_qty:,}`")
                lines.append(f"• *종료가격*: `{f(exec_avg_price)}`")
                lines.append(f"• *마지막 손익*: `{f(total_pnl, '0.001')}` USDT")
                lines.append("──────────────")
                lines.append(f"💰*최종 확정이익*: `{f(cumulative_pnl, '0.001')}` USDT")
                # 리셋
                self.active_positions[symbol]["cum_pnl"] = Decimal("0")

            # 2. 부분 청산
            else:
                pnl_icon = "🎉" if total_pnl > 0 else "💧"
                cum_icon = "💰" if cumulative_pnl > 0 else "💸"

                lines.append(f"⚠️*부분 청산 ({pos_side})*")
                lines.append("")
                lines.append(f"• *종목*:{side_color} `{symbol}`")
                lines.append("──────────────")
                lines.append(f"• *전수량*: `{(total_qty + final_amt):,}`")
                lines.append(f"• *정리수량*: `{total_qty:,}`")
                lines.append(f"• *남은수량*: `{final_amt:,}`")
                lines.append(f"• *체결가격*: `{f(exec_avg_price)}`")
                lines.append("──────────────")
                lines.append(f"• *이번손익*: {pnl_icon} `{f(total_pnl, '0.001')}` USDT")
                lines.append(
                    f"• *누적실현*: {cum_icon} `{f(cumulative_pnl, '0.001')}` USDT"
                )
        # =========================================================
        # Case B: 진입 (신규 / 추가)
        # =========================================================
        else:
            prev_amt = final_amt - total_qty

            if prev_amt < Decimal("0.00001"):
                # 신규 진입
                self.active_positions[symbol]["cum_pnl"] = Decimal("0")

                lines.append(f"🍀 *신규 진입 ({pos_side})*")
                lines.append("")
                lines.append(f"• *종목*: {side_color} `{symbol}`")
                lines.append("──────────────")
                lines.append(f"• *진입수량*: `{f(total_qty)}`")
                lines.append(f"• *진입가격*: `{price_f(exec_avg_price, symbol)}`")
            else:
                # 추가 진입 (물타기/불타기)
                lines.append(f"🌊 *추가 진입 ({pos_side})*")
                lines.append("")
                lines.append(f"• *종목*: {side_color} `{symbol}`")
                lines.append("──────────────")
                lines.append(f"• *추가수량*: `{f(total_qty)}`")
                lines.append(f"• *추매가격*: `{price_f(exec_avg_price, symbol)}`")
                lines.append(f"• *최종평단*: `{price_f(final_ep, symbol)}`")
                lines.append(f"• *보유수량*: `{f(final_amt)}`")

        # 공통 하단 (시간)
        lines.append(f"• *시간*: `{now_str}`")

        # 최종 메시지 조립
        msg = "\n".join(lines)

        print(msg)
        print("-" * 30)
        asyncio.create_task(send_telegram_message(msg))

    async def stop(self):
        if self.client:
            await self.client.close_connection()
