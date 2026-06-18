"""거시 가치 오버레이 데이터 — 에너지 DCA(XLE/XOM) 사이징 보조 입력.

전부 read-only 외부 조회(주문 아님). 키/소스 없으면 해당 항목 None으로 graceful degrade.
검증 기준(2026-06-18 실측):
- WTI: FRED `DCOILWTICO` (키 FRED_API_KEY). 일간.
- 원유재고: EIA `/v2/seriesid/PET.WCESTUS1.W` (키 EIA_API_KEY). 주간, "excluding SPR". 검증됨.
- 배당수익률: yfinance `trailingAnnualDividendYield`(=fraction). ⚠️ `dividendYield`는 퍼센트라 쓰지 말 것.
- 셰일 손익분기: 분기성 → strategy.json 수동값(기본 65~70).
- 리그수: HTML 파싱 불안정 → 보류(추후 oilpriceapi 등).
"""
from __future__ import annotations
import os
import requests

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"
EIA_SERIES_URL = "https://api.eia.gov/v2/seriesid/PET.WCESTUS1.W"  # 주간 상업용 원유재고(SPR제외)

# 튜닝(추후 strategy.json으로 이동 가능). 가치 오버레이 임계.
WTI_FLOOR = 67.0      # 셰일 손익분기 중앙(~$65-70). 이 근처/이하면 딥밸류
WTI_PREMIUM = 85.0    # 이 위면 프리미엄 영역(되돌림 위험) → 사이즈↓
XLE_YIELD_CHEAP = 0.035   # XLE trailing 배당수익률 이 이상이면 가격발 저평가
XLE_YIELD_RICH = 0.025


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def wti_price() -> float | None:
    """FRED DCOILWTICO 최신 종가(USD/bbl). 키 없으면 None."""
    key = os.getenv("FRED_API_KEY")
    if not key:
        return None
    try:
        r = requests.get(FRED_URL, params={
            "series_id": "DCOILWTICO", "api_key": key,
            "sort_order": "desc", "limit": 5, "file_type": "json",
        }, timeout=10)
        r.raise_for_status()
        for o in r.json().get("observations", []):
            v = _num(o.get("value"))   # 결측은 "." → None, 다음 관측으로
            if v is not None:
                return v
    except Exception:
        return None
    return None


def crude_inventory(weeks_5yr: int = 260) -> dict:
    """EIA 주간 상업용 원유재고(WCESTUS1) 최신값 + 동주차 5년평균 대비. 키 없으면 빈 dict."""
    key = os.getenv("EIA_API_KEY")
    if not key:
        return {}
    try:
        r = requests.get(EIA_SERIES_URL, params={"api_key": key, "length": weeks_5yr},
                         timeout=15)
        r.raise_for_status()
        rows = r.json().get("response", {}).get("data", [])
        if not rows:
            return {}
        # 최신순 → 최신값
        rows = sorted(rows, key=lambda x: x.get("period", ""), reverse=True)
        latest = rows[0]
        latest_val = _num(latest.get("value"))
        # 동주차(주차) 5년평균 프록시
        import datetime as _dt
        def _wk(p):
            try:
                return _dt.date.fromisoformat(p).isocalendar()[1]
            except Exception:
                return None
        target_wk = _wk(latest.get("period"))
        same_wk = [_num(x.get("value")) for x in rows
                   if _wk(x.get("period")) == target_wk and _num(x.get("value")) is not None]
        avg5 = sum(same_wk) / len(same_wk) if same_wk else None
        return {
            "period": latest.get("period"),
            "value_mbbl": latest_val,
            "five_yr_avg_mbbl": round(avg5) if avg5 else None,
            "vs_5yr_mbbl": round(latest_val - avg5) if (latest_val and avg5) else None,
            "units": latest.get("units", "MBBL"),
        }
    except Exception:
        return {}


def dividend_yield(ticker: str) -> float | None:
    """trailing 배당수익률(fraction, 예 0.0395). yfinance 미설치/실패 시 None.
    ⚠️ yfinance `dividendYield`는 퍼센트(2.65)라 안 씀 — trailingAnnualDividendYield(fraction) 사용."""
    try:
        import yfinance as yf
    except Exception:
        return None
    try:
        info = yf.Ticker(ticker).info
        y = info.get("trailingAnnualDividendYield")
        if y is not None:
            return float(y)
        # 폴백: rate/price 직접계산(검증: XLE 2.16/54.67=3.95%)
        rate = _num(info.get("trailingAnnualDividendRate"))
        px = _num(info.get("previousClose"))
        if rate and px:
            return round(rate / px, 4)
    except Exception:
        return None
    return None


def macro_snapshot() -> dict:
    """가치 오버레이용 거시 스냅샷(전부 read-only)."""
    return {
        "wti": wti_price(),
        "inventory": crude_inventory(),
        "xle_yield": dividend_yield("XLE"),
        "xom_yield": dividend_yield("XOM"),
        "shale_breakeven": WTI_FLOOR,
    }


def valuation_factor(snap: dict | None = None) -> tuple[float, list[str]]:
    """거시 가치 → 매수강도 보정배수(대략 0.7~1.4). (배수, 사유들) 반환.
    데이터 없으면 1.0(중립). 기술적 사이징(z-score/추세)에 곱해 쓰는 '오버레이'.
    """
    snap = snap or macro_snapshot()
    f = 1.0
    why = []
    wti = snap.get("wti")
    if wti is not None:
        if wti <= WTI_FLOOR:
            f *= 1.3; why.append(f"WTI ${wti:.0f}≤손익분기 → 딥밸류 ×1.3")
        elif wti >= WTI_PREMIUM:
            f *= 0.8; why.append(f"WTI ${wti:.0f}≥프리미엄 → ×0.8")
        else:
            why.append(f"WTI ${wti:.0f} 중립대")
    xle_y = snap.get("xle_yield")
    if xle_y is not None:
        if xle_y >= XLE_YIELD_CHEAP:
            f *= 1.15; why.append(f"XLE배당 {xle_y*100:.1f}%≥{XLE_YIELD_CHEAP*100:.0f}% → ×1.15")
        elif xle_y <= XLE_YIELD_RICH:
            f *= 0.9; why.append(f"XLE배당 {xle_y*100:.1f}%≤{XLE_YIELD_RICH*100:.0f}% → ×0.9")
    f = max(0.5, min(1.6, f))
    if not why:
        why.append("거시데이터 없음(키 미발급) → 중립 1.0")
    return round(f, 3), why


def report() -> str:
    """`macro` CLI용 텍스트 — 현재 거시 스냅샷 + 가치배수."""
    snap = macro_snapshot()
    f, why = valuation_factor(snap)
    inv = snap.get("inventory") or {}
    L = ["🛢️ 에너지 거시 가치 오버레이"]
    L.append(f"  WTI: {('$%.2f' % snap['wti']) if snap.get('wti') else 'n/a(FRED키 필요)'}"
             f"  (셰일 손익분기 ~${WTI_FLOOR:.0f})")
    if inv:
        L.append(f"  원유재고({inv.get('period')}): {inv.get('value_mbbl'):,} {inv.get('units')}"
                 f"  · 5년평균대비 {inv.get('vs_5yr_mbbl'):+,}" if inv.get('vs_5yr_mbbl') is not None
                 else f"  원유재고({inv.get('period')}): {inv.get('value_mbbl')}")
    else:
        L.append("  원유재고: n/a(EIA키 필요)")
    L.append(f"  배당수익률(trailing): XLE {('%.2f%%' % (snap['xle_yield']*100)) if snap.get('xle_yield') else 'n/a'}"
             f" · XOM {('%.2f%%' % (snap['xom_yield']*100)) if snap.get('xom_yield') else 'n/a'}")
    L.append(f"  → 가치배수 ×{f}  ({' / '.join(why)})")
    return "\n".join(L)
