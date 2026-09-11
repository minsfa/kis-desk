"""연금저축(구) ISA 재배치 — 2차 매수 시점 판단용 주간 모니터. 매매 없음, 알림만.

배경 (2026-09-11 확정):
  ISA 해지 5,000만 + 구 계좌 예수금 → 미국 ETF 재배치
  1차 2026-09-14(월): S&P500 1,100만 + 미국배당다우존스 1,000만
  2차 약 한 달 뒤: 같은 금액. **시점은 고정이 아니라 이 스크립트로 주간 판단**
  중소형 quality 700만: 조건 충족 상품 확인 전까지 MMF 대기

주간에 보는 것 세 가지:
  1) 환율 — 1차 체결 환율 대비. 오르면 2차 불리, 내리면 유리
  2) 대상 ETF 가격 — 1차 대비. 지수 레벨 리스크
  3) 국내 증시 — KOSPI 레짐 (참고)

신호가 있을 때만 이모지로 시작, 평소 'OK'. thesis_watch.py 패턴 준수.
"""
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트

from src.kis.config import load_settings
from src.kis.client import KisClient
from src.kis.fundamentals import get_fundamentals

# ── 1차 매수 기준값 — 체결 후 실제 값으로 갱신할 것 ──────────────
# (None이면 "1차 미체결"로 표시하고 현재가만 보고)
BASE = {
    "date": None,          # "2026-09-14"
    "fx": None,            # 1차 체결일 원달러 (예: 1370.0)
    "360200": None,        # ACE 미국S&P500 체결 평단
    "458730": None,        # TIGER 미국배당다우존스 체결 평단
}

TARGETS = [
    ("ACE 미국S&P500", "360200", "S&P500 코어 — 2차 1,100만"),
    ("TIGER 미국배당다우존스", "458730", "배당 코어 — 2차 1,000만"),
]
REFERENCE = [
    ("TIGER 미국S&P500", "360750"),
    ("KODEX 미국배당다우존스", "489250"),
    ("KODEX 200 (국내 참고)", "069500"),
]

# 2차 집행 판단 가이드 (하드룰 아님 — 사람이 판단)
FX_CHEAP = -2.0      # 1차 대비 환율 -2% 이하 = 2차에 유리
FX_RICH = +3.0       # +3% 이상 = 2차 분할 더 쪼개는 것 검토
ETF_DIP = -5.0       # 1차 대비 -5% 이하 = 2차 앞당기기 검토
ETF_RUN = +7.0       # +7% 이상 = 서두르지 말 것


def _fx():
    """FRED DEXKOUS 원달러. tools/fx_check.py 와 같은 소스."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from fx_check import _fred_key, latest_usdkrw  # type: ignore
        key = _fred_key()
        if not key:
            return None
        rate, _asof = latest_usdkrw(key)   # (환율, 날짜) 순서 — 날짜가 뒤
        return float(rate) if rate else None
    except Exception:
        return None


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
    today = date.today()
    c = KisClient(load_settings("prod"))
    alerts, lines = [], []

    # ── 환율
    fx = _fx()
    if fx is None:
        lines.append("환율 조회 실패 (FRED)")
    elif BASE["fx"]:
        gap = (fx / BASE["fx"] - 1) * 100
        lines.append(f"환율 {fx:,.1f}원 (1차 {BASE['fx']:,.1f} 대비 {gap:+.1f}%)")
        if gap <= FX_CHEAP:
            alerts.append(f"💱 환율 {fx:,.1f}원 — 1차 대비 {gap:+.1f}%. 2차 매수에 유리, 앞당기기 검토")
        elif gap >= FX_RICH:
            alerts.append(f"💱 환율 {fx:,.1f}원 — 1차 대비 {gap:+.1f}%. 2차를 더 잘게 쪼개는 것 검토")
    else:
        lines.append(f"환율 {fx:,.1f}원 (1차 기준값 미설정)")

    # ── 대상 ETF
    for nm, code, memo in TARGETS:
        px, chg = _px(c, code)
        time.sleep(0.3)
        if px is None:
            lines.append(f"{nm} 조회 실패")
            continue
        base = BASE.get(code)
        if base:
            gap = (px / base - 1) * 100
            lines.append(f"{nm} {px:,.0f} (1차 {base:,.0f} 대비 {gap:+.1f}%, 당일 {chg:+.1f}%)")
            if gap <= ETF_DIP:
                alerts.append(f"📉 {nm} 1차 대비 {gap:+.1f}% — {memo} 앞당기기 검토")
            elif gap >= ETF_RUN:
                alerts.append(f"📈 {nm} 1차 대비 {gap:+.1f}% — 서두르지 말 것. 예정대로 또는 지연")
        else:
            lines.append(f"{nm} {px:,.0f} (당일 {chg:+.1f}%) — 1차 기준값 미설정")

    # ── 참고 지표
    ref = []
    for nm, code in REFERENCE:
        px, chg = _px(c, code)
        time.sleep(0.3)
        if px:
            ref.append(f"{nm} {px:,.0f}({chg:+.1f}%)")

    # ── 출력
    head = f"■ 연금 재배치 주간 점검 ({today:%m/%d})"
    if not BASE["date"]:
        head += "  ⚠️ 1차 미체결 — 체결 후 BASE 갱신 필요"
    out = [head]
    if alerts:
        out += alerts
    out += ["", "· " + "\n· ".join(lines)]
    if ref:
        out += ["", "참고: " + " / ".join(ref)]
    if not alerts:
        out.insert(0, "OK 트리거 없음")
    out.append("\n※ 하드룰 아님. 2차 집행은 사람이 판단, 매수는 지시 후 실행.")
    print("\n".join(out))


if __name__ == "__main__":
    main()
