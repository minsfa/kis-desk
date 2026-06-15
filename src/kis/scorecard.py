"""지주사 종합 스코어카드(Phase D) — A(PBR)·B(NAV할인)·C(수급/공시) 결합.

리포트가 말한 최강 조합 "저평가 + 외국인 순매수 + 자사주 이벤트"를 투명한 가중점수로 랭킹.
점수 구성(전부 명시·튜닝가능):
  · 가치(NAV)   : 양(+)할인율만 가점(보수적 하한). 프리미엄(고cov)은 감점, 저cov는 0(불확실).
  · 가치(PBR)   : 0<PBR≤0.5 동조 가점.
  · 수급(외국인) : 매수전환 > 순매수 가점, 지속매도 감점, 대량매도 강감점.
  · 공시(트리거) : 자사주소각/공개매수/자사주취득/인적분할 등 가점(합산·상한).

밸류에이션이 싸도 수급이 이탈(SK스퀘어)하면 점수가 깎이도록 설계 — 단일 지표 함정 방지.
openclaw cron 이 마감 후 호출해 텔레그램 보고 가능(report 텍스트 반환).
"""
from __future__ import annotations
import csv
from datetime import datetime, timedelta, timezone

from .client import KisClient
from .config import PROJECT_ROOT
from . import nav, overlay, quality
from .fundamentals import get_fundamentals, HOLDCO_BASKET, _eok_to_human

KST = timezone(timedelta(hours=9))
HOLDCO_DIR = PROJECT_ROOT / "data" / "holdco"

# ---- 가중치(튜닝 포인트) ----
W_NAV = 60          # 할인율 1.0당 점수(0.4 → 24점)
W_PBR_AGREE = 8     # PBR≤0.5 동조 가점
W_PREMIUM = 40      # 프리미엄(음수,고cov) 감점 계수
EVENT_PTS = {"자사주소각": 15, "공개매수": 12, "자사주취득": 8,
             "인적분할": 8, "분할": 4, "합병": 2, "자사주처분": 0}  # 처분=환원 아님
EVENT_CAP = 20
FLOW_TURN, FLOW_BUY = 12, 6
FLOW_SELL, FLOW_DUMP = -10, -18    # 지속매도 / 대량매도(누적·최근 모두 큰 음수)
DUMP_EOK = -2000                   # 30일 누적 외인순매수 이 이하면 대량매도


def _score_one(code: str, name: str, stakes: dict, c: KisClient,
               capcache: dict) -> dict:
    f = get_fundamentals(c, code)
    capcache[code] = f.get("mktcap_eok")          # NAV 계산에서 self-cap 재사용
    navr = nav.compute_nav(c, code, stakes, capcache) or {}
    ff = overlay.foreign_flow(c, code)
    evs = overlay.events(code, days=120)
    tags = sorted({e["tag"] for e in evs})
    q = quality.get_quality(c, code)

    disc = navr.get("discount")
    cov = navr.get("coverage")
    pbr = f.get("pbr")

    # 가치 점수
    val_pts = 0.0
    val_note = ""
    if disc is None:
        val_note = "NAV불가"
    elif disc >= 0:
        val_pts = disc * W_NAV
        val_note = f"NAV할인 {disc*100:.0f}%"
    elif cov is not None and cov >= 0.6:
        val_pts = disc * W_PREMIUM        # 진짜 프리미엄 → 감점(음수)
        val_note = f"프리미엄 {disc*100:.0f}%(고cov)"
    else:
        val_note = f"음수(저cov,불확실)"   # 0점
    if pbr and 0 < pbr <= 0.5:
        val_pts += W_PBR_AGREE
        val_note += " +PBR동조"

    # 수급 점수
    flow_pts = 0.0
    if ff.get("turn"):
        flow_pts = FLOW_TURN; flow_note = "외인전환"
    elif ff.get("net_buy"):
        flow_pts = FLOW_BUY; flow_note = "외인순매수"
    elif (ff.get("cum_val") or 0) <= DUMP_EOK and (ff.get("rec5_val") or 0) < 0:
        flow_pts = FLOW_DUMP; flow_note = "외인대량매도"
    elif (ff.get("rec5_val") or 0) < 0 and (ff.get("cum_val") or 0) < 0:
        flow_pts = FLOW_SELL; flow_note = "외인지속매도"
    else:
        flow_note = "중립"

    # 공시 점수(합산 상한)
    ev_pts = min(EVENT_CAP, sum(EVENT_PTS.get(t, 0) for t in tags))

    # 사업 질·재무 점수(주로 감점) + 가치함정 플래그
    qual_pts, qual_notes = quality.quality_points(q)
    trap = quality.is_value_trap(disc, pbr, q)

    score = round(val_pts + flow_pts + ev_pts + qual_pts, 1)
    return {
        "code": code, "name": name, "score": score,
        "pbr": pbr, "discount": disc, "coverage": cov,
        "mktcap_eok": f.get("mktcap_eok"), "nav_eok": navr.get("nav_eok"),
        "val_pts": round(val_pts, 1), "flow_pts": flow_pts, "ev_pts": ev_pts,
        "qual_pts": qual_pts, "qual_notes": qual_notes, "trap": trap,
        "roe": q.get("roe"), "borrow_dep": q.get("borrow_dep"), "op_grw": q.get("op_grw"),
        "val_note": val_note, "flow_note": flow_note,
        "rec5_val": ff.get("rec5_val"), "cum_val": ff.get("cum_val"),
        "tags": tags, "recent_event": (evs[0] if evs else None),
        "n_subs": navr.get("n_subs"), "n_unmatched": navr.get("n_unmatched"),
    }


def build(c: KisClient, codes: dict[str, str] | None = None) -> dict:
    codes = codes or HOLDCO_BASKET
    stakes = nav.load_stakes()
    capcache: dict = {}
    rows = []
    for code, name in codes.items():
        try:
            rows.append(_score_one(code, name, stakes, c, capcache))
        except Exception as e:
            rows.append({"code": code, "name": name, "score": -999, "error": str(e)})
    rows.sort(key=lambda r: -r.get("score", -999))
    day = f"{datetime.now(KST):%Y-%m-%d}"
    HOLDCO_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = HOLDCO_DIR / f"scorecard_{day}.csv"
    cols = ["code", "name", "score", "pbr", "discount", "coverage",
            "val_pts", "flow_pts", "ev_pts", "qual_pts", "trap",
            "roe", "borrow_dep", "op_grw", "rec5_val", "cum_val"]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return {"date": day, "rows": rows, "csv": str(csv_path),
            "stakes_as_of": stakes.get("as_of")}


def report(c: KisClient, codes: dict[str, str] | None = None, top: int = 12) -> str:
    """텔레그램/openclaw 보고용 종합 랭킹."""
    sc = build(c, codes)
    rows = [r for r in sc["rows"] if not r.get("error")]
    lines = [f"🏛️ 지주사 저평가 종합 스코어카드 — {sc['date']}",
             "점수 = NAV할인+PBR + 외국인수급 + 자사주공시 − 사업질감점(저ROE·적자·고차입). 🪤=가치함정"]
    lines.append(f"\n🏆 TOP {min(top, len(rows))}")
    for i, r in enumerate(rows[:top], 1):
        pbr = r.get("pbr"); pbr_s = f"{pbr:.2f}" if pbr and pbr > 0 else "—"
        d = r.get("discount")
        d_s = f"{d*100:+.0f}%" if d is not None else "—"
        roe = r.get("roe"); roe_s = f"{roe:.1f}" if roe is not None else "—"
        cum = r.get("cum_val") or 0
        ev = "·".join(r.get("tags") or []) or "—"
        trap = " 🪤" if r.get("trap") else ""
        qn = ("/" + "·".join(r["qual_notes"])) if r.get("qual_notes") else ""
        lines.append(
            f"{i:>2}. {r['name']}({r['code']})  {r['score']:>5}{trap} "
            f"[NAV {d_s}/PBR {pbr_s}/ROE {roe_s}/외인30d {cum:+,}억/공시 {ev}{qn}]")
    # 코멘트: 3중 충족(질 양호) / 가치함정
    combo = [r for r in rows if (r.get("discount") or 0) >= 0.4
             and r.get("flow_pts", 0) > 0 and r.get("ev_pts", 0) > 0 and not r.get("trap")]
    if combo:
        lines.append("\n✅ 3중 충족 + 사업질 양호: "
                     + ", ".join(f"{r['name']}" for r in combo[:6]))
    traps = [r for r in rows if r.get("trap")]
    if traps:
        lines.append("🪤 가치함정 주의(자산은 싸나 본업 부실): "
                     + ", ".join(f"{r['name']}({'·'.join(r['qual_notes'][:2])})" for r in traps[:8]))
    dumps = [r for r in rows if (r.get("discount") or 0) >= 0.4
             and (r.get("cum_val") or 0) <= DUMP_EOK]
    if dumps:
        lines.append("🚩 수급이탈(싸지만 외인 대량매도): "
                     + ", ".join(f"{r['name']}" for r in dumps[:6]))
    lines.append(f"\n💾 {sc['csv']}  (지분율 {sc.get('stakes_as_of')} DART)")
    return "\n".join(lines)
