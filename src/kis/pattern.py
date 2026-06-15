"""데이터 기반 단가 제안 — 과거 일봉으로 '급등→익일 눌림→반등' 통계를 뽑는다.

가설(HYPOTHESES C): 전날 +surge%↑ 급등하면, 다음날 일정 폭 눌렸다가 반등하는 경향.
과거 모든 급등일 D에 대해 D+1의 저가(=오늘 종가 대비 눌림 깊이)와 저가→종가 반등을 모아
백분위로 요약 → '어느 가격에 걸면 체결 확률 얼마, 반등 기대 얼마'를 제안한다.
순수 과거 통계일 뿐 미래 보장 아님. 표본 부족(<8건)이면 None.
"""
from __future__ import annotations
import csv
from statistics import median

from .config import PROJECT_ROOT
from .tick import round_tick

DAILY = PROJECT_ROOT / "data" / "daily"


def _load(code: str):
    p = DAILY / f"{code}.csv"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out.append({k: float(r[k]) for k in ("open", "high", "low", "close")})
            except Exception:
                pass
    return out


def _pct(xs, q):
    if not xs:
        return 0.0
    s = sorted(xs); i = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[i]


def surge_pullback(code: str, surge_min: float = 5.0) -> dict | None:
    """급등 다음날의 눌림(저가 vs 전날종가)·반등(저가→종가) 분포."""
    rows = _load(code)
    if len(rows) < 30:
        return None
    dips, gaps, bounces, d2s = [], [], [], []
    for i in range(1, len(rows) - 1):
        prev, today, nxt = rows[i - 1]["close"], rows[i]["close"], rows[i + 1]
        if prev <= 0 or (today / prev - 1) * 100 < surge_min:
            continue
        dips.append((nxt["low"] / today - 1) * 100)         # 익일 저가 vs 오늘 종가(보통 음수)
        gaps.append((nxt["open"] / today - 1) * 100)        # 익일 시가 갭
        if nxt["low"] > 0:
            bounces.append((nxt["close"] / nxt["low"] - 1) * 100)   # 저가→종가 반등
            hi2 = nxt["high"] if i + 2 >= len(rows) else max(nxt["high"], rows[i + 2]["high"])
            d2s.append((hi2 / nxt["low"] - 1) * 100)        # 저가→(D+1·D+2 고가) 반등
    n = len(dips)
    if n < 8:
        return None
    return {
        "n": n,
        "dip_p50": median(dips), "dip_p25": _pct(dips, 0.25), "dip_p75": _pct(dips, 0.75),
        "gap_med": median(gaps),
        "bounce_med": median(bounces) if bounces else 0.0,
        "d2_med": median(d2s) if d2s else 0.0,
        "down_rate": sum(1 for d in dips if d < 0) / n * 100,   # 익일 종가아래로 내려간 비율
    }


def suggest_levels(code: str, close: float, surge_min: float = 5.0, target_pct: float = 1.5) -> dict | None:
    """오늘 종가 close 기준, 데이터로 본 2단계 진입가 + 목표가 + 체결확률 제안."""
    s = surge_pullback(code, surge_min)
    if not s or close <= 0:
        return None
    # dip 분포는 음수. p50(얕은 눌림)·p25(깊은 눌림) 두 단계. 매수는 호가단위 내림, 목표는 올림.
    e1 = round_tick(close * (1 + s["dip_p50"] / 100), up=False)
    e2 = round_tick(close * (1 + s["dip_p25"] / 100), up=False)
    bounce = max(s["bounce_med"], target_pct)        # 기대 반등(최소 목표% 보장)
    return {
        "stats": s,
        "levels": [
            {"price": e1, "target": round_tick(e1 * (1 + bounce / 100), up=True), "fill_prob": 50, "tag": "얕은눌림(p50)"},
            {"price": e2, "target": round_tick(e2 * (1 + bounce / 100), up=True), "fill_prob": 25, "tag": "깊은눌림(p25)"},
        ],
        "note": (f"표본 {s['n']}건 · 익일저가 중앙 {s['dip_p50']:+.1f}%/하위25% {s['dip_p25']:+.1f}% "
                 f"· 저가→종가 반등 중앙 {s['bounce_med']:+.1f}% · 익일하락률 {s['down_rate']:.0f}%"),
    }
