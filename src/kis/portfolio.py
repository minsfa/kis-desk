"""포트폴리오 모니터링(읽기 전용) — 보유 종목을 우리 렌즈로 점검·제안.

자동매매와 분리된 '모니터링 모드'. 잔고를 읽어 보유 각 종목을 가치/사업질/수급/공시로
분석하고 한 줄 제안(들고가/비중축소/점검 등)을 붙인다. **주문은 절대 하지 않는다.**

계좌는 .env(CANO)로 지정된 것을 그대로 본다(=현재 계좌). 두 계좌(모니터링/자동매매)
분리 운용은 추후 계좌번호 파라미터를 받게 확장.
"""
from __future__ import annotations

from .client import KisClient
from .fundamentals import get_fundamentals
from . import quality as Q, overlay as O, nav as N
from .market import get_balance


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def analyze_holding(c: KisClient, code: str, name: str, stakes: dict, capcache: dict) -> dict:
    """보유 1종목 분석: 밸류·질·수급·공시 + 한 줄 제안."""
    out = {"code": code, "name": name}
    try:
        f = get_fundamentals(c, code)
    except Exception as e:
        out["error"] = f"시세조회 실패({e})"
        return out
    pbr, per, pos = f.get("pbr"), f.get("per"), f.get("pos_52w")
    out.update({"price": f.get("price"), "pbr": pbr, "per": per, "pos_52w": pos})

    # PBR 없음(ETF·일부 종목·신규상장 등) → 깊은 분석 생략
    if not pbr or pbr <= 0:
        out["verdict"] = "펀더 데이터 없음 — 분석생략(시세만)"
        return out

    q = Q.get_quality(c, code)
    roe, op_grw = q.get("roe"), q.get("op_grw")
    out.update({"roe": roe, "op_grw": op_grw})

    # NAV (지주사만)
    disc = None
    if code in (stakes.get("holdcos") or {}):
        r = N.compute_nav(c, code, stakes, capcache)
        disc = r.get("discount") if r else None
    out["nav_discount"] = disc

    # 수급·공시
    try:
        ff = O.foreign_flow(c, code)
        out["foreign_30d"] = ff.get("cum_val")
        out["foreign_turn"] = ff.get("turn")
    except Exception:
        out["foreign_30d"] = None
    try:
        tags = sorted({e["tag"] for e in O.events(code, days=180)})
    except Exception:
        tags = []
    out["tags"] = tags

    # ---- 한 줄 판정(휴리스틱, 투명) ----
    cheap = (pbr <= 0.7) or (disc is not None and disc >= 0.35)
    expensive = (pbr >= 2.0) or (per and per >= 30)
    high = (pos is not None and pos >= 0.80)
    weak = (roe is not None and roe < 5) or (op_grw is not None and op_grw < 0)
    trap = Q.is_value_trap(disc, pbr, q)
    dumping = (out.get("foreign_30d") is not None and out["foreign_30d"] <= -2000)
    soak = "자사주소각" in tags

    if trap:
        v = "🪤 점검 — 싸지만 본업부실(저ROE/적자/감익)"
    elif dumping and (expensive or high):
        v = "⚠️ 비중축소 — 고평가/고점 + 외국인 대량매도"
    elif dumping:
        v = "🚩 주의 — 외국인 대량매도(수급이탈)"
    elif expensive and high:
        v = "⚠️ 비중축소 검토 — 고평가 + 52주 고점권"
    elif cheap and (roe is not None and roe >= 8):
        v = "🟢 들고가/추가검토 — 저평가 + 양호한 수익성" + (" + 소각" if soak else "")
    elif op_grw is not None and op_grw < 0:
        v = "△ 관찰 — 이익 둔화 국면"
    else:
        v = "— 중립/홀드"
    out["verdict"] = v
    return out


def report(c: KisClient) -> str:
    """현재 계좌(read-only) 포트폴리오 점검 리포트."""
    bal = get_balance(c)
    holdings = bal.get("holdings") or []
    lines = ["📋 포트폴리오 점검 (읽기전용 · 매매 안 함)"]
    dep = _f(bal.get("deposit")); ev = _f(bal.get("eval_total")); pnl = _f(bal.get("pnl_total"))
    lines.append(f"  예수금 {f'{dep:,.0f}원' if dep is not None else '—'} · "
                 f"평가총액 {f'{ev:,.0f}원' if ev else '—'} · 평가손익 {f'{pnl:,.0f}원' if pnl is not None else '—'}")
    if not holdings:
        lines.append("  (보유 종목 없음)")
        return "\n".join(lines)

    stakes = N.load_stakes()
    capcache: dict = {}
    rows = []
    for h in holdings:
        code = h.get("code"); name = h.get("name") or code
        if not code:
            continue
        a = analyze_holding(c, code, name, stakes, capcache)
        a["_qty"] = h.get("qty"); a["_avg"] = h.get("avg_price")
        a["_pnl_pct"] = h.get("pnl_pct"); a["_pnl"] = h.get("eval_pnl")
        rows.append(a)

    lines.append(f"\n  보유 {len(rows)}종목:")
    for a in rows:
        pbr = a.get("pbr"); pbr_s = f"PBR {pbr:.2f}" if pbr and pbr > 0 else "—"
        roe = a.get("roe"); roe_s = f"ROE {roe:.0f}" if roe is not None else ""
        disc = a.get("nav_discount"); disc_s = f"NAV {disc*100:+.0f}%" if disc is not None else ""
        og = a.get("op_grw"); og_s = f"영익 {og:+.0f}%" if og is not None else ""
        fr = a.get("foreign_30d"); fr_s = f"외인 {fr:+,}억" if fr is not None else ""
        tags = "·".join(a.get("tags") or [])
        pnlp = a.get("_pnl_pct")
        meta = " ".join(s for s in [pbr_s, roe_s, disc_s, og_s, fr_s, tags] if s)
        lines.append(f"  • {a['name']}({a['code']}) 수익 {pnlp}% | {meta}")
        lines.append(f"      → {a.get('verdict','—')}")
    lines.append("\n  ※ 휴리스틱 제안 — 최종 판단은 본인. 정성(업황·이벤트)은 별도 확인.")
    return "\n".join(lines)
