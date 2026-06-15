"""일봉(daily) 히스토리 다운로더 — 과거 OHLCV를 받아 data/daily/<code>.csv 적재.

KIS inquire-daily-itemchartprice(FHKST03010100): 1콜 최대 ~100영업일.
기간이 길면 종료일을 뒤로 밀며 페이징. 레이트리밋(EGW00201) 시 백오프 재시도.
백테스트(특히 거시/가치 스윙) 데이터 토대.
"""
from __future__ import annotations
import csv
import time
from datetime import datetime, timedelta, timezone

from .client import KisClient
from .config import PROJECT_ROOT

KST = timezone(timedelta(hours=9))
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
CHART_PATH = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"

# 가치주 리서치 + 추가 대형주/ETF (확장 유니버스)
VALUE_BASKET = {
    # 사용자 지정
    "316140": "우리금융지주", "360750": "TIGER미국S&P500", "028260": "삼성물산",
    # 금융
    "105560": "KB금융", "055550": "신한지주", "086790": "하나금융지주",
    "175330": "JB금융지주", "024110": "기업은행", "032830": "삼성생명",
    "000810": "삼성화재", "005830": "DB손해보험", "001450": "현대해상",
    # 자동차/부품
    "005380": "현대차", "000270": "기아", "012330": "현대모비스",
    # 통신/유틸
    "017670": "SK텔레콤", "030200": "KT", "015760": "한국전력",
    # 소비재
    "033780": "KT&G", "049770": "동원F&B", "000080": "하이트진로",
    # 산업/소재
    "005490": "POSCO홀딩스", "011170": "롯데케미칼", "006360": "GS건설",
    "375500": "DL이앤씨", "004360": "세방", "001120": "LX인터내셔널", "011200": "HMM",
    # 대형 성장/반도체/플랫폼 (비교용)
    "005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER",
    "035720": "카카오", "051910": "LG화학", "006400": "삼성SDI",
    "207940": "삼성바이오로직스", "373220": "LG에너지솔루션", "068270": "셀트리온",
    "010130": "고려아연",
    # ETF
    "069500": "KODEX200", "379800": "KODEX미국S&P500",
}


def _window(c: KisClient, code: str, d1: str, d2: str) -> list[dict]:
    for attempt in range(5):
        d = c.get(CHART_PATH, "FHKST03010100", {
            "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": d1, "FID_INPUT_DATE_2": d2,
            "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
        })
        if str(d.get("rt_cd")) == "0":
            return [r for r in (d.get("output2") or []) if r.get("stck_bsop_date")]
        time.sleep(0.4 + attempt * 0.3)  # 레이트리밋 등 백오프
    return []


def download(c: KisClient, code: str, start: str, end: str) -> dict:
    rows: dict[str, dict] = {}
    cursor = end
    for _ in range(60):  # 안전 상한(60*100영업일)
        out = _window(c, code, start, cursor)
        if not out:
            break
        for r in out:
            rows[r["stck_bsop_date"]] = r
        oldest = min(r["stck_bsop_date"] for r in out)
        if oldest <= start:
            break
        cursor = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    path = DAILY_DIR / f"{code}.csv"
    dates = sorted(d for d in rows if d >= start)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume", "value"])
        for dt in dates:
            r = rows[dt]
            w.writerow([dt, r.get("stck_oprc"), r.get("stck_hgpr"), r.get("stck_lwpr"),
                        r.get("stck_clpr"), r.get("acml_vol"), r.get("acml_tr_pbmn")])
    return {"code": code, "rows": len(dates),
            "first": dates[0] if dates else None, "last": dates[-1] if dates else None}


def download_basket(c: KisClient, codes: dict[str, str], years: int) -> str:
    end = datetime.now(KST)
    start = (end - timedelta(days=int(years * 365.25))).strftime("%Y%m%d")
    end_s = end.strftime("%Y%m%d")
    L = [f"=== 일봉 히스토리 다운로드 ({start}~{end_s}, ~{years}년) ==="]
    for code, name in codes.items():
        try:
            r = download(c, code, start, end_s)
            L.append(f"  {code} {name:14}: {r['rows']:>4}일  {r['first']}~{r['last']}")
        except Exception as e:
            L.append(f"  {code} {name}: 오류 {e}")
    L.append(f"저장 위치: data/daily/<code>.csv")
    return "\n".join(L)
