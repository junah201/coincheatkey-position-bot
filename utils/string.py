from decimal import ROUND_DOWN, Decimal


def f(value: Decimal, quantize_str: str = "0.00000001") -> str:
    """
    Decimal을 받아서 예쁜 문자열로 변환하는 함수
    1. 소수점 8자리 이하 버림 (ROUND_DOWN)
    2. 소수점 뒤 불필요한 0 제거 (Normalize)
    3. 천 단위 콤마 적용
    """
    if value is None:
        return "0"

    # 1. 소수점 8자리까지만 남기고 버림 (Truncate)
    # 예: 10.123456789 -> 10.12345678
    # 예: 10.5 -> 10.50000000 (일단 0이 채워짐)
    quantized = value.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)

    # 2. 고정 소수점 문자열로 변환 ('f' 포맷은 과학적 표기법 1E-8 등을 방지함)
    s = format(quantized, "f")

    # 3. 우측의 불필요한 '0'과 불필요한 소수점 '.' 제거 (Normalize 효과)
    if "." in s:
        s = s.rstrip("0").rstrip(".")

    # 4. 정수부에 천 단위 콤마 찍기
    if "." in s:
        integer_part, decimal_part = s.split(".")
        return f"{int(integer_part):,}.{decimal_part}"
    else:
        return f"{int(s):,}"


def price_f(value: Decimal, symbol: str):
    if symbol in ["BTCUSDT", "ETHUSDT"]:
        return f(value, "0.01")

    return f(value)
