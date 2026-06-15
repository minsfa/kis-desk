"""일별 투자자 순매수(개인/기관/외국인) 다운로더.

KIS inquire-investor(FHKST01010900): 종목별 최근 ~30영업일 투자자 매매동향.
필드: 개인/외국인/기관 각각 순매수수량(ntby_qty)·순매수금액(ntby_tr_pbmn)·매수량·매도량.
※ 이 API는 최근 30일 윈도우만 제공 → 장기 이력은 매일 누적해 쌓아야 함.
"""
from __future__ import annotations
import csv
import time
from datetime import datetime, timedelta, timezone

from .client import KisClient
from .config import PROJECT_ROOT
from .daily import VALUE_BASKET  # 동일 유니버스 재사용

KST = timezone(timedelta(hours=9))
INV_DIR = PROJECT_ROOT / "data" / "investor"
HIST_DIR = PROJECT_ROOT / "data" / "investor_history"
INV_PATH = "/uapi/domestic-stock/v1/quotations/inquire-investor"
HCOLS = ["date", "close", "indiv_netqty", "foreign_netqty", "inst_netqty",
         "indiv_netval", "foreign_netval", "inst_netval"]


def fetch(c: KisClient, code: str) -> list[dict]:
    for attempt in range(5):
        d = c.get(INV_PATH, "FHKST01010900",
                  {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
        if str(d.get("rt_cd")) == "0":
            return d.get("output") or []
        time.sleep(0.4 + attempt * 0.3)
    return []


def download(c: KisClient, code: str) -> dict:
    rows = [r for r in fetch(c, code) if r.get("stck_bsop_date")
            and r.get("prsn_ntby_qty") not in (None, "")]
    rows.sort(key=lambda r: r["stck_bsop_date"])
    INV_DIR.mkdir(parents=True, exist_ok=True)
    path = INV_DIR / f"{code}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "close", "indiv_netqty", "foreign_netqty", "inst_netqty",
                    "indiv_netval", "foreign_netval", "inst_netval"])
        for r in rows:
            w.writerow([r["stck_bsop_date"], r.get("stck_clpr"),
                        r.get("prsn_ntby_qty"), r.get("frgn_ntby_qty"), r.get("orgn_ntby_qty"),
                        r.get("prsn_ntby_tr_pbmn"), r.get("frgn_ntby_tr_pbmn"),
                        r.get("orgn_ntby_tr_pbmn")])
    return {"code": code, "rows": len(rows),
            "first": rows[0]["stck_bsop_date"] if rows else None,
            "last": rows[-1]["stck_bsop_date"] if rows else None}


def accumulate(c: KisClient, codes: dict[str, str]) -> str:
    """매일 30일창을 받아 data/investor_history/<code>.csv 에 날짜기준 병합 누적.
    누적이 길어지면 외국인 매도->매수 전환을 추세로 포착."""
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    added_total = 0
    turns = []
    for code, name in codes.items():
        path = HIST_DIR / f"{code}.csv"
        rows: dict[str, list] = {}
        if path.exists():
            with open(path, encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    rows[r["date"]] = [r.get(k, "") for k in HCOLS]
        before = len(rows)
        for r in fetch(c, code):
            d = r.get("stck_bsop_date")
            if d and r.get("prsn_ntby_qty") not in (None, ""):
                rows[d] = [d, r.get("stck_clpr"), r.get("prsn_ntby_qty"),
                           r.get("frgn_ntby_qty"), r.get("orgn_ntby_qty"),
                           r.get("prsn_ntby_tr_pbmn"), r.get("frgn_ntby_tr_pbmn"),
                           r.get("orgn_ntby_tr_pbmn")]
        added = len(rows) - before
        added_total += added
        dates = sorted(rows)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(HCOLS)
            for d in dates:
                w.writerow(rows[d])
        # 전환 감지: 외국인 순매수량 최근5일 vs 직전5일
        fq = []
        for d in dates:
            try: fq.append(float(rows[d][3] or 0))
            except: fq.append(0.0)
        if len(fq) >= 10:
            rec, pri = sum(fq[-5:]), sum(fq[-10:-5])
            if rec > 0 and pri < 0:
                turns.append(name)
    msg = (f"[수급누적] {datetime.now(KST):%Y-%m-%d %H:%M} · {len(codes)}종목 · "
           f"신규 {added_total}행 적재 (data/investor_history/)")
    if turns:
        msg += f"\n⚡외국인 매수전환 신호: {', '.join(turns)}"
    return msg


def download_basket(c: KisClient, codes: dict[str, str]) -> str:
    L = ["=== 일별 투자자 순매수 다운로드 (개인/외국인/기관, 최근 ~30일) ==="]
    for code, name in codes.items():
        try:
            r = download(c, code)
            L.append(f"  {code} {name:14}: {r['rows']:>3}일  {r['first']}~{r['last']}")
        except Exception as e:
            L.append(f"  {code} {name}: 오류 {e}")
    L.append("저장 위치: data/investor/<code>.csv")
    return "\n".join(L)
