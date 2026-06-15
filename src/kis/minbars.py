"""분봉 수집기 — 마감 후(또는 장중) 하루치 1분봉 OHLCV 전체를 끌어와 종목별 CSV에 누적.

목적: 일중 패턴(오전 저점→점심 전 반등 등) 검증용 시계열 축적. 읽기 전용(주문 없음).
KIS inquire-time-itemchartprice(FHKST03010200)는 콜당 30분치만 주므로, 마감 15:30부터
09:00까지 30분 단위로 시각을 거슬러 호출해 하루 전체(~390분)를 모은다.
저장: data/minbars/<code>.csv  (date,time,open,high,low,close,vol)  — (date,time) 유니크 누적.
"""
from __future__ import annotations
import csv
from datetime import datetime, timezone, timedelta

from .client import KisClient
from .config import PROJECT_ROOT

KST = timezone(timedelta(hours=9))
DIR = PROJECT_ROOT / "data" / "minbars"
TCHART = "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"

# 기본 감시군 — 삼성/SK 반도체·부품 대형주 복합체 + TIGER200IT레버리지(레버리지 표현)
# 005930 삼성전자 · 000660 SK하이닉스 · 009150 삼성전기 · 034730 SK · 243880 IT레버
WATCH = ["005930", "000660", "009150", "034730", "243880"]
COLS = ["date", "time", "open", "high", "low", "close", "vol"]


def fetch_day(c: KisClient, code: str, date: str | None = None, premarket: bool = True) -> list[dict]:
    """당일 1분봉 전체. date=YYYYMMDD(미지정=오늘). 정규장(J)+프리마켓(UN, 08:00~).
    ⚠️ 프리마켓(08:xx)은 '당일에만' 조회됨 — 과거 백필 불가, 같은 날 수집해야 함."""
    bars: dict[str, dict] = {}

    def pull(mkt, anchors):
        for hhmm in anchors:
            params = {"FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": mkt,
                      "FID_INPUT_ISCD": code, "FID_INPUT_HOUR_1": f"{hhmm}00",
                      "FID_PW_DATA_INCU_YN": "Y"}
            try:
                d = c.get(TCHART, "FHKST03010200", params)
            except Exception:
                continue
            for r in d.get("output2") or []:
                dt = r.get("stck_bsop_date"); tm = r.get("stck_cntg_hour")
                if not dt or not tm or (date and dt != date):
                    continue
                bars[f"{dt}{tm}"] = {
                    "date": dt, "time": tm,
                    "open": r.get("stck_oprc"), "high": r.get("stck_hgpr"),
                    "low": r.get("stck_lwpr"), "close": r.get("stck_prpr"),
                    "vol": r.get("cntg_vol"),
                }

    # 정규장(KRX) 15:30→09:01
    pull("J", ["1530", "1500", "1430", "1400", "1330", "1300",
               "1230", "1200", "1130", "1100", "1030", "1000", "0930", "0901"])
    # 프리마켓(NXT 통합) 09:00→08:00 — ETF(NXT 미거래)는 빈 응답
    if premarket:
        pull("UN", ["0900", "0830", "0810"])
    return [bars[k] for k in sorted(bars)]


HIST = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"


def fetch_day_hist(c: KisClient, code: str, date: str) -> list[dict]:
    """과거 특정일 1분봉 전체. FHKST03010230(콜당 120분)을 4구간 앵커로 수집."""
    bars: dict[str, dict] = {}
    for anchor in ("153000", "133000", "113000", "093000"):
        params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                  "FID_INPUT_DATE_1": date, "FID_INPUT_HOUR_1": anchor,
                  "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": ""}
        try:
            d = c.get(HIST, "FHKST03010230", params)
        except Exception:
            continue
        for r in d.get("output2") or []:
            dt = r.get("stck_bsop_date"); tm = r.get("stck_cntg_hour")
            if not dt or not tm or dt != date:
                continue
            bars[f"{dt}{tm}"] = {
                "date": dt, "time": tm,
                "open": r.get("stck_oprc"), "high": r.get("stck_hgpr"),
                "low": r.get("stck_lwpr"), "close": r.get("stck_prpr"),
                "vol": r.get("cntg_vol"),
            }
    return [bars[k] for k in sorted(bars)]


def backfill(c: KisClient, codes: list[str] | None = None, days: int = 32) -> dict:
    """오늘부터 days 일 거슬러 평일 분봉을 누적(휴장일은 빈 응답→스킵)."""
    codes = codes or WATCH
    DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(KST)
    out = {}
    for code in codes:
        have = _load_keys(code)
        appended = 0; daysfilled = set()
        rows_all = []
        for back in range(days + 1):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:        # 주말 스킵
                continue
            ds = day.strftime("%Y%m%d")
            for r in fetch_day_hist(c, code, ds):
                k = f"{r['date']}{r['time']}"
                if k not in have:
                    have.add(k); rows_all.append(r); appended += 1; daysfilled.add(r["date"])
        rows_all.sort(key=lambda r: (r["date"], r["time"]))
        p = _path(code)
        write_header = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            if write_header:
                w.writeheader()
            for r in rows_all:
                w.writerow(r)
        out[code] = {"appended": appended, "days": len(daysfilled), "file": str(p)}
    return out


def _path(code: str):
    return DIR / f"{code}.csv"


def _load_keys(code: str) -> set[str]:
    p = _path(code)
    keys = set()
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                keys.add(f"{row['date']}{row['time']}")
    return keys


def collect(c: KisClient, codes: list[str] | None = None, date: str | None = None) -> dict:
    """codes 각 종목의 하루치 분봉을 끌어와 종목별 CSV에 누적(중복 (date,time) 스킵)."""
    codes = codes or WATCH
    DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for code in codes:
        rows = fetch_day(c, code, date)
        have = _load_keys(code)
        new = [r for r in rows if f"{r['date']}{r['time']}" not in have]
        p = _path(code)
        write_header = not p.exists()
        with open(p, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLS)
            if write_header:
                w.writeheader()
            for r in new:
                w.writerow(r)
        out[code] = {"fetched": len(rows), "appended": len(new), "total_keys": len(have) + len(new),
                     "file": str(p)}
    return out


def load(code: str, date: str | None = None) -> list[dict]:
    """저장된 분봉 읽기(특정일 또는 전체). 분석용."""
    p = _path(code)
    if not p.exists():
        return []
    rows = []
    with open(p, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if date and row["date"] != date:
                continue
            rows.append(row)
    return rows
