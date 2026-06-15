"""NAV 할인 엔진(Phase B) — 지주사 시총 vs 보유 상장 자회사 지분 시가평가.

장부가 PBR은 지주사에 안 맞는다(자회사 지분을 취득원가로 들고 있어, 자회사 주가가
폭등해도 장부 PBR은 안 따라감 → SK스퀘어 PBR 6.74 같은 착시). 그래서 NAV로 본다:

  NAV(상장지분) = Σ (자회사 시총 × 지주사 지분율)
  NAV 할인율   = 1 − (지주사 시총 / NAV)

지분율 출처: DART 「타법인 출자현황」(dart.other_corp_investments) — 사업보고서 정규데이터.
자회사 시총: KIS inquire-price 실측(fundamentals).

⚠️ 한계(반드시 같이 표기): 이 NAV는 **상장 자회사 지분만** 합산한다.
   - 비상장 자회사 가치·자체사업가치 → 미반영(NAV 과소 → 할인율 보수적/과소평가)
   - 지주사 순부채·순현금 → 미반영
   - 법인명 매칭 실패분은 은닉하지 않고 coverage로 노출.
즉 "할인율 X% 이상"의 하한 신호로 쓰고, 정밀가치는 매칭/보강 후 해석한다.
"""
from __future__ import annotations
import csv
import json
from datetime import datetime, timedelta, timezone

from .client import KisClient
from .config import PROJECT_ROOT
from . import dart
from .fundamentals import get_fundamentals, HOLDCO_BASKET, _eok_to_human
from .logging_util import log_event

KST = timezone(timedelta(hours=9))
HOLDCO_DIR = PROJECT_ROOT / "data" / "holdco"
STAKES = HOLDCO_DIR / "stakes.json"          # DART 시드(자동)
STAKES_MANUAL = HOLDCO_DIR / "stakes_manual.json"  # 수동 보강(매칭실패·비상장 핵심)
MIN_STAKE = 1.0   # 1% 미만 지분은 노이즈로 제외(스코어 영향 미미)


# ---------- 지분율 맵 시드(DART) ----------

def seed_stakes(codes: dict[str, str] | None = None, year: int | None = None,
                reprt: str = "annual") -> dict:
    """지주사별 타법인출자현황을 DART에서 받아 상장 자회사 지분율 맵 저장.

    matched(상장 매칭) / unmatched(매칭 실패, 비상장 포함) 둘 다 기록해 coverage 투명화.
    """
    codes = codes or HOLDCO_BASKET
    year = year or (datetime.now(KST).year - 1)  # 직전 사업연도 기본
    HOLDCO_DIR.mkdir(parents=True, exist_ok=True)
    out = {"as_of": f"{datetime.now(KST):%Y-%m-%d}", "year": year, "reprt": reprt, "holdcos": {}}
    for code, name in codes.items():
        invs = dart.other_corp_investments(code, year, reprt)
        matched, unmatched = [], []
        for it in invs:
            pct = it.get("stake_pct")
            if pct is None or pct < MIN_STAKE:
                continue
            sub = dart.match_listed(it["name"])
            if sub and sub != code:   # 자기 자신 제외
                matched.append({"code": sub, "name": it["name"],
                                "stake_pct": pct, "book_eok": it.get("book_eok")})
            else:
                unmatched.append({"name": it["name"], "stake_pct": pct,
                                  "book_eok": it.get("book_eok")})
        # 동일 자회사 중복(보통주+우선주 등) → 코드별 최대 지분율 한 건으로(중복합산 방지)
        by_code: dict[str, dict] = {}
        for m in matched:
            cur = by_code.get(m["code"])
            if cur is None or (m.get("stake_pct") or 0) > (cur.get("stake_pct") or 0):
                by_code[m["code"]] = m
        matched = list(by_code.values())
        out["holdcos"][code] = {"name": name, "matched": matched, "unmatched": unmatched}
        log_event("nav_seed", code=code, matched=len(matched), unmatched=len(unmatched))
    STAKES.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def load_stakes() -> dict:
    base = json.loads(STAKES.read_text(encoding="utf-8")) if STAKES.exists() else {"holdcos": {}}
    if STAKES_MANUAL.exists():   # 수동 보강 병합(같은 자회사 code면 수동 우선)
        man = json.loads(STAKES_MANUAL.read_text(encoding="utf-8"))
        for hc, info in (man.get("holdcos") or {}).items():
            tgt = base["holdcos"].setdefault(hc, {"name": info.get("name", hc), "matched": [], "unmatched": []})
            by_code = {m["code"]: m for m in tgt.get("matched", [])}
            for m in info.get("matched", []):
                by_code[m["code"]] = m   # override/add
            tgt["matched"] = list(by_code.values())
    return base


# ---------- NAV 계산 ----------

def compute_nav(c: KisClient, code: str, stakes: dict, capcache: dict) -> dict | None:
    """단일 지주사 NAV·할인율. capcache: {code: mktcap_eok} 런 내 캐시."""
    hc = (stakes.get("holdcos") or {}).get(code)
    if not hc:
        return None

    def _cap(cd):
        if cd not in capcache:
            try:
                capcache[cd] = get_fundamentals(c, cd).get("mktcap_eok")
            except Exception:
                capcache[cd] = None
        return capcache[cd]

    self_cap = _cap(code)
    nav_eok = 0.0
    subs = []
    for m in hc.get("matched", []):
        cap = _cap(m["code"])
        pct = m.get("stake_pct")
        val = (cap * pct / 100.0) if (cap and pct) else None
        if val:
            nav_eok += val
        subs.append({**m, "sub_cap_eok": cap, "stake_value_eok": round(val) if val else None})
    subs.sort(key=lambda s: s.get("stake_value_eok") or 0, reverse=True)

    discount = (1 - self_cap / nav_eok) if (self_cap and nav_eok) else None
    # coverage: 매칭된 상장지분 장부가 / 전체(매칭+미매칭) 장부가 — NAV가 얼마나 포괄적인가
    bk_m = sum(m.get("book_eok") or 0 for m in hc.get("matched", []))
    bk_u = sum(u.get("book_eok") or 0 for u in hc.get("unmatched", []))
    cov = (bk_m / (bk_m + bk_u)) if (bk_m + bk_u) else None

    return {
        "code": code, "name": hc.get("name"),
        "mktcap_eok": self_cap, "nav_eok": round(nav_eok) if nav_eok else None,
        "discount": round(discount, 3) if discount is not None else None,
        "n_subs": len([s for s in subs if s.get("stake_value_eok")]),
        "n_unmatched": len(hc.get("unmatched", [])),
        "coverage": round(cov, 2) if cov is not None else None,
        "subs": subs,
    }


def snapshot(c: KisClient, codes: dict[str, str] | None = None) -> dict:
    codes = codes or HOLDCO_BASKET
    stakes = load_stakes()
    capcache: dict = {}
    rows = []
    for code in codes:
        r = compute_nav(c, code, stakes, capcache)
        if r:
            rows.append(r)
    # 할인율 내림차순(가장 저평가 위로). None은 뒤로.
    rows.sort(key=lambda r: (r.get("discount") is None, -(r.get("discount") or -9)))
    day = f"{datetime.now(KST):%Y-%m-%d}"
    HOLDCO_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = HOLDCO_DIR / f"nav_{day}.csv"
    cols = ["code", "name", "mktcap_eok", "nav_eok", "discount", "n_subs", "n_unmatched", "coverage"]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})
    return {"date": day, "rows": rows, "csv": str(csv_path), "stakes_as_of": stakes.get("as_of")}


def summary(c: KisClient, codes: dict[str, str] | None = None, top: int | None = None) -> str:
    snap = snapshot(c, codes)
    rows = snap["rows"]
    n_all = len(rows)
    if top:   # 할인율 상위 N개만(이미 할인율 내림차순 정렬됨)
        rows = rows[:top]
    lines = [f"💎 지주사 NAV 할인율 — {snap['date']} "
             f"(상장 자회사 지분 시가평가, 지분율 {snap.get('stakes_as_of') or '?'} DART)"
             + (f" · 전체 {n_all}개 중 상위 {len(rows)}" if top else "")]
    lines.append("  순위 종목(코드)  할인율  시총→NAV  자회사  cov  판정")
    have = [r for r in rows if r.get("discount") is not None]
    COV_HI = 0.6   # 프리미엄(음수) 신뢰 기준
    for i, r in enumerate(rows, 1):
        d = r.get("discount")
        d_s = f"{d*100:>5.0f}%" if d is not None else "  —  "
        cap = _eok_to_human(r.get("mktcap_eok"))
        nav = _eok_to_human(r.get("nav_eok"))
        cov = r.get("coverage")
        cov_s = f"{cov*100:>3.0f}%" if cov is not None else " — "
        # 판정 로직:
        #  +할인율: 비상장 누락은 NAV 과소 → 할인율은 '보수적 하한'(클수록 견고). cov 무관 신뢰.
        #  -프리미엄: cov 높으면 진짜 프리미엄(비쌈), cov 낮으면 NAV 과소 아티팩트(무시).
        if d is None:
            verdict = "데이터부족"
        elif d >= 0.4:
            verdict = "🟢 저평가(하한)"
        elif d >= 0:
            verdict = "△ 소폭할인"
        elif cov is not None and cov >= COV_HI:
            verdict = "🔴 프리미엄(고cov)"
        else:
            verdict = "⚠ 저cov아티팩트"
        lines.append(f"  {i:>2}. {r['name']}({r['code']})  {d_s}  {cap}→{nav}  "
                     f"자{r.get('n_subs',0)}  {cov_s}  {verdict}")
    if not have:
        lines.append("\n⚠️ 계산된 종목 없음 — 'navseed' 로 지분율 맵 먼저 생성하세요.")
    else:
        n_cheap = sum(1 for r in have if r['discount'] >= 0.4)
        lines.append(f"\n🟢 할인율≥40%(보수적 하한): {n_cheap}/{len(have)}종목.")
        lines.append("※ NAV=상장지분만(비상장·순현금·부채 미반영). +할인율은 보수적 하한(견고), "
                     "-프리미엄은 cov<60%면 NAV 과소 아티팩트로 해석.")
    lines.append(f"💾 {snap['csv']}")
    return "\n".join(lines)


def detail(c: KisClient, code: str) -> str:
    """단일 지주사 NAV 구성(자회사별 지분가치) 상세."""
    stakes = load_stakes()
    r = compute_nav(c, code, stakes, {})
    if not r:
        return f"{code}: 지분율 맵 없음 — 'navseed' 먼저."
    lines = [f"💎 {r['name']}({code}) NAV 분해",
             f"  시총 {_eok_to_human(r['mktcap_eok'])} / NAV {_eok_to_human(r['nav_eok'])} "
             f"→ 할인율 {(r['discount']*100):.0f}%" if r.get("discount") is not None else "  할인율 계산불가"]
    for s in r["subs"]:
        if s.get("stake_value_eok"):
            lines.append(f"  • {s['name']}({s['code']}) {s['stake_pct']:.1f}% "
                         f"× 시총 {_eok_to_human(s['sub_cap_eok'])} = {_eok_to_human(s['stake_value_eok'])}")
    if r.get("n_unmatched"):
        hc = stakes["holdcos"][code]
        um = ", ".join(f"{u['name']}({u['stake_pct']:.0f}%)" for u in hc.get("unmatched", [])[:8])
        lines.append(f"  ⚠️ 미매칭(비상장/매칭실패) {r['n_unmatched']}건: {um}")
    return "\n".join(lines)
