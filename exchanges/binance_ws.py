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

        self.wallet_balance = Decimal("0")
        self.leverage_map = defaultdict(lambda: Decimal("1"))
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
        """봇 시작 시 현재 포지션 상태, 지갑 잔고, 레버리지 동기화"""
        try:
            # 1. 잔고 및 평단가 동기화
            account_info = await self.client.futures_account()
            self.wallet_balance = Decimal(
                str(account_info.get("totalWalletBalance", "0"))
            )

            for position in account_info["positions"]:
                symbol = position["symbol"]
                self.leverage_map[symbol] = Decimal(str(position["leverage"]))
                amt = Decimal(str(position["positionAmt"]))
                ep = Decimal(str(position["entryPrice"]))

                if amt != Decimal("0"):
                    self.active_positions[symbol].update({"amt": amt, "price": ep})

                logging.info(
                    f"초기화 - {symbol}: 레버리지 {self.leverage_map[symbol]}x"
                )

        except Exception as e:
            logging.error(f"초기화 에러: {e}")

    async def start(self):
        logging.info("바이낸스 웹소켓 시작")
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
            elif event_type == "ACCOUNT_CONFIG_UPDATE":
                self._update_leverage(msg)

        except Exception as e:
            logging.error(f"처리 중 에러: {e}")

    def _update_wallet(self, msg):
        """ACCOUNT_UPDATE 이벤트 처리: 지갑 정보 최신화"""
        data = msg.get("a", {})

        for b in data.get("B", []):
            if b["a"] == "USDT":
                self.wallet_balance = Decimal(str(b["wb"]))

        for p in data.get("P", []):
            symbol = p["s"]
            amt = Decimal(str(p["pa"]))  # 포지션 수량
            ep = Decimal(str(p["ep"]))  # 최신 평단가

            self.active_positions[symbol]["amt"] = amt
            if amt != Decimal("0"):
                self.active_positions[symbol]["price"] = ep

    def _update_leverage(self, msg):
        """ACCOUNT_CONFIG_UPDATE 처리: 레버리지 변경 시 메모리 갱신"""
        # 바이낸스 ACCOUNT_CONFIG_UPDATE는 'ac' 딕셔너리 안에 레버리지 정보를 보냅니다.
        ac = msg.get("ac")
        if ac:
            symbol = ac.get("s")
            leverage = Decimal(str(ac.get("l", "1")))
            self.leverage_map[symbol] = leverage
            logging.info(f"⚙️ 레버리지 변경 감지: {symbol} -> {leverage}x")

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

        side = orders[0]["S"]
        is_reduce = any(o.get("R", False) for o in orders)

        for o in orders:
            q = Decimal(str(o.get("l", "0"))) * multiplier
            p = Decimal(str(o.get("ap", "0")))  # 체결 가격

            total_qty += q
            total_val += p * q

        # 실행 평단가 계산 (0으로 나누기 방지)
        exec_avg_price = total_val / total_qty if total_qty > 0 else Decimal("0")

        return {
            "total_qty": total_qty,
            "exec_avg_price": exec_avg_price,
            "side": side,
            "is_reduce": is_reduce,
        }

    async def get_positions_with_pnl(self):
        """현재 포지션 + 실현손익 + 시드 비중 조회"""
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
            # 1. 현재가 조회 (PNL 계산을 위해 현재가는 여전히 API나 별도 소켓 필요)
            all_tickers = await self.client.futures_symbol_ticker()
            price_map = {t["symbol"]: Decimal(str(t["price"])) for t in all_tickers}

            # [삭제됨] 기존의 futures_position_information() 호출 부분 제거!
            # (레버리지는 self.leverage_map 메모리에서 바로 가져올 예정)

        except Exception as e:
            logging.error(f"포지션 정보 조회 에러: {e}")
            return []

        results = []
        for symbol in active_symbols:
            data = self.active_positions[symbol]
            entry_price = data["price"]
            raw_amt = data["amt"]  # 실제 수량

            sim_amt = raw_amt * self.SIMULATION_MULTIPLIER  # 뻥튀기 된 수량

            # 메모리에 누적된 실현 손익 가져오기
            realized_pnl = data.get("cum_pnl", Decimal("0"))

            current_price = price_map.get(symbol, entry_price)

            # 미실현 PNL 및 ROE 계산
            pnl = (current_price - entry_price) * sim_amt
            entry_value = entry_price * abs(sim_amt)
            roe = (pnl / entry_value) * 100 if entry_value > 0 else Decimal("0")

            # =========================================================
            # [추가] 시드 대비 비중 계산
            # =========================================================

            # [수정] 메모리(self.leverage_map)에서 해당 심볼의 레버리지를 즉시 꺼내옴 ⚡
            # (없을 경우를 대비해 getattr로 방어 로직 추가)
            leverage_map = getattr(self, "leverage_map", {})
            leverage = leverage_map.get(symbol, Decimal("1"))

            # 뻥튀기 전 실제 수량을 기준으로 실제 포지션 가치 계산
            real_position_value = entry_price * abs(raw_amt)

            # 실제 들어간 내 증거금 = 총 가치 / 레버리지
            margin_used = (
                real_position_value / leverage if leverage > 0 else Decimal("0")
            )

            # 시드 대비 비중 (%)
            # 이전 단계에서 추가했던 self.wallet_balance를 사용합니다.
            wallet_balance = getattr(self, "wallet_balance", Decimal("0"))
            seed_usage_percent = (
                (margin_used / wallet_balance) * 100
                if wallet_balance > 0
                else Decimal("0")
            )

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
                    "leverage": leverage,  # 레버리지
                    "seed_usage_percent": seed_usage_percent,  # 비중(%)
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
        exec_avg_price = agg_data["exec_avg_price"]
        side = agg_data["side"]
        is_reduce = agg_data["is_reduce"]

        # 지갑 상태 조회
        wallet = self.active_positions.get(
            symbol,
            {"amt": Decimal("0"), "price": Decimal("0"), "cum_pnl": Decimal("0")},
        )
        entry_price = wallet["price"]
        final_amt = abs(wallet["amt"]) * self.SIMULATION_MULTIPLIER

        calc_pnl = Decimal("0")
        if is_reduce and entry_price > 0:
            # 1. 숏 포지션 청산 (BUY 주문)
            # 이익 = (진입가 - 체결가) * 수량
            if side == "BUY":
                calc_pnl = (entry_price - exec_avg_price) * total_qty

            # 2. 롱 포지션 청산 (SELL 주문)
            # 이익 = (체결가 - 진입가) * 수량
            else:
                calc_pnl = (exec_avg_price - entry_price) * total_qty

        # 손익 누적 (메모리 업데이트)
        if calc_pnl != 0:
            self.active_positions[symbol]["cum_pnl"] += calc_pnl

        cumulative_pnl = self.active_positions[symbol]["cum_pnl"]

        # 포지션 방향 및 색상
        if is_reduce or calc_pnl != 0:
            pos_side = "SHORT" if side == "BUY" else "LONG"
            side_color = "🔴" if pos_side == "SHORT" else "🟢"
        else:
            pos_side = "LONG" if side == "BUY" else "SHORT"
            side_color = "🟢" if pos_side == "LONG" else "🔴"

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        leverage = self.leverage_map[symbol]

        # 메시지 작성을 위한 리스트 (나중에 join으로 합침)
        lines = []

        # =========================================================
        # Case A: 청산 (익절 / 손절)
        # =========================================================
        if calc_pnl != Decimal("0") or is_reduce:
            # 1. 전체 청산
            if final_amt < Decimal("0.00001"):
                lines.append(f"💵 *전체 청산 ({pos_side})*")
                lines.append("")
                lines.append(f"• *종목*:{side_color} `{symbol} ({leverage}x)`")
                lines.append("──────────────")
                lines.append(f"• *정리수량*: `{total_qty:,}`")
                lines.append(f"• *종료가격*: `{f(exec_avg_price)}`")
                lines.append(f"• *마지막 손익*: `{f(calc_pnl, '0.001')}` USDT")
                lines.append("──────────────")
                lines.append(f"💰*최종 확정이익*: `{f(cumulative_pnl, '0.001')}` USDT")
                # 리셋
                self.active_positions[symbol]["cum_pnl"] = Decimal("0")

            # 2. 부분 청산``
            else:
                pnl_icon = "🎉" if calc_pnl > 0 else "💧"
                cum_icon = "💰" if cumulative_pnl > 0 else "💸"

                # 청산한 비율 퍼센트 계산
                liquidation_ratio = (total_qty / (total_qty + final_amt)) * Decimal(
                    "100"
                )

                lines.append(f"⚠️*부분 청산 ({pos_side})*")
                lines.append("")
                lines.append(f"• *종목*:{side_color} `{symbol} ({leverage}x)`")
                lines.append("──────────────")
                lines.append(f"• *이전수량*: `{(total_qty + final_amt):,}`")
                lines.append(
                    f"• *정리수량*: `{total_qty:,}` ({f(liquidation_ratio, '0.01')}%)"
                )
                lines.append(f"• *남은수량*: `{final_amt:,}`")
                lines.append(f"• *체결가격*: `{f(exec_avg_price)}`")
                lines.append("──────────────")
                lines.append(f"• *이번손익*: {pnl_icon} `{f(calc_pnl, '0.001')}` USDT")
                lines.append(
                    f"• *누적실현*: {cum_icon} `{f(cumulative_pnl, '0.001')}` USDT"
                )
        # =========================================================
        # Case B: 진입 (신규 / 추가)
        # =========================================================
        else:
            prev_amt = final_amt - total_qty

            # [수정] API 호출 없이 메모리에서 바로 레버리지 가져오기 ⚡

            # 봇에서 100배 뻥튀기 된 수량을 실제 수량으로 되돌림
            actual_final_amt = final_amt / self.SIMULATION_MULTIPLIER

            # 현재 포지션의 총 가치 (실제 수량 * 진입 평단가)
            total_position_value = actual_final_amt * entry_price

            # 실제 들어간 내 돈(증거금) = 총 가치 / 레버리지
            margin_used = (
                total_position_value / leverage if leverage > 0 else Decimal("0")
            )

            # 시드 대비 비중 (%)
            seed_usage_percent = (
                (margin_used / self.wallet_balance) * 100
                if self.wallet_balance > 0
                else Decimal("0")
            )

            if prev_amt < Decimal("0.00001"):
                # 신규 진입
                self.active_positions[symbol]["cum_pnl"] = Decimal("0")

                lines.append(f"🍀 *신규 진입 ({pos_side})*")
                lines.append("")
                lines.append(f"• *종목*: {side_color} `{symbol} ({leverage}x)`")
                lines.append("──────────────")
                lines.append(f"• *진입수량*: `{f(total_qty)}`")
                lines.append(f"• *진입가격*: `{price_f(exec_avg_price, symbol)}`")
                lines.append(f"• *시드비중*: `{f(seed_usage_percent, '0.01')}%`")
            else:
                # 추가 진입 (물타기/불타기)
                lines.append(f"🌊 *추가 진입 ({pos_side})*")
                lines.append("")
                lines.append(f"• *종목*: {side_color} `{symbol} ({leverage}x)`")
                lines.append("──────────────")
                lines.append(f"• *추가수량*: `{f(total_qty)}`")
                lines.append(f"• *추매가격*: `{price_f(exec_avg_price, symbol)}`")
                lines.append(f"• *최종평단*: `{price_f(entry_price, symbol)}`")
                lines.append(f"• *보유수량*: `{f(final_amt)}`")
                lines.append(f"• *총 비중*: `{f(seed_usage_percent, '0.01')}%`")

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
