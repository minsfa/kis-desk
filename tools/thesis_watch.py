"""2026-09-11 리서치 가설 주간 체크 — 크론용. 매매 없음, 알림만.

22종목 전수조사(2026-09-11)에서 세운 가설을 매주 재검증한다.
가설은 셋으로 나뉜다:
  1) 가격 게이트  — 진입/익절 트리거. 매주 도달 여부 판정
  2) 이벤트 캘린더 — 검증일이 정해진 것. D-day 카운트
  3) 반증 감시     — 공시가 뜨면 결론이 뒤집히는 것

신호가 있을 때만 이모지로 시작, 평소 'OK'. sep_cleanup_watch.py 패턴 준수.
"""
import os
import sys
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트

import csv
from datetime import timedelta
from pathlib import Path

from src.kis.config import load_settings
from src.kis.client import KisClient
from src.kis.fundamentals import get_fundamentals
from src.kis.daily import download

DAILY_DIR = Path(__file__).resolve().parent.parent / "data" / "daily"

# ── 1) 가격 게이트 ────────────────────────────────────────────────
# (이름, 코드, 종류, 기준, 메모)  종류: ma=이동평균선, px=절대가, pbr=PBR
GATES = [
    ("삼성전자", "005930", "px", 290_000, "위: 62→50주 부분익절 (반도체 42.8%→28.4% 3단계 중 3단계)"),
    ("삼성전자", "005930", "ma", 60, "위: 섹터 방향타 — 상관 0.59~0.63, 이게 못 넘으면 삼성전기도 못 넘음"),
    ("SK하이닉스", "000660", "px", 2_000_000, "위: 5→3주 축소 시작"),
    ("SK하이닉스", "000660", "pbr", 12.0, "위: PBR 12 초과 = 축소 가속"),
    ("삼성전기", "009150", "ma", 60, "위: 신규매수 게이트 1 (60일선 위 매수가 +6.16% vs 아래 +0.92%)"),
    ("원익IPS", "240810", "ma", 200, "아래: 추세전환 — 후보에서 제외"),
    ("대웅", "003090", "px", 15_410, "아래: 52주 저점 이탈 = 잔여분 즉시 정리"),
    ("현대차", "005380", "px", 480_000, "위: 4주 중 2주 축소 검토"),
    ("삼성물산", "028260", "px", 330_000, "아래: NAV 할인 45%+ = 추가매수 조건 2 충족"),
]

# ── 2) 이벤트 캘린더 ──────────────────────────────────────────────
EVENTS = [
    (date(2026, 9, 17), "대웅 메디톡스 5,001억 증액 후 첫 변론 — 결과 보고 129주 중 절반 정리"),
    (date(2026, 9, 22), "1차 강제매도 — 새빗켐 59주 · 아난티 4주"),
    (date(2026, 9, 29), "2차 강제매도 — 제이엔케이 476주(절반) · 카카오 23주 · 아모레 3주"),
    (date(2026, 9, 30), "구리 ETN 정리 권고 마감 (만기 10/26 전 유동성 확보)"),
    (date(2026, 10, 8), "삼성전자 3Q 잠정실적(추정) — 반도체 섹터 첫 관문"),
    (date(2026, 10, 26), "⚠️ 구리 ETN Q570071 최종거래일 — 이날 넘기면 자동 만기상환"),
    (date(2026, 10, 30), "삼성전자 확정실적 + 배당 이사회(30조 확정 여부) / 삼성전기 3Q(2H 순이익 +63% 검증)"),
    (date(2026, 11, 5), "대웅 별건 형사 항소심 선고 — 유죄 유지 시 잔여분 정리"),
    (date(2026, 11, 18), "원익IPS 3Q — 2H 영업이익 +432%(OPM 7.6%→17.9%) 검증일"),
    (date(2026, 11, 16), "마니커F&G 3Q — LH 556.2억 처분이익·재투자·환원 확인 후 매도 판단"),
    (date(2026, 12, 17), "카카오 임시주총 (인적분할 승인 여부)"),
    (date(2026, 12, 29), "🚨 카카오 매도 하드 데드라인 — 12/30~1/26 매매거래정지"),
]

# ── 3) 반증 감시 (공시 키워드) ────────────────────────────────────
# (이름, 코드, 키워드들, 뜨면 어떻게 되는가)
DISCLOSURE_WATCH = [
    ("대웅", "003090", ("소각",), "자사주 소각 결의 = 축소 판정 근거 소멸 → 코어 복귀 검토"),
    ("제이엔케이글로벌", "126880", ("제3자배정", "유상증자", "신주발행"), "경영권 방어용 유증 = 절반 아닌 전량 즉시 매도"),
    ("SK하이닉스", "000660", ("주주환원", "자기주식", "배당"), "3Q 주주환원안 — 고정배당만이면 배당 논거 폐기"),
]
DART_DAYS = 10  # 주간 크론이므로 여유 있게


def _fund(c, code):
    for _ in range(3):
        try:
            f = get_fundamentals(c, code)
            if f.get("price"):
                return f
        except Exception:
            pass
        time.sleep(1.0)
    return None


def _closes(c, code, need):
    """data/daily/<code>.csv 종가 리스트(과거→최신). 없거나 오래되면 받아서 갱신."""
    path = DAILY_DIR / f"{code}.csv"
    stale = True
    if path.exists():
        try:
            rows = list(csv.DictReader(open(path)))
            last = max(r["date"] for r in rows)
            stale = (date.today() - date(int(last[:4]), int(last[4:6]), int(last[6:8]))).days > 5
        except Exception:
            rows, stale = [], True
    if stale:
        try:
            end = date.today()
            download(c, code, (end - timedelta(days=500)).strftime("%Y%m%d"), end.strftime("%Y%m%d"))
            rows = list(csv.DictReader(open(path)))
        except Exception:
            if not path.exists():
                return None
            rows = list(csv.DictReader(open(path)))
    rows.sort(key=lambda r: r["date"])
    closes = [int(r["close"]) for r in rows if r.get("close")]
    return closes if len(closes) >= need else None


def _ma(c, code, n):
    """최근 n일 종가 이동평균. 실패 시 None."""
    closes = _closes(c, code, n)
    return sum(closes[-n:]) / n if closes else None


def check_gates(c):
    hit, near, info = [], [], []
    for nm, code, kind, level, memo in GATES:
        f = _fund(c, code)
        time.sleep(0.3)
        if not f:
            info.append(f"{nm} 조회실패")
            continue
        px = float(f["price"])

        if kind == "pbr":
            cur, target, label = float(f.get("pbr") or 0), level, "PBR"
            if not cur:
                continue
            gap = (cur / target - 1) * 100
            line = f"{nm} PBR {cur:.2f} (기준 {target:.1f}, {gap:+.1f}%)"
            if cur >= target:
                hit.append(f"🎯 {line} 돌파 — {memo}")
            elif gap >= -10:
                near.append(line + " ← 근접")
            else:
                info.append(line)
            continue

        if kind == "ma":
            target = _ma(c, code, level)
            time.sleep(0.3)
            if target is None:
                info.append(f"{nm} {level}일선 계산실패")
                continue
            label = f"{level}일선"
        else:
            target = float(level)
            label = "기준가"

        gap = (px / target - 1) * 100
        line = f"{nm} {px:,.0f} vs {label} {target:,.0f} ({gap:+.1f}%)"
        above = memo.startswith("위")
        if (above and px >= target) or (not above and px <= target):
            hit.append(f"🎯 {line} 도달 — {memo}")
        elif abs(gap) <= 3:
            near.append(line + " ← 근접")
        else:
            info.append(line)
    return hit, near, info


def check_events(today):
    soon, passed = [], []
    for d, desc in EVENTS:
        dd = (d - today).days
        if dd < 0:
            if dd >= -7:
                passed.append(f"✅ {d:%m/%d} 경과 — {desc}")
            continue
        if dd <= 14:
            soon.append(f"D-{dd:<3} {d:%m/%d}  {desc}")
    return soon, passed


def check_disclosures(c):
    try:
        from src.kis.dart import recent_disclosures
    except Exception:
        return [], "dart 모듈 없음"
    out = []
    for nm, code, keywords, effect in DISCLOSURE_WATCH:
        try:
            items = recent_disclosures(code, days=DART_DAYS)
        except Exception:
            continue
        for it in items:
            title = it.get("title") or ""
            if any(k in title for k in keywords):
                out.append(f"📢 {nm} 공시 「{title}」 — {effect}")
    return out, None


def main():
    today = date.today()
    c = KisClient(load_settings("prod"))

    hit, near, info = check_gates(c)
    soon, passed = check_events(today)
    disc, disc_err = check_disclosures(c)

    urgent = hit + disc
    lines = []

    if urgent:
        lines.append(f"■ 가설 트리거 {len(urgent)}건 ({today:%m/%d})")
        lines += urgent
    if soon:
        lines.append("\n■ 다가오는 검증일 (2주 내)")
        lines += soon
    if passed:
        lines.append("\n■ 지난주 경과")
        lines += passed
    if near:
        lines.append("\n■ 게이트 근접 (±3%)")
        lines += near

    if not urgent and not soon:
        head = f"OK 트리거 없음 ({today:%m/%d})"
        print(head + ("\n" + "\n".join(lines) if lines else ""))
        if info:
            print("--- " + " · ".join(info))
        return

    print("\n".join(lines))
    if info:
        print("\n--- 참고: " + " · ".join(info))
    if disc_err:
        print(f"--- 공시감시 미작동: {disc_err}")


if __name__ == "__main__":
    main()
