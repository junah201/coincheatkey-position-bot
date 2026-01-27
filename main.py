# main.py
import asyncio
import os

from dotenv import load_dotenv

from exchanges.binance_ws import BinanceWebSocket

# .env 로드
load_dotenv()


async def main():
    # 1. 텔레그램 봇 초기화
    print("🤖 텔레그램 봇 초기화 완료")

    binance = BinanceWebSocket()
    await binance.start()

    while True:
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
