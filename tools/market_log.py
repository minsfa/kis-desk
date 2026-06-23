"""국내 증시 빚투·과열 관찰용 일별 데이터 로거.

⚠️ 종합 '지수'를 만드는 도구가 아니다. 개별 지표를 매일 CSV에 누적하면서
KOSPI·삼성전자·SK하이닉스 등락률과 나란히 비교 관찰하는 게 목적이다.
데이터가 충분히 쌓이면 그때 패턴을 보고 지수화 여부를 판단한다.
지금은 정확히 수집·저장·조회만 잘 되면 된다.

독립 실행 스크립트(src/ 밖, 라이브 매매 게이트와 무관, KIS 호출은 전부 읽기전용).

사용법:
  python tools/market_log.py            # = log: 오늘(최신 영업일) 1행 append/갱신
  python tools/market_log.py --show [N]  # 최근 N영업일(기본 15) 표 출력
  python tools/market_log.py --compare   # 각 지표 vs 등락률(부호/상관) 거친 비교
  python tools/market_log.py --backfill  # 초기 1회용, 최근 ~60영업일 채움

데이터 소스(전부 읽기전용):
  - 신용거래융자 잔고: KOFIA FreeSIS (tools/credit_check.py 재사용, 단위 조원)
  - 삼성/하이닉스 종가·등락률·거래대금: KIS 일봉 inquire-daily-itemchartprice
  - KOSPI 지수·등락률: KIS 업종 inquire-index-price / inquire-index-daily-price
  - KOSPI 시총·거래대금·회전율·반도체쏠림: KRX OPEN API stk_bydd_trd (tools/krx_api.py)

칼럼(data/market/daily.csv):
  1단계:
    date, kospi, kospi_ret, s_close, s_ret, s_value, h_close, h_ret, h_value,
    credit_all, credit_kospi, credit_kosdaq
  2단계(KRX OPEN API 로 채움):
    kospi_mktcap, kospi_value, turnover, credit_ratio, semi_val_share, pension_net
    - kospi_mktcap = Σ MKTCAP(KOSPI), 원
    - kospi_value  = Σ ACC_TRDVAL(KOSPI), 원
    - turnover     = kospi_value / kospi_mktcap × 100 (회전율 %)
    - credit_ratio = credit_kospi(조원→원) / kospi_mktcap × 100 (신용/시총 %, 같은 basDd)
    - semi_val_share = (삼성+하이닉스 거래대금) / kospi_value × 100 (반도체 쏠림 %)
    - pension_net  = 공란(투자자별 API 는 OPEN API 승인 범위 밖)
    ⚠️ KRX 는 전일치가 익일 08시 갱신 — 당일/주말/휴일은 빈 배열 → 2단계 공란(graceful).

단위:
  - 신용잔고(credit_*): 조원  (credit_check.py 와 동일)
  - 거래대금(s_value, h_value): 원  (KIS acml_tr_pbmn 원본 단위)
  - 등락률(*_ret): %
  - kospi: 지수 포인트
fail-soft: 한 소스가 실패해도 나머지 칼럼은 채우고, 실패한 칼럼만 공란 + 경고.
"""
from __future__ import annotations

import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# repo 루트를 path 에 추가 (src.* 및 tools.* import 용)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.credit_check import fetch_rows as _credit_fetch_rows  # KOFIA 재사용
from tools import krx_api  # KRX OPEN API (2단계 칼럼)

KST = timezone(timedelta(hours=9))
DATA_DIR = ROOT / "data" / "market"
CSV_PATH = DATA_DIR / "daily.csv"

SAMSUNG = "005930"
HYNIX = "000660"
KOSPI_IDX = "0001"  # 업종지수 코드(코스피 종합)

# CSV 칼럼 스키마 — 처음부터 전부 써두고, 2단계 칼럼은 값만 공란.
# (스키마가 나중에 안 바뀌게 고정)
COLUMNS = [
    # 1단계: 지금 채움
    "date", "kospi", "kospi_ret",
    "s_close", "s_ret", "s_value",
    "h_close", "h_ret", "h_value",
    "credit_all", "credit_kospi", "credit_kosdaq",
    # 2단계: KRX 계정 後 채움 (지금은 공란, 자리만)
    "kospi_mktcap", "kospi_value", "turnover",
    "credit_ratio", "semi_val_share", "pension_net",
]
STAGE2 = {"kospi_mktcap", "kospi_value", "turnover",
          "credit_ratio", "semi_val_share", "pension_net"}


# ─────────────────────────────────────────────────────────────────────────
# KIS 클라이언트 (지연 로딩 — --show/--compare 는 CSV만 읽으므로 KIS 불필요)
# ─────────────────────────────────────────────────────────────────────────
def _kis_client():
    from src.kis.config import load_settings
    from src.kis.client import KisClient
    return KisClient(load_settings("prod"))


def _f(v):
    """문자열 숫자 → float, 실패 시 None."""
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return None


# ─────────────────────────────────────────────────────────────────────────
# KIS 시세 조회 (전부 읽기전용)
# ─────────────────────────────────────────────────────────────────────────
KR_CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
IDX_PRICE = "/uapi/domestic-stock/v1/quotations/inquire-index-price"
IDX_DAILY = "/uapi/domestic-stock/v1/quotations/inquire-index-daily-price"


def fetch_stock_daily(c, code: str, d1: str, d2: str) -> dict[str, dict]:
    """KIS 일봉 — {YYYYMMDD: {"close","value","ret"}}.
    ret(등락률 %)는 직전 영업일 종가 대비로 계산(output2 에 ctrt 없음).
    거래대금(value)은 원 단위(acml_tr_pbmn 원본)."""
    d = c.get(KR_CHART, "FHKST03010100", {
        "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": d1, "FID_INPUT_DATE_2": d2,
        "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0",
    })
    out = [r for r in (d.get("output2") or []) if r.get("stck_bsop_date")]
    # 과거 → 최신 순으로 정렬해 직전 종가로 등락률 계산
    out.sort(key=lambda r: r["stck_bsop_date"])
    res: dict[str, dict] = {}
    prev_close = None
    for r in out:
        dt = r["stck_bsop_date"]
        close = _f(r.get("stck_clpr"))
        value = _f(r.get("acml_tr_pbmn"))  # 거래대금(원)
        ret = None
        if close is not None and prev_close not in (None, 0):
            ret = (close / prev_close - 1) * 100
        res[dt] = {"close": close, "value": value, "ret": ret}
        if close:
            prev_close = close
    return res


def fetch_kospi_daily(c, d1: str, d2: str) -> dict[str, dict]:
    """KIS 업종 일별지수 — {YYYYMMDD: {"kospi","ret"}}. 등락률은 API 제공값.
    ⚠️ 이 지수 API 는 종목 일봉과 달리 FID_INPUT_DATE_1(최신 기준일)에서
    과거로 최대 100영업일을 돌려준다. 그래서 최신 기준일을 DATE_1 에 둔다."""
    later, earlier = (d1, d2) if d1 >= d2 else (d2, d1)
    d = c.get(IDX_DAILY, "FHPUP02120000", {
        "FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": KOSPI_IDX,
        "FID_INPUT_DATE_1": later, "FID_INPUT_DATE_2": earlier,
        "FID_PERIOD_DIV_CODE": "D",
    })
    out = [r for r in (d.get("output2") or []) if r.get("stck_bsop_date")]
    res: dict[str, dict] = {}
    for r in out:
        res[r["stck_bsop_date"]] = {
            "kospi": _f(r.get("bstp_nmix_prpr")),
            "ret": _f(r.get("bstp_nmix_prdy_ctrt")),
        }
    return res


def fetch_kospi_now(c) -> dict | None:
    """KIS 업종 현재지수(코스피 종합) — {"date","kospi","ret"}."""
    d = c.get(IDX_PRICE, "FHPUP02100000",
              {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": KOSPI_IDX})
    o = d.get("output") or {}
    if not o:
        return None
    return {
        "date": datetime.now(KST).strftime("%Y%m%d"),
        "kospi": _f(o.get("bstp_nmix_prpr")),
        "ret": _f(o.get("bstp_nmix_prdy_ctrt")),
    }


# ─────────────────────────────────────────────────────────────────────────
# CSV 입출력
# ─────────────────────────────────────────────────────────────────────────
def _read_csv() -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def _write_csv(rows: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: r.get("date", ""))
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            # 스키마에 없는 키는 버리고, 빠진 칼럼은 공란으로
            w.writerow({k: r.get(k, "") for k in COLUMNS})


def _blank_row(date: str) -> dict:
    row = {k: "" for k in COLUMNS}
    row["date"] = date
    return row


def _merge_into(rows_by_date: dict[str, dict], date: str, **vals):
    """date 행에 1단계 값 병합(None/빈값은 덮어쓰지 않음). 2단계 칼럼은 건드리지 않음."""
    row = rows_by_date.setdefault(date, _blank_row(date))
    for k, v in vals.items():
        if k in STAGE2:
            continue
        if v is None or v == "":
            continue
        row[k] = v


def _merge_stage2(rows_by_date: dict[str, dict], date: str, **vals):
    """date 행에 2단계(KRX) 값 병합. STAGE2 칼럼만 허용, None/빈값은 스킵."""
    row = rows_by_date.setdefault(date, _blank_row(date))
    for k, v in vals.items():
        if k not in STAGE2:
            continue
        if v is None or v == "":
            continue
        row[k] = v


# ─────────────────────────────────────────────────────────────────────────
# 수집 (log / backfill 공용)
# ─────────────────────────────────────────────────────────────────────────
def _collect(days: int) -> tuple[dict[str, dict], list[str]]:
    """최근 days 영업일치 1단계 데이터 수집. (rows_by_date, 경고목록) 반환.
    fail-soft: 소스별 try/except, 실패해도 나머지는 채움."""
    warns: list[str] = []
    rows_by_date: dict[str, dict] = {}

    end = datetime.now(KST)
    d2 = end.strftime("%Y%m%d")
    # 영업일 ≈ 달력일 * 0.7. 여유 있게 잡음.
    d1 = (end - timedelta(days=int(days * 1.6) + 10)).strftime("%Y%m%d")

    c = None
    try:
        c = _kis_client()
    except Exception as e:
        warns.append(f"KIS 클라이언트 생성 실패 — kospi/삼성/하이닉스 공란: {str(e)[:80]}")

    # 1) 삼성전자 일봉
    if c is not None:
        try:
            sam = fetch_stock_daily(c, SAMSUNG, d1, d2)
            for dt, v in sam.items():
                _merge_into(rows_by_date, dt, s_close=_round(v["close"]),
                            s_ret=_round(v["ret"], 2), s_value=_int(v["value"]))
        except Exception as e:
            warns.append(f"삼성전자 일봉 실패 — s_* 공란: {str(e)[:80]}")
        # 2) SK하이닉스 일봉
        try:
            hy = fetch_stock_daily(c, HYNIX, d1, d2)
            for dt, v in hy.items():
                _merge_into(rows_by_date, dt, h_close=_round(v["close"]),
                            h_ret=_round(v["ret"], 2), h_value=_int(v["value"]))
        except Exception as e:
            warns.append(f"SK하이닉스 일봉 실패 — h_* 공란: {str(e)[:80]}")
        # 3) KOSPI 업종 일별지수
        try:
            ks = fetch_kospi_daily(c, d1, d2)
            for dt, v in ks.items():
                _merge_into(rows_by_date, dt, kospi=_round(v["kospi"], 2),
                            kospi_ret=_round(v["ret"], 2))
        except Exception as e:
            warns.append(f"KOSPI 일별지수 실패 — kospi/kospi_ret 공란: {str(e)[:80]}")

    # 4) KOFIA 신용거래융자 잔고 (조원)
    try:
        credit = _credit_fetch_rows(days_back=max(days, 20))
        for r in credit:
            _merge_into(rows_by_date, r["date"],
                        credit_all=_round(r["total"], 4),
                        credit_kospi=_round(r["kospi"], 4),
                        credit_kosdaq=_round(r["kosdaq"], 4))
    except Exception as e:
        warns.append(f"KOFIA 신용잔고 실패 — credit_* 공란: {str(e)[:80]}")

    # 요청한 영업일 수에 맞춰 윈도우 컷오프(소스별 반환 길이가 달라 정렬 통일).
    cutoff = (end - timedelta(days=int(days * 1.6) + 10)).strftime("%Y%m%d")
    rows_by_date = {d: r for d, r in rows_by_date.items() if d >= cutoff}

    # 5) KRX OPEN API — 2단계(KOSPI 시총/거래대금/회전율/반도체 쏠림) + credit_ratio.
    #    이미 모인 영업일들에 대해서만 날짜별 호출(전 종목 1일치). 빈 배열(주말/당일/
    #    미갱신)은 graceful 스킵. 같은 basDd 의 신용·시총으로 credit_ratio 계산.
    try:
        key = krx_api._auth_key()
    except Exception:
        key = None
    if not key:
        warns.append("KRX_AUTH_KEY 없음 — 2단계(KRX) 칼럼 공란.")
    else:
        krx_fail = 0
        for dt in sorted(rows_by_date):
            try:
                m = krx_api.fetch_kospi_metrics(dt, key=key)
            except Exception as e:
                krx_fail += 1
                warns.append(f"KRX {dt} 실패 — 해당 날짜 2단계 공란: {str(e)[:60]}")
                continue
            if not m:  # 빈 배열(주말/휴일/당일/미갱신) → 공란 유지
                continue
            _merge_stage2(rows_by_date, dt,
                          kospi_mktcap=_round(m["kospi_mktcap"]),
                          kospi_value=_round(m["kospi_value"]),
                          turnover=_round(m["turnover"], 3),
                          semi_val_share=_round(m["semi_val_share"], 2))
            # credit_ratio = 신용(코스피, 조원→원) / 시총(원) × 100 — 같은 basDd 기준
            row = rows_by_date.get(dt, {})
            ck = _f(row.get("credit_kospi"))  # 조원
            mc = m["kospi_mktcap"]            # 원
            if ck is not None and mc:
                _merge_stage2(rows_by_date, dt,
                              credit_ratio=_round(ck * 1e12 / mc * 100, 3))
            # pension_net 은 투자자별 API 가 OPEN API 에 없어 공란 유지.
        if krx_fail:
            warns.append(f"KRX 실패 {krx_fail}건 — 해당 날짜만 공란, 나머지는 채움.")

    return rows_by_date, warns


def _round(v, n: int = 0):
    if v is None:
        return ""
    return round(v, n) if n else int(round(v))


def _int(v):
    if v is None:
        return ""
    return int(round(v))


# ─────────────────────────────────────────────────────────────────────────
# 명령: log / backfill
# ─────────────────────────────────────────────────────────────────────────
def cmd_log():
    """오늘(최신 영업일) 1행 append/갱신. 이미 있으면 갱신, 신규면 추가."""
    rows = _read_csv()
    existing = {r["date"]: r for r in rows}

    collected, warns = _collect(days=12)  # 최신 영업일 + 직전 종가 계산용 여유

    # KOSPI 일별지수에 오늘이 아직 없을 수 있으니 현재지수로 보강
    try:
        c = _kis_client()
        now = fetch_kospi_now(c)
        if now and now["kospi"]:
            _merge_into(collected, now["date"], kospi=_round(now["kospi"], 2),
                        kospi_ret=_round(now["ret"], 2))
    except Exception:
        pass  # 일별지수로 이미 채워졌으면 OK

    if not collected:
        print("로그 실패 — 수집된 데이터 없음.")
        for w in warns:
            print(f"  ⚠️ {w}")
        return

    # 가장 최신 날짜 1행만 대상으로 함 (log 의 본분)
    target = max(collected)
    new_row = collected[target]

    if target in existing:
        # 기존 행 갱신 (1·2단계 모두 — 새 값이 있을 때만 덮어씀)
        old = existing[target]
        changed = []
        for k in COLUMNS:
            nv = new_row.get(k, "")
            if nv not in (None, "") and str(old.get(k, "")) != str(nv):
                old[k] = nv
                changed.append(k)
        msg = f"갱신({len(changed)}칼럼)" if changed else "변경없음(스킵)"
        print(f"[{_fmt_date(target)}] 기존 행 {msg}.")
    else:
        rows.append(new_row)
        print(f"[{_fmt_date(target)}] 신규 행 추가.")

    _write_csv(rows)
    print(f"저장: {CSV_PATH}  (총 {len(rows)}행)")
    for w in warns:
        print(f"  ⚠️ {w}")


def cmd_backfill():
    """초기 1회용 — 최근 ~60영업일치를 채워 CSV 에 병합."""
    rows = _read_csv()
    existing = {r["date"]: r for r in rows}

    collected, warns = _collect(days=60)
    if not collected:
        print("백필 실패 — 수집된 데이터 없음.")
        for w in warns:
            print(f"  ⚠️ {w}")
        return

    added, updated = 0, 0
    for dt, new_row in collected.items():
        if dt in existing:
            old = existing[dt]
            ch = False
            for k in COLUMNS:
                nv = new_row.get(k, "")
                if nv not in (None, "") and str(old.get(k, "")) != str(nv):
                    old[k] = nv
                    ch = True
            updated += 1 if ch else 0
        else:
            rows.append(new_row)
            existing[dt] = new_row
            added += 1

    _write_csv(rows)
    print(f"백필 완료: 신규 {added}행 / 갱신 {updated}행 (총 {len(rows)}행)")
    print(f"저장: {CSV_PATH}")
    for w in warns:
        print(f"  ⚠️ {w}")


# ─────────────────────────────────────────────────────────────────────────
# 명령: show
# ─────────────────────────────────────────────────────────────────────────
def _fmt_date(s: str) -> str:
    return f"{s[:4]}/{s[4:6]}/{s[6:8]}" if len(s) == 8 else s


def _val_eok(v) -> str:
    """거래대금(원) → 억/조 단위 보기 좋게."""
    f = _f(v)
    if f is None:
        return "—"
    jo = f / 1e12
    if abs(jo) >= 1:
        return f"{jo:,.2f}조"
    return f"{f / 1e8:,.0f}억"


def _ret_s(v) -> str:
    f = _f(v)
    return f"{f:+.2f}%" if f is not None else "—"


def _num_s(v, n: int = 2) -> str:
    f = _f(v)
    if f is None:
        return "—"
    return f"{f:,.{n}f}"


def _won_jo(v) -> str:
    """원 단위 큰 값(시총/거래대금) → 조원 환산 표기."""
    f = _f(v)
    if f is None:
        return "—"
    return f"{f / 1e12:,.2f}"


def _pct_s(v, n: int = 2) -> str:
    f = _f(v)
    if f is None:
        return "—"
    return f"{f:.{n}f}"


def cmd_show(n: int):
    rows = _read_csv()
    if not rows:
        print("데이터 없음 — 먼저 --backfill 또는 log 실행.")
        return
    recent = rows[-n:]
    print(f"국내 증시 일별 관찰 데이터 (최근 {len(recent)}영업일)")
    print("  거래대금=원기준 억/조 환산, 신용잔고=조원, 등락률=%\n")
    hdr = (f"{'일자':>11} {'KOSPI':>9} {'KOSPI%':>8} │ "
           f"{'삼성종가':>8} {'삼성%':>7} {'삼성대금':>10} │ "
           f"{'하이닉스':>9} {'하닉%':>7} {'하닉대금':>10} │ "
           f"{'신용전체':>8} {'코스피':>7} {'코스닥':>7}")
    print(hdr)
    print("─" * len(hdr))
    for r in recent:
        print(f"{_fmt_date(r['date']):>11} "
              f"{_num_s(r.get('kospi')):>9} {_ret_s(r.get('kospi_ret')):>8} │ "
              f"{_num_s(r.get('s_close'), 0):>8} {_ret_s(r.get('s_ret')):>7} "
              f"{_val_eok(r.get('s_value')):>10} │ "
              f"{_num_s(r.get('h_close'), 0):>9} {_ret_s(r.get('h_ret')):>7} "
              f"{_val_eok(r.get('h_value')):>10} │ "
              f"{_num_s(r.get('credit_all')):>8} {_num_s(r.get('credit_kospi')):>7} "
              f"{_num_s(r.get('credit_kosdaq')):>7}")

    # ── 2단계(KRX) 지표 표 ──────────────────────────────────────────────
    print("\n[2단계 / KRX OPEN API] KOSPI 시총·거래대금=조원, 회전율·신용비중·반도체쏠림=%")
    h2 = (f"{'일자':>11} {'시총(조)':>10} {'거래대금(조)':>12} {'회전율%':>8} "
          f"{'신용/시총%':>10} {'반도체쏠림%':>11} {'연기금':>7}")
    print(h2)
    print("─" * len(h2))
    for r in recent:
        print(f"{_fmt_date(r['date']):>11} "
              f"{_won_jo(r.get('kospi_mktcap')):>10} "
              f"{_won_jo(r.get('kospi_value')):>12} "
              f"{_pct_s(r.get('turnover'), 3):>8} "
              f"{_pct_s(r.get('credit_ratio'), 3):>10} "
              f"{_pct_s(r.get('semi_val_share'), 2):>11} "
              f"{_num_s(r.get('pension_net')) if r.get('pension_net') else '—':>7}")
    print("\n※ pension_net(연기금 순매수)·레버리지ETF 는 투자자별/ETF 전용 API 가 "
          "현재 KRX OPEN API 승인 범위 밖 → 공란.")


# ─────────────────────────────────────────────────────────────────────────
# 명령: compare
# ─────────────────────────────────────────────────────────────────────────
def _pearson(xs: list[float], ys: list[float]) -> float | None:
    """피어슨 상관. 표준편차 0 이거나 표본<3 이면 None."""
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx <= 0 or syy <= 0:
        return None
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _series_diff(rows: list[dict], col: str) -> list[tuple[str, float | None]]:
    """전일대비 증감(부호 비교용). [(date, diff)] — 첫 행은 None."""
    out: list[tuple[str, float | None]] = []
    prev = None
    for r in rows:
        cur = _f(r.get(col))
        diff = (cur - prev) if (cur is not None and prev is not None) else None
        out.append((r["date"], diff))
        if cur is not None:
            prev = cur
    return out


def cmd_compare():
    rows = _read_csv()
    n = len(rows)
    if n < 4:
        print(f"데이터 부족({n}행) — 축적 중. 비교는 4행 이상부터.")
        return

    print(f"빚투·과열 지표 vs 가격 등락 비교 (총 {n}행)\n")

    rets = {"kospi_ret": "KOSPI", "s_ret": "삼성", "h_ret": "하이닉스"}

    # 충분(>20행)하면 피어슨 상관, 아니면 부호 일치 비교
    if n > 20:
        print(f"[1] 피어슨 상관 — 각 지표의 '전일대비 증감' vs 세 가격 등락률 ({n}행)")
        # 지표: 신용 전체/코스피/코스닥, 삼성·하이닉스 거래대금
        metrics = {
            "credit_all": "신용잔고(전체) Δ",
            "credit_kospi": "신용잔고(코스피) Δ",
            "credit_kosdaq": "신용잔고(코스닥) Δ",
            "s_value": "삼성 거래대금 Δ",
            "h_value": "하이닉스 거래대금 Δ",
            # 2단계(KRX) 지표 — 전일대비 증감 기준
            "kospi_value": "KOSPI 거래대금 Δ",
            "turnover": "회전율 Δ",
            "credit_ratio": "신용/시총 Δ",
            "semi_val_share": "반도체쏠림 Δ",
        }
        print(f"  {'지표':<20} " + " ".join(f"{lbl:>9}" for lbl in rets.values()))
        for col, label in metrics.items():
            diffs = dict(_series_diff(rows, col))
            line = f"  {label:<20} "
            for rcol in rets:
                xs, ys = [], []
                for r in rows:
                    dv = diffs.get(r["date"])
                    rv = _f(r.get(rcol))
                    if dv is not None and rv is not None:
                        xs.append(dv)
                        ys.append(rv)
                corr = _pearson(xs, ys)
                line += f"{('%+.2f' % corr) if corr is not None else '—':>9} "
            print(line)
        print("\n  (양수=같은 방향, 음수=반대 방향. 표본 적은 칼럼은 — 표시)")
    else:
        print(f"[1] 부호 일치 비교 — 데이터 적음({n}행, 상관은 20행 초과부터). "
              "지표 증감 부호 vs 가격 등락 부호 최근 행 표시")
        # 최근 행들에서 신용잔고 증감 부호와 등락 부호를 같은 줄에
        credit_diff = dict(_series_diff(rows, "credit_all"))
        print(f"\n  {'일자':>11} {'신용Δ(조)':>10} {'KOSPI%':>8} {'삼성%':>7} {'하닉%':>7}")
        for r in rows[-min(n, 12):]:
            cd = credit_diff.get(r["date"])
            cd_s = f"{cd:+.3f}" if cd is not None else "—"
            print(f"  {_fmt_date(r['date']):>11} {cd_s:>10} "
                  f"{_ret_s(r.get('kospi_ret')):>8} {_ret_s(r.get('s_ret')):>7} "
                  f"{_ret_s(r.get('h_ret')):>7}")

    # 최근 신용잔고 추이 한 줄 요약 (방향성)
    cd = [(_fmt_date(d), v) for d, v in _series_diff(rows, "credit_all") if v is not None]
    if cd:
        last = cd[-1]
        up = sum(1 for _, v in cd[-5:] if v > 0)
        print(f"\n[2] 최근 신용잔고 방향: 마지막 {last[0]} {last[1]:+.3f}조, "
              f"최근5행 중 {up}일 증가.")


# ─────────────────────────────────────────────────────────────────────────
def _parse_show_n(argv: list[str]) -> int:
    i = argv.index("--show")
    if i + 1 < len(argv):
        try:
            return int(argv[i + 1])
        except ValueError:
            pass
    return 15


def main():
    argv = sys.argv[1:]
    if "--show" in argv:
        cmd_show(_parse_show_n(argv))
    elif "--compare" in argv:
        cmd_compare()
    elif "--backfill" in argv:
        cmd_backfill()
    else:
        cmd_log()  # 기본 = log


if __name__ == "__main__":
    main()
