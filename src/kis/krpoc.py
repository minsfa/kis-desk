"""국내 PoC 단일 래퍼 — OpenClaw가 파라미터 없이 안전하게 실행할 1개 명령(국내판).

고정: TIGER 미국S&P500(360750, ETF) + 우리금융지주(316140, 주식), 각 1주 시장가.
흐름: 현재가 → (라이브면) 시장가 매수 → 잔고로 체결확인 → 시장가 매도 → 청산확인 → 요약.
체결확인은 잔고(balance) 기반(체결조회 ccnl 불안정 회피). 국내 정규장 09:00~15:30에만 체결.
라이브는 (--live AND .env DRY_RUN=false) 일 때만. 그 외엔 dry 리허설.
"""
from __future__ import annotations
import time

from .client import KisClient
from . import market, orders, report

ITEMS = [
    ("360750", "TIGER 미국S&P500", "ETF"),
    ("316140", "우리금융지주", "주식"),
]
QTY = 1


def _held(c: KisClient, code: str) -> int:
    for h in market.get_balance(c).get("holdings", []):
        if h.get("code") == code:
            try:
                return int(float(h.get("qty") or 0))
            except Exception:
                return 0
    return 0


def run(c: KisClient, live: bool = False) -> str:
    L = ["=== 국내 PoC (ETF·주식 각 1주 시장가 왕복) ==="]
    go_live = live and (not c.s.dry_run)
    if not go_live:
        why = "DRY_RUN=true" if c.s.dry_run else "--live 아님"
        L.append(f"(라이브 미실행: {why} → dry 리허설만)")

    for code, name, kind in ITEMS:
        L.append(f"\n--- {kind} {name}({code}) ---")
        try:
            price = int(float(market.get_price(c, code)["price"]))
        except Exception as e:
            L.append(f" 시세조회 오류: {e}"); continue
        L.append(f" 현재가 {price:,}원")

        if not go_live:
            dry = orders.buy(c, code, QTY, market=True, live=False)
            L.append(f" [dry] 매수계획 {QTY}주 시장가 ≈ {dry.get('est_amount'):,}원 (한도 {c.s.max_order_amount:,})")
            continue

        b = orders.buy(c, code, QTY, market=True, live=True)
        L.append(f" 매수 ok={b.get('ok')} no={b.get('order_no')} msg={b.get('msg')}")
        if not b.get("ok"):
            L.append("  매수 실패 → 스킵"); continue
        q = 0
        for i in range(8):
            time.sleep(2); q = _held(c, code)
            if q >= QTY:
                break
        if q < QTY:
            if b.get("order_no"):
                cx = orders.cancel(c, b.get("order_no"), b.get("org_no") or "", QTY, live=True)
                L.append(f"  미체결 → 취소 ok={cx.get('ok')} msg={cx.get('msg')}")
            continue
        L.append(f"  매수체결 {q}주")

        sresp = orders.sell(c, code, QTY, market=True, live=True)
        L.append(f" 매도 ok={sresp.get('ok')} no={sresp.get('order_no')} msg={sresp.get('msg')}")
        for i in range(8):
            time.sleep(2); q = _held(c, code)
            if q == 0:
                break
        L.append(f"  청산 보유={q}" + (" (flat)" if q == 0 else " ⚠️미청산"))

    L.append("\n요약:\n" + report.summarize_today())
    return "\n".join(L)
