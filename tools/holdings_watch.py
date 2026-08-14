"""보유종목 1단계 트리거 감시 (2026-08-14 관리안) — 크론용. 매매 없음, 알림만.
신호 시에만 첫 줄이 이모지로 시작, 평소 'OK'(침묵). semi_watch.py 패턴 준수.

트리거:
  A. 삼성전자 — 지지선 268,000 종가 이탈(❄️) / 컨센 목표가 490,000 도달 시 부분익절 검토(🎯)
  B. 정리·축소군 — 당일 +5% 이상 반등 시 분할 매도 기회 알림(💸)
  C. 제이엔케이글로벌 — DART 유상증자 공시 감지(⚠️)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트

from src.kis.config import load_settings
from src.kis.client import KisClient
from src.kis.fundamentals import get_fundamentals
from src.kis import dart

SAMSUNG = ("삼성전자", "005930", 268_000, 490_000)  # (이름, 코드, 지지선, 목표가)

# 반등 시 분할 매도 검토 대상 (2026-08-14 테마 조사 결론)
EXIT_GROUP = [  # 정리(손절 우선)
    ("마니커에프앤지", "195500"),
    ("새빗켐", "107600"),
]
TRIM_GROUP = [  # 축소(반등 시 분할)
    ("카카오", "035720"),
    ("이연제약", "102460"),
    ("제이엔케이글로벌", "126880"),
    ("대덕전자", "353200"),
    ("TIGER 200 IT", "139260"),
]
BOUNCE_PCT = 5.0  # 당일 상승률 알림 문턱

JNK_CODE = "126880"
RIGHTS_KEYWORDS = ("유상증자", "신주", "전환사채", "교환사채")


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
    c = KisClient(load_settings("prod"))
    alerts, info = [], []

    # A. 삼성전자 레벨
    nm, code, floor, target = SAMSUNG
    px, chg = _quote(c, code)
    time.sleep(0.3)
    if px is None:
        alerts.append(f"⚡ {nm} 시세 조회 실패")
    else:
        info.append(f"{nm} {px:,.0f} ({chg:+.1f}%)")
        if px < floor:
            alerts.append(f"❄️ {nm} {px:,.0f}원 — 지지선 {floor:,}원 이탈. 트리거 점검(비중 축소 검토).")
        elif px >= target:
            alerts.append(f"🎯 {nm} {px:,.0f}원 — 컨센 목표가 {target:,}원 도달. 부분 익절(12~18주) 검토.")

    # B. 정리·축소군 반등
    for group, label in ((EXIT_GROUP, "정리군"), (TRIM_GROUP, "축소군")):
        for nm, code in group:
            px, chg = _quote(c, code)
            time.sleep(0.3)
            if px is None:
                continue
            if chg >= BOUNCE_PCT:
                alerts.append(f"💸 [{label}] {nm} {px:,.0f}원 ({chg:+.1f}%) 반등 — 분할 매도 검토.")

    # C. JNK 유상증자 공시
    try:
        for d in dart.recent_disclosures(JNK_CODE, days=3) or []:
            title = str(d.get("report_nm") or d.get("title") or "")
            if any(k in title for k in RIGHTS_KEYWORDS):
                alerts.append(f"⚠️ 제이엔케이글로벌 공시: {title} — 희석 리스크, 축소 판단 재점검.")
    except Exception as e:
        info.append(f"(dart 조회 실패: {e})")

    if alerts:
        print("\n".join(alerts))
        print("--- 참고: " + " · ".join(info))
    else:
        print("OK 트리거 없음 | " + " · ".join(info))


if __name__ == "__main__":
    main()
