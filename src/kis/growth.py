"""성장 렌즈(ARK 프레임, 정량) — 수치화 가능한 지표 + 해석 라벨.

값이 아니라 '의미'를 출력한다(고성장/견조/저성장, 매력/부담 등). 정성(TAM·해자·경영진·
라이트의 법칙)은 도구가 흉내내지 않는다 → 사람이 별도 딥리서치. docs/METRICS_FRAMEWORK.md 참조.

데이터: KIS 손익계산서(FHKST66430200, 연간 FY=12월 행)에서 매출·영익·순익 시계열,
수익성비율 ROE, 시세에서 시총·PER. 매출원가·감가·R&D는 KIS가 부정확(99.99)이라 제외(거짓정밀 금지).
"""
from __future__ import annotations

from .client import KisClient
from .fundamentals import get_fundamentals
from .logging_util import log_event

INC_PATH = "/uapi/domestic-stock/v1/finance/income-statement"   # FHKST66430200
PRF_PATH = "/uapi/domestic-stock/v1/finance/profit-ratio"       # FHKST66430400


def _f(v):
    try:
        x = float(v)
        return None if x == 99.99 else x   # 99.99 = KIS 결측 플래그
    except (TypeError, ValueError):
        return None


def _cagr(first: float, last: float, years: int) -> float | None:
    if not first or first <= 0 or not last or last <= 0 or years <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def _annual_series(c: KisClient, code: str) -> list[dict]:
    """연간(FY=12월) 매출·영익·순익 시계열, 최신→과거."""
    d = c.get(INC_PATH, "FHKST66430200",
              {"FID_DIV_CLS_CODE": "1", "fid_cond_mrkt_div_code": "J", "fid_input_iscd": code})
    rows = []
    for r in (d.get("output") or []):
        ym = r.get("stac_yymm", "")
        if ym.endswith("12"):
            rows.append({"yy": ym[:4],
                         "rev": _f(r.get("sale_account")),
                         "op": _f(r.get("bsop_prti")),
                         "ni": _f(r.get("thtr_ntin"))})
    rows.sort(key=lambda x: x["yy"], reverse=True)
    return rows


def metrics(c: KisClient, code: str) -> dict:
    f = get_fundamentals(c, code)
    cap = f.get("mktcap_eok")
    per = f.get("per")
    fy = _annual_series(c, code)
    out = {"code": code, "price": f.get("price"), "mktcap_eok": cap, "per": per,
           "n_years": len(fy)}
    if len(fy) < 2:
        out["error"] = "연간 데이터 부족"
        return out

    rev = [r["rev"] for r in fy if r["rev"]]
    latest, oldest = fy[0], fy[-1]
    n = len(fy) - 1
    # 매출 CAGR (전체 가용연수 + 3년)
    out["rev_cagr_all"] = _cagr(oldest["rev"], latest["rev"], n)
    if len(fy) >= 4:
        out["rev_cagr_3y"] = _cagr(fy[3]["rev"], fy[0]["rev"], 3)
    else:
        out["rev_cagr_3y"] = out["rev_cagr_all"]
    # 영업이익 CAGR(성장 대용, 순익보다 안정)
    if latest["op"] and oldest["op"]:
        out["op_cagr_all"] = _cagr(oldest["op"], latest["op"], n)
    # 마진
    out["op_margin"] = (latest["op"] / latest["rev"]) if (latest["op"] and latest["rev"]) else None
    prev = fy[1]
    prev_margin = (prev["op"] / prev["rev"]) if (prev["op"] and prev["rev"]) else None
    out["op_margin_prev"] = prev_margin
    out["margin_trend"] = (None if (out["op_margin"] is None or prev_margin is None)
                           else ("확대" if out["op_margin"] > prev_margin else "축소"))
    out["latest_ni"] = latest["ni"]
    out["latest_rev"] = latest["rev"]
    # PSR / 성장조정 PSR / PEG
    out["psr"] = (cap / latest["rev"]) if (cap and latest["rev"]) else None
    g = out.get("rev_cagr_3y")
    out["psr_growth_adj"] = (out["psr"] / (g * 100)) if (out["psr"] and g and g > 0) else None
    og = out.get("op_cagr_all")
    out["peg"] = (per / (og * 100)) if (per and per > 0 and og and og > 0) else None
    log_event("growth", code=code, rev_cagr_3y=out.get("rev_cagr_3y"), psr=out.get("psr"))
    return out


def hurdle_grid(c: KisClient, code: str, m: dict | None = None) -> dict:
    """ARK식 5년 15% CAGR 허들 + 멀티플 컴프레션 민감도.

    미래순익 = 현재순익×(1+g)^5, 미래시총 = 미래순익×출구PER → 현재시총 대비 5년 연복리.
    성장률 g(매출/영익 추정)와 출구배수를 격자로 돌려 ≥15% 셀을 표시.
    """
    m = m or metrics(c, code)
    cap = m.get("mktcap_eok"); ni = m.get("latest_ni")
    res = {"ok": False}
    if not (cap and ni and ni > 0):
        res["note"] = "순이익<0 또는 데이터부족 → 이익기반 허들 계산불가(매출기반 정성판단)"
        return res
    base_g = m.get("op_cagr_all") or m.get("rev_cagr_3y") or 0.10
    g_list = [0.10, 0.15, 0.20, round(base_g, 2)] if base_g else [0.10, 0.15, 0.20]
    g_list = sorted(set(round(x, 2) for x in g_list if x and x > 0))
    cur_per = m.get("per") or 0
    exit_list = sorted(set(int(x) for x in [10, 15, 20, min(cur_per, 25) if cur_per else 20] if x and x > 0))
    grid = []
    for g in g_list:
        row = {"g": g, "cells": []}
        for ex in exit_list:
            fut_cap = ni * (1 + g) ** 5 * ex
            cagr5 = (fut_cap / cap) ** (1 / 5) - 1 if cap > 0 else None
            row["cells"].append({"exit_per": ex, "cagr": cagr5, "pass": (cagr5 is not None and cagr5 >= 0.15)})
        grid.append(row)
    res.update({"ok": True, "base_g": base_g, "cur_per": cur_per, "grid": grid})
    return res


# ---- 해석 라벨 ----
def _lab_cagr(g):
    if g is None: return "—"
    p = g * 100
    return f"{p:.0f}% ({'고성장' if p >= 20 else '견조' if p >= 10 else '저성장'})"

def _lab_psr_adj(v):
    if v is None: return "—"
    return f"{v:.1f} ({'매력' if v < 1 else '보통' if v < 2 else '부담'})"

def _lab_peg(v):
    if v is None: return "—"
    return f"{v:.2f} ({'저평가' if v < 1 else '적정' if v < 2 else '부담'})"


def summary(c: KisClient, code: str) -> str:
    m = metrics(c, code)
    name = ""
    L = [f"🟧 성장 렌즈 — {code} (정량만; 정성은 별도 딥리서치)"]
    if m.get("error"):
        return "\n".join(L + [f"  {m['error']}"])
    om = m.get("op_margin")
    om_s = f"{om*100:.1f}%" if om is not None else "—"
    psr = m.get("psr")
    psr_s = f"{psr:.1f}" if psr else "—"
    L.append(f"  매출 CAGR(3년) {_lab_cagr(m.get('rev_cagr_3y'))} · 전체{m['n_years']}년 {_lab_cagr(m.get('rev_cagr_all'))}")
    L.append(f"  영업이익률 {om_s} ({m.get('margin_trend') or '—'}) · 영익CAGR {_lab_cagr(m.get('op_cagr_all'))}")
    L.append(f"  PSR {psr_s} · 성장조정PSR {_lab_psr_adj(m.get('psr_growth_adj'))} · PEG {_lab_peg(m.get('peg'))}")
    # 15% 허들 그리드
    h = hurdle_grid(c, code, m)
    if not h.get("ok"):
        L.append(f"  ⚠️ 15% 허들: {h.get('note')}")
    else:
        exits = [c2["exit_per"] for c2 in h["grid"][0]["cells"]]
        L.append(f"\n  📐 5년 15%CAGR 허들 (행=이익성장가정, 열=출구PER, ✅=통과)")
        L.append("     성장\\PER  " + "  ".join(f"{e:>4}x" for e in exits))
        for row in h["grid"]:
            cells = "  ".join((f"{c2['cagr']*100:>3.0f}%" + ("✅" if c2["pass"] else "  ")) if c2['cagr'] is not None else "  — " for c2 in row["cells"])
            L.append(f"     {row['g']*100:>4.0f}%    {cells}")
        L.append(f"  (현재 PER {h['cur_per']:.0f} · 역사 이익성장 {(_lab_cagr(h['base_g']))})")
    return "\n".join(L)
