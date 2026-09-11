"""2026-09 잡주 8종 정리 계획 감시 — 크론용. 매매 없음, 알림만 (매도는 지시 대기).
신호 시에만 첫 줄이 이모지로 시작, 평소 'OK'(침묵). holdings_watch.py 패턴 준수.

계획 (2026-09-08 확정, life-ops #13):
  1) 9/8~9/23  반등 매도 — 당일 +3% 이상이면 해당 종목 전량 매도 검토 (💸)
  2) 9/22(화)  1차 강제 — 소형 5종 미매도분 전량 시장가 (📅)
  3) 9/29(화)  2차 강제 — 나머지 3종 잔량 전량, 9/30 예비일 (📅)
  4) 9/24~25 추석 휴장, 9/28 개장 (KIS 휴장일 조회로 확인)
JYP(035900)는 제외.
"""
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트

from src.kis.config import load_settings
from src.kis.client import KisClient
from src.kis.fundamentals import get_fundamentals
from src.kis.market import get_balance

TRANCHE_1 = [  # 9/22 강제 — 소형 5종
    ("마니커에프앤지", "195500"),
    ("동국S&C", "100130"),
    ("새빗켐", "107600"),
    ("이연제약", "102460"),
    ("아난티", "025980"),
]
TRANCHE_2 = [  # 9/29 강제 — 나머지 3종
    ("제이엔케이글로벌", "126880"),
    ("카카오", "035720"),
    ("아모레퍼시픽", "090430"),
]
BOUNCE_PCT = 3.0
FORCE_1 = date(2026, 9, 22)
FORCE_2 = date(2026, 9, 29)
END = date(2026, 9, 30)


def _quote(c, code):
    for _ in range(3):
        try:
            f = get_fundamentals(c, code)
            px = f.get("price")
            if px:
                return float(px), float(f.get("change_pct") or 0.0)
        except Exception:
            pass
        time.sleep(1.0)
    return None, None


def main():
    today = date.today()
    if today > END:
        print("🏁 9월 정리 계획 종료일 경과 — 크론 kis-sep-cleanup 삭제 필요.")
        return

    c = KisClient(load_settings("prod"))
    held = {h["code"]: int(h["qty"]) for h in get_balance(c).get("holdings", []) if int(h["qty"]) > 0}
    alerts, info = [], []

    for label, group, force_day in (("1차", TRANCHE_1, FORCE_1), ("2차", TRANCHE_2, FORCE_2)):
        remain = [(nm, code) for nm, code in group if code in held]
        info.append(f"{label} 잔여 {len(remain)}/{len(group)}")
        if not remain:
            continue
        if today >= force_day:
            names = " · ".join(f"{nm} {held[code]}주" for nm, code in remain)
            tag = "강제 매도일" if today == force_day else "강제 매도 기한 경과"
            alerts.append(f"📅 [{label} {tag}] 시장가 전량 매도 지시 필요: {names}")
            continue
        for nm, code in remain:
            px, chg = _quote(c, code)
            time.sleep(0.3)
            if px is None:
                continue
            if chg >= BOUNCE_PCT:
                alerts.append(f"💸 [{label}] {nm} {px:,.0f}원 ({chg:+.1f}%) 반등 — {held[code]}주 전량 매도 지시?")

    if alerts:
        print("\n".join(alerts))
        print("--- 참고: " + " · ".join(info))
    else:
        print("OK 트리거 없음 | " + " · ".join(info))


if __name__ == "__main__":
    main()
