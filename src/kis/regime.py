"""레짐/로테이션 대시보드(오건영 '패스파인더의 눈' 참고) — KR+US 상대강도.

종목(가치/성장) 위에 얹는 탑다운 축: "지금 자금이 어느 갈림길로 도는가"를 ETF 기간수익률로 추적.
오건영 갈림길(에너지vs테크·금·원전·중국본토vs홍콩·미국일극) 중 ETF 상대강도로 수치화 가능한 것만.
거시 현물지표(Brent·EIA·금리수치·운임·CPI)는 구조화 불가 → WebSearch 스냅샷으로 별도(여기서 안 흉내냄).

데이터: KR 일봉 FHKST03010100 / US 일봉 HHDFS76240000(overseas dailyprice). 1콜 ~100영업일(≈5개월).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone

from .client import KisClient
from .logging_util import log_event

KST = timezone(timedelta(hours=9))
KR_CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
US_DAILY = "/uapi/overseas-price/v1/quotations/dailyprice"

# 유니버스: (ticker, market, exch, label, group). exch=KR은 'J', US는 AMS/NAS/NYS.
# US 티커는 가격조회 성공으로 검증, KR 코드는 search-stock-info 이름으로 런타임 검증.
UNIVERSE = [
    # 에너지 vs 테크 (오건영 핵심 F)
    ("XLE", "us", "AMS", "에너지(XLE)", "에너지vs테크"),
    ("XLK", "us", "AMS", "테크(XLK)", "에너지vs테크"),
    ("091160", "kr", "J", "KODEX반도체", "에너지vs테크"),
    # 미국 코어
    ("SPY", "us", "AMS", "S&P500(SPY)", "미국코어"),
    ("QQQ", "us", "NAS", "나스닥100(QQQ)", "미국코어"),
    ("069500", "kr", "J", "KODEX200", "미국코어"),
    # 안전/실물
    ("GLD", "us", "AMS", "금(GLD)", "안전·실물"),
    ("USO", "us", "AMS", "WTI원유(USO)", "안전·실물"),
    ("URA", "us", "AMS", "우라늄·원전(URA)", "안전·실물"),
    ("TLT", "us", "NAS", "美장기채(TLT)", "안전·실물"),
    # 중국 본토 vs 홍콩 (이미 발굴)
    ("192090", "kr", "J", "TIGER차이나CSI300(본토)", "중국본토vs홍콩"),
    ("372330", "kr", "J", "KODEX차이나항셍테크(홍콩)", "중국본토vs홍콩"),
    # 미국 일극 균열 (G)
    ("EWJ", "us", "AMS", "일본(EWJ)", "미국vs비미국"),
    ("VEU", "us", "AMS", "비미국(VEU)", "미국vs비미국"),
]


def _us_closes(c: KisClient, symb: str, excd: str) -> list[float]:
    d = c.get(US_DAILY, "HHDFS76240000",
              {"AUTH": "", "EXCD": excd, "SYMB": symb, "GUBN": "0", "BYMD": "", "MODP": "1"})
    out = d.get("output2") or []
    cl = [float(r["clos"]) for r in out if r.get("clos") not in (None, "", "0")]
    return cl  # 최신 → 과거 순


def _kr_closes(c: KisClient, code: str) -> list[float]:
    end = datetime.now(KST)
    start = end - timedelta(days=160)
    d = c.get(KR_CHART, "FHKST03010100",
              {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
               "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
               "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
               "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
    out = d.get("output2") or []
    cl = [float(r["stck_clpr"]) for r in out if r.get("stck_clpr") not in (None, "", "0")]
    return cl  # 최신 → 과거 순


def _verify_kr(c: KisClient, code: str) -> str | None:
    try:
        d = c.get("/uapi/domestic-stock/v1/quotations/search-stock-info", "CTPF1604R",
                  {"PRDT_TYPE_CD": "300", "PDNO": code})
        return (d.get("output", {}) or {}).get("prdt_abrv_name") or None
    except Exception:
        return None


def _ret(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or closes[0] <= 0 or closes[n] <= 0:
        return None
    return closes[0] / closes[n] - 1


def collect(c: KisClient) -> list[dict]:
    rows = []
    for tk, mkt, exch, label, group in UNIVERSE:
        try:
            cl = _us_closes(c, tk, exch) if mkt == "us" else _kr_closes(c, tk)
            name = label
            if mkt == "kr":
                v = _verify_kr(c, tk)
                if v:
                    name = f"{v}"   # KIS 확인 이름
            if len(cl) < 25:
                rows.append({"ticker": tk, "group": group, "label": label, "err": "데이터부족"})
                continue
            rows.append({
                "ticker": tk, "market": mkt, "group": group, "label": label, "name": name,
                "last": cl[0],
                "r1m": _ret(cl, 21), "r3m": _ret(cl, 63), "rall": _ret(cl, len(cl) - 1),
                "ndays": len(cl),
            })
        except Exception as e:
            rows.append({"ticker": tk, "group": group, "label": label, "err": str(e)})
    log_event("regime", n=len([r for r in rows if not r.get("err")]))
    return rows


def _pct(v):
    return f"{v*100:+.1f}%" if v is not None else "—"


def dashboard(c: KisClient) -> str:
    rows = [r for r in collect(c)]
    ok = [r for r in rows if not r.get("err")]
    day = f"{datetime.now(KST):%Y-%m-%d}"
    L = [f"🌍 레짐·로테이션 대시보드 — {day} (ETF 기간수익률, 오건영 갈림길 참고)",
         "  그룹/종목  ·  1개월 / 3개월 / 전체(~5M)"]
    # 그룹별 출력
    groups = {}
    for r in ok:
        groups.setdefault(r["group"], []).append(r)
    for g, rs in groups.items():
        L.append(f"\n[{g}]")
        for r in sorted(rs, key=lambda x: -(x.get("r3m") or -9)):
            L.append(f"  {r['name']}({r['ticker']})  {_pct(r['r1m'])} / {_pct(r['r3m'])} / {_pct(r['rall'])}")
    # 갈림길 신호(상대강도)
    def _g(tk, k):
        for r in ok:
            if r["ticker"] == tk:
                return r.get(k)
        return None
    L.append("\n📊 갈림길 신호 (3개월 상대강도)")
    xle, xlk = _g("XLE", "r3m"), _g("XLK", "r3m")
    if xle is not None and xlk is not None:
        lead = "에너지" if xle > xlk else "테크"
        L.append(f"  • 에너지 vs 테크: XLE {_pct(xle)} vs XLK {_pct(xlk)} → **{lead} 우위**")
    cn_m, cn_h = _g("192090", "r3m"), _g("372330", "r3m")
    if cn_m is not None and cn_h is not None:
        L.append(f"  • 중국 본토 vs 홍콩: 본토 {_pct(cn_m)} vs 홍콩 {_pct(cn_h)} → **{'본토' if cn_m>cn_h else '홍콩'} 우위**")
    spy, veu = _g("SPY", "r3m"), _g("VEU", "r3m")
    if spy is not None and veu is not None:
        L.append(f"  • 미국 vs 비미국: SPY {_pct(spy)} vs VEU(ex-US) {_pct(veu)} → **{'미국' if spy>veu else '비미국'} 우위**")
    gld = _g("GLD", "r3m")
    if gld is not None:
        L.append(f"  • 금(GLD) 3개월: {_pct(gld)}")
    # 전체 3개월 리더/래거드
    ranked = sorted([r for r in ok if r.get("r3m") is not None], key=lambda x: -x["r3m"])
    if ranked:
        top = ranked[:3]; bot = ranked[-3:]
        L.append("\n🏆 3개월 리더: " + ", ".join(f"{r['name']}({_pct(r['r3m'])})" for r in top))
        L.append("🐌 3개월 래거드: " + ", ".join(f"{r['name']}({_pct(r['r3m'])})" for r in bot))
    errs = [r for r in rows if r.get("err")]
    if errs:
        L.append("\n⚠️ 조회실패: " + ", ".join(f"{r['label']}({r['ticker']})" for r in errs))
    L.append("\n※ 거시 현물(Brent·EIA재고·금리수치·운임·CPI)은 별도 WebSearch 스냅샷. 여긴 ETF 상대강도만.")
    return "\n".join(L)
