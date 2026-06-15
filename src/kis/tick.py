"""국내주식 호가단위(tick) 계산·반올림.

지정가 주문 시 가격이 호가단위 배수가 아니면 '주식주문호가단위 오류'로 거부된다.
KRX 호가단위(2023 개편 기준):
  <2,000:1 / <5,000:5 / <20,000:10 / <50,000:50 / <200,000:100 / <500,000:500 / 그이상:1,000
"""
from __future__ import annotations
import math

_BANDS = [(2000, 1), (5000, 5), (20000, 10), (50000, 50),
          (200000, 100), (500000, 500)]


def tick_size(price: float) -> int:
    for hi, t in _BANDS:
        if price < hi:
            return t
    return 1000


def round_tick(price: float, up: bool | None = None) -> int:
    """호가단위에 맞춰 정수 가격으로. up=True 올림(매수용), False 내림(매도용), None 반올림."""
    t = tick_size(price)
    if up is True:
        return int(math.ceil(price / t)) * t
    if up is False:
        return int(math.floor(price / t)) * t
    return int(round(price / t)) * t
