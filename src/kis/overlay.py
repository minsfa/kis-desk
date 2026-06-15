"""수급·공시 오버레이(Phase C) — 지주사 바스켓에 외국인 순매수 + 자사주/구조변화 이벤트.

리포트가 말한 "외국인 매수 + 자사주 소각" 콤보를 실측으로 확인하는 축.
- 수급: KIS inquire-investor 30일창 외국인 순매수(금액 억원). 매도→매수 전환·강도.
- 공시: DART 최근 N일 자기주식(취득/소각)·공개매수·분할·합병 이벤트.

밸류에이션(Phase A PBR / Phase B NAV)과 독립. 합산 스코어는 Phase D(scorecard)에서.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

from .client import KisClient
from . import investor, dart
from .fundamentals import HOLDCO_BASKET

KST = timezone(timedelta(hours=9))

# 공시 제목 → 이벤트 태그(지주사 재평가 트리거 한정). 위에서부터 우선(먼저 맞는 것 채택).
# 주의: '주식소각결정'은 '자기주식' 없이도 소각이다. '자기주식처분'은 환원 아님(중립, 별도 태그).
_EVENT_RULES = [
    ("자사주소각", lambda t: "소각" in t),                      # '주식소각결정' 포함
    ("자사주취득", lambda t: "자기주식취득" in t),              # 취득/취득신탁 (처분 제외)
    ("자사주처분", lambda t: "자기주식처분" in t),              # 처분=환원 아님(중립)
    ("공개매수",   lambda t: "공개매수" in t),
    ("인적분할",   lambda t: "인적분할" in t),
    ("분할",       lambda t: "분할" in t),
    ("합병",       lambda t: "합병" in t),
]


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def foreign_flow(c: KisClient, code: str) -> dict:
    """외국인 순매수 30일창 요약. 금액은 억원(frgn_ntby_tr_pbmn=백만원 → /100)."""
    rows = [r for r in investor.fetch(c, code) if r.get("stck_bsop_date")]
    rows.sort(key=lambda r: r["stck_bsop_date"])
    qty = [_num(r.get("frgn_ntby_qty")) for r in rows]
    val = [_num(r.get("frgn_ntby_tr_pbmn")) / 100.0 for r in rows]  # → 억원
    n = len(rows)
    rec5 = sum(val[-5:]) if n >= 5 else sum(val)
    pri5 = sum(val[-10:-5]) if n >= 10 else 0.0
    cum = sum(val)
    # 매수전환: 직전5일 순매도 → 최근5일 순매수
    turn = (rec5 > 0 and pri5 < 0)
    # 강도: 30일 누적이 +면서 최근5일도 +
    buy = (cum > 0 and rec5 > 0)
    return {
        "code": code, "days": n,
        "rec5_val": round(rec5), "pri5_val": round(pri5), "cum_val": round(cum),
        "turn": turn, "net_buy": buy,
        "last_date": rows[-1]["stck_bsop_date"] if rows else None,
    }


def events(code: str, days: int = 120) -> list[dict]:
    """최근 N일 지주사 재평가 트리거 공시(자사주/공개매수/분할/합병). 최신순."""
    out = []
    for d in dart.recent_disclosures(code, days=days):
        title = d.get("title", "")
        for tag, pred in _EVENT_RULES:
            if pred(title):
                out.append({"date": d["date"], "tag": tag, "title": title})
                break
    return out


def overlay(c: KisClient, codes: dict[str, str] | None = None, days: int = 120) -> dict:
    codes = codes or HOLDCO_BASKET
    rows = []
    for code, name in codes.items():
        try:
            ff = foreign_flow(c, code)
        except Exception as e:
            ff = {"code": code, "error": str(e)}
        try:
            ev = events(code, days=days)
        except Exception:
            ev = []
        tags = sorted({e["tag"] for e in ev})
        rows.append({"code": code, "name": name, "flow": ff, "events": ev, "tags": tags})
    return {"date": f"{datetime.now(KST):%Y-%m-%d}", "rows": rows, "days": days}


def summary(c: KisClient, codes: dict[str, str] | None = None, days: int = 120) -> str:
    ov = overlay(c, codes, days)
    lines = [f"📡 지주사 수급·공시 오버레이 — {ov['date']} "
             f"(외국인 30일 순매수 + 최근 {days}일 트리거 공시)"]
    lines.append("  종목(코드)  외인5일/30일(억)  전환  공시이벤트")
    # 정렬: 매수전환 > 순매수 > 나머지, 그 안에서 30일 누적 큰 순
    def _key(r):
        f = r["flow"]
        return (not f.get("turn"), not f.get("net_buy"), -(f.get("cum_val") or -9e9))
    for r in sorted(ov["rows"], key=_key):
        f = r["flow"]
        if f.get("error"):
            lines.append(f"  {r['name']}({r['code']}): 수급 오류")
            continue
        rec, cum = f.get("rec5_val", 0), f.get("cum_val", 0)
        turn = "⚡전환" if f.get("turn") else ("＋매수" if f.get("net_buy") else "－매도")
        evs = ""
        if r["tags"]:
            recent_ev = r["events"][0]
            evs = f"  📄{'·'.join(r['tags'])}(최근 {recent_ev['date']})"
        lines.append(f"  {r['name']}({r['code']})  {rec:+,}/{cum:+,}  {turn}{evs}")
    n_turn = sum(1 for r in ov["rows"] if r["flow"].get("turn"))
    n_ev = sum(1 for r in ov["rows"] if r["tags"])
    lines.append(f"\n⚡외국인 매수전환 {n_turn}종목 · 📄트리거 공시 {n_ev}종목. "
                 "(밸류에이션 결합은 Phase D scorecard)")
    return "\n".join(lines)
