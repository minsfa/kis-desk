"""주간 포트폴리오 브리핑 (월요일 아침) — 크론용. read-only, 매매 없음.
한투(KIS) 계좌 요약 + 매도·매수 타겟 거리 + ISA 관찰종목 기준선 거리를 한 장으로.
운영 모드(2026-08-14): 한투=타겟 알림→지시 시 실행 / ISA=주·월 저템포."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트

from src.kis.config import load_settings
from src.kis.client import KisClient
from src.kis.fundamentals import get_fundamentals
from src.kis import market

# 한투 타겟 (2026-08-14 관리안)
KIS_TARGETS = [
    ("삼성전자", "005930", "🎯 490,000 도달 시 부분익절(15주 제안) / ❄️ 268,000 이탈 시 축소", None),
    ("삼성물산", "028260", "매수 1순위 — 예수금 생기면 우선 배치", None),
]
# ISA 관찰 기준선 (도달 시 즉시 제안 대상)
ISA_LINES = [
    ("하나금융지주", "086790", 108_000, "이하 매수(배당 4%)"),
    ("신한지주", "055550", 92_500, "이하 매수(배당 3.2%)"),
    ("TIGER 배당다우", "458730", None, "정기 적립 축"),
    ("ACE 싱가포르리츠", "316300", None, "신규 중단(희석)"),
    ("두산에너빌리티", "034020", None, "실적 확인 후 분류"),
]


def _px(c, code):
    for _ in range(3):
        try:
            f = get_fundamentals(c, code)
            if f.get("price"):
                return float(f["price"]), float(f.get("change_pct") or 0)
        except Exception:
            pass
        time.sleep(1.0)
    return None, None


def main():
    c = KisClient(load_settings("prod"))
    out = ["📋 주간 포트폴리오 브리핑"]

    # 1. 한투 계좌 요약
    try:
        bal = market.get_balance(c)
        holds = bal.get("holdings", [])
        buy = ev = 0.0
        movers = []
        for h in holds:
            q = float(h.get("qty") or 0)
            a = float(h.get("avg_price") or 0)
            p = float(h.get("eval_pnl") or 0)
            buy += q * a
            ev += q * a + p
            movers.append((h.get("name"), p, float(h.get("pnl_pct") or 0)))
        pnl = ev - buy
        out.append(f"\n■ 한투: 평가 {ev:,.0f} / 손익 {pnl:+,.0f} ({pnl/buy*100:+.2f}%) / {len(holds)}종목")
        movers.sort(key=lambda m: m[1])
        worst = " · ".join(f"{n} {p/1e4:+,.0f}만" for n, p, _ in movers[:3])
        best = " · ".join(f"{n} {p/1e4:+,.0f}만" for n, p, _ in movers[-3:][::-1])
        out.append(f"  상위: {best}")
        out.append(f"  하위: {worst}")
    except Exception as e:
        out.append(f"\n■ 한투 잔고 조회 실패: {e}")

    # 2. 한투 타겟 거리
    out.append("\n■ 한투 타겟")
    for nm, code, note, _ in KIS_TARGETS:
        px, chg = _px(c, code)
        time.sleep(0.3)
        s = f"{px:,.0f} ({chg:+.1f}%)" if px else "조회실패"
        out.append(f"  {nm} {s} — {note}")

    # 3. ISA 기준선 거리 (저템포 — 주간 확인용)
    out.append("\n■ ISA 관찰 (기준선 도달 시 즉시 제안)")
    for nm, code, line, note in ISA_LINES:
        px, chg = _px(c, code)
        time.sleep(0.3)
        if px is None:
            out.append(f"  {nm} 조회실패")
            continue
        if line:
            gap = (px / line - 1) * 100
            mark = "✅ 도달!" if px <= line else f"{gap:+.1f}%"
            out.append(f"  {nm} {px:,.0f} → 기준 {line:,} ({mark}) — {note}")
        else:
            out.append(f"  {nm} {px:,.0f} ({chg:+.1f}%) — {note}")

    print("\n".join(out))


if __name__ == "__main__":
    main()
