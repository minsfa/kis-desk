"""종목 특이이슈 진단 — 데이터 4축을 모아 '컨텍스트 블록 + 11항목 프롬프트'로 조립(판단X).

조립 전용: 엔진은 LLM을 호출하지 않는다. 출력 텍스트를 madu_bot(텔레그램) 또는 사람이 읽고 판단.
4축: 시세/거래량(KIS) · 급등눌림 통계(pattern) · 공시(DART) · 뉴스(네이버).
키(DART/네이버) 없거나 데이터 없으면 해당 축은 '확인불가'로 표기하고 계속 진행.
"""
from __future__ import annotations
import csv
from statistics import mean

from .client import KisClient
from .config import PROJECT_ROOT
from .market import get_price
from .pattern import surge_pullback, suggest_levels

DAILY = PROJECT_ROOT / "data" / "daily"


def _vol_multiple(code: str, today_vol: float, lookback: int = 20) -> float | None:
    """최근 lookback 영업일 평균 대비 오늘 거래량 배수. CSV 없으면 None."""
    p = DAILY / f"{code}.csv"
    if not p.exists() or today_vol <= 0:
        return None
    vols = []
    with open(p, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                vols.append(float(r["volume"]))
            except Exception:
                pass
    base = vols[-lookback:-1] if len(vols) > lookback else vols[:-1]
    if not base:
        return None
    avg = mean(base)
    return round(today_vol / avg, 1) if avg > 0 else None


def _name(code: str) -> str | None:
    from .news import _name as nm
    return nm(code)


def run(c: KisClient, code: str, name: str | None = None) -> str:
    from . import stratcfg
    cfg = stratcfg.load()
    surge, target, dip, budget = cfg["surge"], cfg["target"], cfg["dip"], cfg["budget"]
    name = name or _name(code) or code

    # 1) 시세·거래량 (KIS)
    px = get_price(c, code)
    close = float(px.get("price") or 0)
    chg = float(px.get("change_pct") or 0)
    vol = float(px.get("volume") or 0)
    volx = _vol_multiple(code, vol)
    volx_s = f"평소 {volx}배" if volx else "평소대비 미상(일봉없음)"

    # 2) 통계 (pattern)
    stats = surge_pullback(code, surge)
    sug = suggest_levels(code, close, surge_min=surge, target_pct=target)

    L = [f"═══ 진단 컨텍스트: {name}({code}) ═══"]
    L.append(f"[시세] 현재가 {close:,.0f}원 ({chg:+.2f}%) · 고 {float(px.get('high') or 0):,.0f}/저 {float(px.get('low') or 0):,.0f} · 거래량 {vol:,.0f} ({volx_s})")
    if stats:
        L.append(f"[통계] +{surge}%↑급등 {stats['n']}건 · 익일저가중앙 {stats['dip_p50']:+.1f}%/하위25% {stats['dip_p25']:+.1f}% · 반등중앙 {stats['bounce_med']:+.1f}% · 익일하락률 {stats['down_rate']:.0f}%")
    else:
        L.append(f"[통계] 표본부족 — 고정 -{dip}% 눌림 적용(일봉 통계 없음)")
    if sug:
        lv = sug["levels"]
        def q(p): return int(budget // p) if 0 < p <= budget else 0
        L.append(f"[제안] 진입 {lv[0]['price']:,}({q(lv[0]['price'])}주,~{lv[0]['fill_prob']}%)/{lv[1]['price']:,}({q(lv[1]['price'])}주,~{lv[1]['fill_prob']}%) → 목표 {lv[0]['target']:,}/{lv[1]['target']:,}")
        entry, tgt, dipp, bnc = lv[0]['price'], lv[0]['target'], stats['dip_p50'], stats['bounce_med']
    else:
        entry = round(close * (1 - dip / 100))
        tgt = round(entry * (1 + target / 100))
        dipp, bnc = -dip, target
        L.append(f"[제안] 진입 {entry:,} → 목표 {tgt:,} (고정공식)")

    # 3) 공시 (DART) — 키 없으면 확인불가
    try:
        from . import dart
        ds = dart.recent_disclosures(code, days=14)
        if ds:
            L.append(f"[공시 DART 최근14일 {len(ds)}건]")
            for x in ds[:8]:
                mark = {"악재?": "🔴", "호재?": "🟢", "중립": "⚪"}.get(x["flag"], "⚪")
                L.append(f"  {mark} {x['date']} {x['title']}")
        else:
            L.append("[공시 DART] 최근14일 공시 없음(ETF 등은 정상)")
    except Exception as e:
        L.append(f"[공시 DART] 확인불가 ({type(e).__name__})")

    # 4) 뉴스 (네이버) — 키 없으면 확인불가
    try:
        from . import news
        items = news.for_stock(code, name=name, display=8)
        if items:
            L.append(f"[뉴스 최근{len(items)}건]")
            for it in items:
                L.append(f"  · {it['title']}")
        else:
            L.append("[뉴스] 조회결과 없음")
    except Exception as e:
        L.append(f"[뉴스] 확인불가 ({type(e).__name__})")

    # ── 11항목 진단 프롬프트 (madu_bot/사람이 위 컨텍스트로 판단) ──
    L.append("")
    L.append("═══ 진단 요청 프롬프트 (위 컨텍스트만 근거로 판정) ═══")
    L.append(f"""역할: 한국 주식 단기 트레이딩 리스크 진단가.
대상: {name}({code}), 현재가 {close:,.0f}원, 등락 {chg:+.2f}%, 거래량 {volx_s}.
전략: 내일 눌림목 진입 {entry:,} → 익절 {tgt:,}. 통계 익일저가 {dipp:+.1f}%, 반등 {bnc:+.1f}%.

위 컨텍스트(공시·뉴스·통계)만 근거로 아래를 판정하라. 근거 없으면 '확인불가' 명시:
A. 내부요인  1)최근공시 호재/악재 분류  2)오늘 급등/급락 직접원인 기사  3)수급주체(외인/기관/개인)  4)거래량 신규유입 여부
B. 외부요인  5)섹터 동반 여부(나홀로면 위험)  6)매크로 트리거(금리/환율/정책)  7)글로벌 동종 방향
C. 종합  8)원인 6분류[실적/수급/테마/매크로/기술반등/루머]  9)익일 지속성 0~100+근거  10)권고[진행/진입가하향/회피]+이유  11)손절라인 제안(진입가 대비 -%)
출력은 번호별 간결히. 추측은 추측이라 표기.""")
    return "\n".join(L)
