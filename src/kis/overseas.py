"""해외(미국) 주식 — 현재가/잔고/매수가능/주문/체결. USD 기준.

국내 모듈과 같은 안전게이트·로깅 사용. 거래소 코드:
  주문·잔고: OVRS_EXCG_CD = NASD(나스닥) / NYSE / AMEX(=NYSE Arca, 대부분 ETF)
  현재가:    EXCD       = NAS / NYS / AMS
미국 정규장(약 22:30~05:00 KST) 외에는 주문이 거부/예약될 수 있음.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

from .client import KisClient
from .config import TR_US_PRICE, US_EXCH, US_EXCH_DAY
from .safety import check_order_usd, bump_count
from .logging_util import log_event

ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
BAL_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
PSAMT_PATH = "/uapi/overseas-stock/v1/trading/inquire-psamount"
CCNL_PATH = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
PRICE_PATH = "/uapi/overseas-price/v1/quotations/price"
KST = timezone(timedelta(hours=9))


def us_session(now: datetime | None = None) -> str:
    """현재 KST 기준 미국 주문 세션.
      'day' = 주간거래(10:00~16:00 KST, 주간 tr_id TTTS603xU, 지정가만, 일부종목만)
      'reg' = 프리/정규/애프터(17:00~익일09:00, 정규 tr_id TTTT100xU)
      'closed' = 그 외 틈(09:00~10:00, 16:00~17:00) — 주문 거부될 수 있음
    """
    n = now or datetime.now(KST)
    hm = n.hour * 60 + n.minute
    if 10 * 60 <= hm < 16 * 60:
        return "day"
    if hm >= 17 * 60 or hm < 9 * 60:
        return "reg"
    return "closed"


def get_price(c: KisClient, symb: str, excg: str = "NASD", daytime: bool = False) -> dict:
    excd = (US_EXCH_DAY if daytime else US_EXCH).get(excg, "NAS")
    d = c.get(PRICE_PATH, TR_US_PRICE, {"AUTH": "", "EXCD": excd, "SYMB": symb})
    o = d.get("output", {}) or {}
    res = {"symbol": symb, "excg": excg, "last": o.get("last"),
           "rate": o.get("rate"), "base": o.get("base"), "tvol": o.get("tvol"),
           "orderable": o.get("ordy")}
    log_event("us_price", **res)
    return res


def get_balance(c: KisClient, excg: str = "NASD", crcy: str = "USD") -> dict:
    s = c.s
    params = {"CANO": s.account, "ACNT_PRDT_CD": s.account_prod,
              "OVRS_EXCG_CD": excg, "TR_CRCY_CD": crcy,
              "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""}
    d = c.get(BAL_PATH, s.tr_us("balance"), params)
    holdings = [{
        "symbol": h.get("ovrs_pdno"), "name": h.get("ovrs_item_name"),
        "qty": h.get("ovrs_cblc_qty"), "avg_price": h.get("pchs_avg_pric"),
        "now_price": h.get("now_pric2"), "pnl": h.get("frcr_evlu_pfls_amt"),
        "pnl_pct": h.get("evlu_pfls_rt"),
    } for h in (d.get("output1") or [])]
    summ = (d.get("output2") or {})
    if isinstance(summ, list):
        summ = summ[0] if summ else {}
    res = {"msg": (d.get("msg1") or "").strip(), "holdings": holdings,
           "frcr_eval": summ.get("tot_evlu_pfls_amt") or summ.get("frcr_evlu_tota")}
    log_event("us_balance", n=len(holdings), msg=res["msg"])
    return res


def can_buy(c: KisClient, symb: str, price: float, excg: str = "NASD") -> dict:
    s = c.s
    params = {"CANO": s.account, "ACNT_PRDT_CD": s.account_prod,
              "OVRS_EXCG_CD": excg, "OVRS_ORD_UNPR": f"{price}", "ITEM_CD": symb}
    d = c.get(PSAMT_PATH, s.tr_us("psamount"), params)
    o = d.get("output", {}) or {}
    res = {"symbol": symb, "rt_cd": d.get("rt_cd"),
           "ord_psbl_usd": o.get("ord_psbl_frcr_amt"),
           "max_qty": o.get("max_ord_psbl_qty"), "exrt": o.get("exrt")}
    log_event("us_canbuy", **res)
    return res


def _order(c: KisClient, side: str, symb: str, qty: int, price: float,
           excg: str = "NASD", live: bool = False, daytime: bool | None = None) -> dict:
    s = c.s
    # daytime=None이면 현재 KST 세션으로 자동결정(낮10~16시=주간거래, 그 외=정규)
    is_day = us_session() == "day" if daytime is None else daytime
    action = side + ("_day" if is_day else "")  # buy/sell → buy_day/sell_day
    tr_id = s.tr_us(action)
    est = price
    if est <= 0:
        try:
            est = float(get_price(c, symb, excg, daytime=is_day)["last"])
        except Exception:
            est = 0.0
    check_order_usd(s, side, symb, qty, est)

    body = {"CANO": s.account, "ACNT_PRDT_CD": s.account_prod,
            "OVRS_EXCG_CD": excg, "PDNO": symb, "ORD_QTY": str(qty),
            "OVRS_ORD_UNPR": f"{price:.2f}", "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00"}
    plan = {"market": "US", "env": s.env, "side": side, "symbol": symb, "excg": excg,
            "qty": qty, "price": f"{price:.2f}", "est_usd": round(qty * est, 2),
            "session": "day" if is_day else "reg", "tr_id": tr_id}

    if s.dry_run or not live:
        log_event("us_order", mode="dry-run", **plan)
        return {"dry_run": True, **plan}

    data = c.post(ORDER_PATH, tr_id, body, use_hashkey=True)
    ok = str(data.get("rt_cd", "1")) == "0"
    out = data.get("output", {}) or {}
    res = {"dry_run": False, "ok": ok, "order_no": out.get("ODNO"),
           "org_no": out.get("KRX_FWDG_ORD_ORGNO"), "msg": data.get("msg1"),
           "msg_cd": data.get("msg_cd"), **plan}
    if ok:
        res["daily_count"] = bump_count()
    log_event("us_order", mode="live", **res)
    return res


def cancel(c: KisClient, symb: str, order_no: str, qty: int, excg: str = "NASD",
           live: bool = False) -> dict:
    """미국 미체결 주문 취소. order_no=원주문번호(ODNO)."""
    s = c.s
    body = {"CANO": s.account, "ACNT_PRDT_CD": s.account_prod, "OVRS_EXCG_CD": excg,
            "PDNO": symb, "ORGN_ODNO": order_no, "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(qty), "OVRS_ORD_UNPR": "0", "ORD_SVR_DVSN_CD": "0"}
    plan = {"market": "US", "symbol": symb, "order_no": order_no, "qty": qty, "excg": excg}
    if s.dry_run or not live:
        log_event("us_cancel", mode="dry-run", **plan)
        return {"dry_run": True, **plan}
    d = c.post(ORDER_PATH.replace("/order", "/order-rvsecncl"), s.tr_us("cancel"),
               body, use_hashkey=True)
    res = {"dry_run": False, "ok": str(d.get("rt_cd", "1")) == "0",
           "msg": d.get("msg1"), **plan}
    log_event("us_cancel", mode="live", **res)
    return res


def today_orders(c: KisClient, excg: str = "NASD") -> list[dict]:
    """미국 당일 주문체결내역 (체결/미체결)."""
    s = c.s
    today = f"{datetime.now(KST):%Y%m%d}"
    params = {"CANO": s.account, "ACNT_PRDT_CD": s.account_prod, "PDNO": "",
              "ORD_STRT_DT": today, "ORD_END_DT": today, "SLL_BUY_DVSN_CD": "00",
              "CCLD_NCCS_DVSN": "00", "OVRS_EXCG_CD": excg, "SORT_SQN": "DS",
              "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
              "CTX_AREA_NK200": "", "CTX_AREA_FK200": ""}
    d = c.get(CCNL_PATH, s.tr_us("ccnl"), params)
    rows = [{
        "order_no": o.get("odno"), "symbol": o.get("pdno"), "name": o.get("prdt_name"),
        "side": o.get("sll_buy_dvsn_cd_name"), "ord_qty": o.get("ft_ord_qty"),
        "ccld_qty": o.get("ft_ccld_qty"), "ccld_price": o.get("ft_ccld_unpr3"),
        "status": o.get("prcs_stat_name") or o.get("rjct_rson_name"),
    } for o in (d.get("output") or [])]
    log_event("us_today_orders", n=len(rows), msg=(d.get("msg1") or "").strip())
    return rows


def buy(c, symb, qty, price, excg="NASD", live=False, daytime=None):
    return _order(c, "buy", symb, qty, price, excg, live, daytime)


def sell(c, symb, qty, price, excg="NASD", live=False, daytime=None):
    return _order(c, "sell", symb, qty, price, excg, live, daytime)


# 저가 일반(비레버리지) 미국 ETF 후보 — (심볼, 이름, 주문거래소코드)
US_CANDIDATES = [
    ("SCHD", "Schwab US Dividend", "AMEX"),
    ("SPLG", "SPDR Portfolio S&P500", "AMEX"),
    ("SCHG", "Schwab US Large-Cap Growth", "AMEX"),
    ("DGRO", "iShares Core Dividend Growth", "AMEX"),
    ("SPYG", "SPDR Portfolio S&P500 Growth", "AMEX"),
]


def pick(c: KisClient, budget_usd: float) -> list[dict]:
    rows = []
    for symb, name, excg in US_CANDIDATES:
        try:
            p = float(get_price(c, symb, excg)["last"])
        except Exception as e:
            rows.append({"symbol": symb, "name": name, "error": str(e)})
            continue
        if p <= 0:
            continue
        rows.append({"symbol": symb, "name": name, "excg": excg, "price": p,
                     "affordable_shares": int(budget_usd // p),
                     "fits": budget_usd >= p})
    rows.sort(key=lambda r: (not r.get("fits", False), r.get("price", 1e12)))
    log_event("us_screener", budget_usd=budget_usd, n=len(rows))
    return rows
