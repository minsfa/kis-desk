"""일일 후보 제안 — 장 마감 후 '내일 후보군 + 진입/목표가'를 정리(데이터·제안 전용, 주문X).

산출: 시장 레짐 한 줄 + C2(오늘 급등→내일 눌림) 후보 + 상시 C1 워치 + 수급 전환 종목.
각 후보에 NXT거래여부·제안진입(-dip%)·목표(+target%)·예산내 수량 표기.
OpenClaw cron이 15:35(KRX마감)·20:05(NXT마감)에 호출해 텔레그램 보고.
"""
from __future__ import annotations
import csv
import os
from datetime import datetime, timezone, timedelta

from .client import KisClient
from .config import TR_PRICE, PROJECT_ROOT
from .daily import VALUE_BASKET
from .strat_v0 import C1_LEADERS, ETF_CODES, _nxt_ok
from .pattern import suggest_levels
from .tick import round_tick

KST = timezone(timedelta(hours=9))
DATA_DIR = PROJECT_ROOT / "data"


def _quote(c, code):
    # KRX 공식 종가/전일대비율 기준('J'). UN(NXT포함)은 시간외 가격이 섞여 종가와 어긋남.
    d = c.get("/uapi/domestic-stock/v1/quotations/inquire-price", TR_PRICE,
              {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    o = d.get("output", {}) or {}
    return float(o.get("stck_prpr") or 0), float(o.get("prdy_ctrt") or 0)


def _foreign_turn(code):
    p = DATA_DIR / "investor_history" / f"{code}.csv"
    if not p.exists():
        return None
    fq = []
    with open(p, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try: fq.append(float(r.get("foreign_netqty") or 0))
            except Exception: pass
    if len(fq) < 10:
        return None
    return sum(fq[-5:]), sum(fq[-10:-5])  # 최근5, 직전5


def run(c: KisClient, budget=None, dip=None, target=None, surge=None) -> str:
    from . import stratcfg
    cfg = stratcfg.load()
    budget = cfg["budget"] if budget is None else budget
    dip = cfg["dip"] if dip is None else dip
    target = cfg["target"] if target is None else target
    surge = cfg["surge"] if surge is None else surge
    top = int(cfg.get("top", 5))
    excl = set(cfg.get("exclude", []))
    L = [f"📋 내일 후보 제안 — {datetime.now(KST):%Y-%m-%d %H:%M}"]
    # 시장 레짐
    reg = {}
    for code, n in [("069500", "KOSPI200"), ("229200", "코스닥150"), ("091160", "반도체")]:
        try: reg[n] = _quote(c, code)[1]
        except Exception: reg[n] = 0
    L.append(f"시장: KOSPI200 {reg['KOSPI200']:+.1f}% · 코스닥150 {reg['코스닥150']:+.1f}% · 반도체 {reg['반도체']:+.1f}% (오늘)")

    rank_n = int(cfg.get("rank_n", 10))
    uni = dict(VALUE_BASKET); uni.update(C1_LEADERS); uni.update(cfg.get("c1_extra", {}))
    cands, turns = [], []
    for code, name in uni.items():
        if code in excl:
            continue
        try:
            close, chg = _quote(c, code)
        except Exception:
            continue
        if close <= 0:
            continue
        nxt = (code not in ETF_CODES) and _nxt_ok(c, code)
        # 진입/목표: 종목별 과거 통계(얕은눌림 p50) 우선, 일봉 없으면 고정 -dip% 폴백
        sug = suggest_levels(code, close, surge_min=surge, target_pct=target)
        if sug:
            entry = sug["levels"][0]["price"]
            tgt = sug["levels"][0]["target"]
            basis = "통계"
        else:
            entry = round_tick(close * (1 - dip / 100), up=False)
            tgt = round_tick(entry * (1 + target / 100), up=True)
            basis = "고정"
        qty = int(budget // entry) if 0 < entry <= budget else 0
        if qty < 1:
            continue
        sig = []
        if chg >= surge:                                      # 오늘 급등 → 내일 C2 눌림목
            sig.append("C2")
        if code in C1_LEADERS or code in cfg.get("c1_extra", {}):  # 대장/관심 상시워치
            sig.append("C1")
        ft = _foreign_turn(code)
        if ft and ft[0] > 0 and ft[1] < 0:                    # 외인 매도→매수 전환
            sig.append("외인전환"); turns.append(name)
        if not sig:                                           # 아무 신호 없으면 후보 제외
            continue
        # 점수 = 오늘등락 + 외인전환 가점 + 대장 가점 (투명·튜닝가능)
        score = round(chg + (8 if "외인전환" in sig else 0) + (4 if "C1" in sig else 0), 1)
        cands.append({"name": name, "code": code, "chg": chg, "close": close, "qty": qty,
                      "nxt": nxt, "entry": entry, "target": tgt, "score": score, "sig": sig,
                      "basis": basis})

    cands.sort(key=lambda x: -x["score"])
    ranked = cands[:rank_n]
    L.append(f"\n🏆 종합 랭킹 TOP {len(ranked)} (점수 = 오늘등락 + 외인전환 +8 + 대장 +4)")
    if ranked:
        L.append("| # | 종목(코드) | 신호 | 등락 | 종가 | 진입가(수량) | 목표가 | 점수 | 방식 | 시장 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(ranked, 1):
            tag = "·".join(r["sig"]); nxtt = "NXT" if r["nxt"] else "정규장만"
            L.append(f"| {i} | {r['name']}({r['code']}) | {tag} | {r['chg']:+.1f}% | {r['close']:,.0f}"
                     f" | {r['entry']:,}({r['qty']}주) | {r['target']:,} | {r['score']} | {r['basis']} | {nxtt} |")
    else:
        L.append("  후보 없음")
    L.append(f"\n[수급 전환(외인 매수전환)]: {', '.join(turns) if turns else '없음'}")
    L.append("\n👉 ① 분석: 'diagnose <번호>'  ② 승인: 'approve add <번호>' (진입/목표/수량 자동) · 단가 바꾸려면 --price 추가")
    L.append("   승인한 것만 다음날 08:01 자동주문, 승인 안 하면 무거래.")
    try:
        from . import approve
        L.append(approve.summary())
    except Exception:
        pass

    # 저장 — 랭킹 번호 기준(diagnose/approve 가 이 번호를 참조)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(_proposal_path(), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["rank", "name", "code", "chg", "close", "qty", "nxt", "entry", "target", "score", "signals", "basis"])
        for i, r in enumerate(ranked, 1):
            w.writerow([i, r["name"], r["code"], f"{r['chg']:.1f}", f"{r['close']:.0f}",
                        r["qty"], r["nxt"], r["entry"], r["target"], r["score"], "|".join(r["sig"]), r["basis"]])
    return "\n".join(L)


def _proposal_path(now=None):
    return DATA_DIR / f"proposal_{(now or datetime.now(KST)):%Y-%m-%d}.csv"


def load_today(now=None) -> list[dict]:
    """오늘자 제안 랭킹 행들(rank 순). 없으면 빈 리스트."""
    p = _proposal_path(now)
    if not p.exists():
        return []
    with open(p, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pick(rank: int, now=None) -> dict | None:
    """랭킹 번호로 후보 1건 조회. diagnose/approve 번호선택용."""
    for r in load_today(now):
        if str(r.get("rank")) == str(rank):
            return r
    return None
