"""저가 '일반' 종목 자동 선정 — PoC 테스트용.

레버리지/인버스는 후보에서 제외(전부 일반 ETF/주식만 큐레이션).
예산 안에서 1주 이상 살 수 있는 종목을 현재가 조회로 골라준다.
"""
from __future__ import annotations

from .client import KisClient
from .market import get_price
from .logging_util import log_event

# 일반(비레버리지·비인버스) 후보. 단가 변동하므로 실행 시점에 현재가로 선별.
CANDIDATES = [
    ("360750", "TIGER 미국S&P500"),
    ("379800", "KODEX 미국S&P500"),
    ("069500", "KODEX 200"),
    ("153130", "KODEX 단기채권"),
    ("214980", "KODEX 단기채권PLUS"),
    ("357870", "TIGER CD금리투자KIS"),
    ("329750", "TIGER 미국채10년선물"),
    ("148070", "KOSEF 국고채10년"),
]


def pick(c: KisClient, budget: int, min_shares: int = 1) -> list[dict]:
    """예산으로 min_shares주 이상 살 수 있는 일반 종목을 (저가순 등) 정렬해 반환."""
    rows = []
    for code, name in CANDIDATES:
        try:
            p = get_price(c, code)
            price = int(float(p["price"]))
        except Exception as e:
            rows.append({"code": code, "name": name, "error": str(e)})
            continue
        if price <= 0:
            continue
        affordable = budget // price
        rows.append({"code": code, "name": name, "price": price,
                     "affordable_shares": affordable,
                     "fits": affordable >= min_shares})
    rows.sort(key=lambda r: (not r.get("fits", False), r.get("price", 1e12)))
    log_event("screener", budget=budget, n=len(rows),
              picked=next((r["code"] for r in rows if r.get("fits")), None))
    return rows
