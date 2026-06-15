"""현금주문 (매수/매도/취소) + 매수가능/체결 조회 — 안전게이트 + dry-run.

기본 dry-run: 실주문 대신 "보낼 내용"만 반환/로깅. live=True 일 때만 실제 전송.
주문구분(ORD_DVSN): '00'=지정가, '01'=시장가.
"""
from __future__ import annotations
from datetime import datetime, timezone, timedelta

from .client import KisClient
from .market import get_price
from .safety import check_order, bump_count, SafetyError
from .logging_util import log_event

ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"
CANCEL_PATH = "/uapi/domestic-stock/v1/trading/order-rvsecncl"
CCLD_PATH = "/uapi/domestic-stock/v1/trading/inquire-daily-ccld"
PSBL_PATH = "/uapi/domestic-stock/v1/trading/inquire-psbl-order"
KST = timezone(timedelta(hours=9))


def _committed_krw(c: KisClient) -> float:
    """현재 국내 노출 = 보유 매입금액 + 미체결 매수주문금액(원)."""
    inv = 0.0
    try:
        from .market import get_balance
        for h in get_balance(c).get("holdings", []):
            inv += int(float(h.get("qty") or 0)) * float(h.get("avg_price") or 0)
    except Exception:
        pass
    pend = 0.0
    try:
        for o in today_orders(c):
            if "매수" in (o.get("side") or ""):
                rem = int(float(o.get("ord_qty") or 0)) - int(float(o.get("ccld_qty") or 0))
                if rem > 0:
                    pend += rem * float(o.get("ord_unpr") or 0)
    except Exception:
        pass
    return inv + pend


def _order(c: KisClient, side: str, code: str, qty: int,
           price: int = 0, market: bool = True, live: bool = False,
           exchange: str | None = None) -> dict:
    """exchange: 거래소 라우팅 EXCG_ID_DVSN_CD (None=미지정/기존, 'KRX'/'NXT'/'SOR').
    SOR/NXT는 넥스트레이드 프리마켓(08:00~)·애프터(~20:00) 라우팅용. ⚠️ 필드명 공식 재확인 권장."""
    s = c.s
    ord_dvsn = "01" if market else "00"
    # 지정가는 호가단위(tick)에 맞춰 반올림(아니면 '주식주문호가단위 오류' 거부)
    if not market and price > 0:
        from .tick import round_tick
        price = round_tick(price)
    ord_price = "0" if market else str(price)

    est = price
    if est <= 0:
        try:
            est = int(float(get_price(c, code)["price"]))
        except Exception:
            est = 0

    check_order(s, side, code, qty, est)  # dry-run에서도 한도/킬스위치 검사

    # 포트폴리오 총 노출 한도(매수에만). MAX_TOTAL_EXPOSURE>0 일 때.
    cap = getattr(s, "max_total_exposure", 0) or 0
    if side == "buy" and cap > 0:
        committed = _committed_krw(c)
        if committed + qty * est > cap:
            raise SafetyError(
                f"포트폴리오 총액 한도 초과: 기보유/미체결 {committed:,.0f} + 이번 {qty*est:,.0f} "
                f"> 한도 {cap:,}원"
            )

    body = {
        "CANO": s.account, "ACNT_PRDT_CD": s.account_prod, "PDNO": code,
        "ORD_DVSN": ord_dvsn, "ORD_QTY": str(qty), "ORD_UNPR": ord_price,
    }
    if exchange:
        body["EXCG_ID_DVSN_CD"] = exchange  # KRX / NXT / SOR
    plan = {"env": s.env, "side": side, "code": code, "qty": qty,
            "ord_dvsn": ord_dvsn, "price": ord_price, "est_amount": qty * est,
            "exchange": exchange or "(default)", "tr_id": s.tr(side)}

    if s.dry_run or not live:
        log_event("order", mode="dry-run", **plan)
        return {"dry_run": True, **plan}

    data = c.post(ORDER_PATH, s.tr(side), body, use_hashkey=True)
    ok = str(data.get("rt_cd", "1")) == "0"
    out = data.get("output", {}) or {}
    result = {
        "dry_run": False, "ok": ok,
        "order_no": out.get("ODNO"),
        "org_no": out.get("KRX_FWDG_ORD_ORGNO"),  # 취소 시 필요
        "ord_tmd": out.get("ORD_TMD"),
        "msg": data.get("msg1"), "msg_cd": data.get("msg_cd"),
        **plan,
    }
    if ok:
        result["daily_count"] = bump_count()
    log_event("order", mode="live", **result)
    return result


def buy(c, code, qty, price=0, market=True, live=False, exchange=None):
    return _order(c, "buy", code, qty, price, market, live, exchange)


def sell(c, code, qty, price=0, market=True, live=False, exchange=None):
    return _order(c, "sell", code, qty, price, market, live, exchange)


def cancel(c: KisClient, order_no: str, org_no: str, qty: int = 0,
           live: bool = False) -> dict:
    """미체결 주문 취소. order_no/org_no 는 주문 응답의 ODNO/KRX_FWDG_ORD_ORGNO."""
    s = c.s
    body = {
        "CANO": s.account, "ACNT_PRDT_CD": s.account_prod,
        "KRX_FWDG_ORD_ORGNO": org_no, "ORGN_ODNO": order_no,
        "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "02",  # 02=취소
        "ORD_QTY": str(qty), "ORD_UNPR": "0",
        "QTY_ALL_ORD_YN": "Y" if qty == 0 else "N",
    }
    plan = {"env": s.env, "order_no": order_no, "org_no": org_no, "qty": qty}
    if s.dry_run or not live:
        log_event("cancel", mode="dry-run", **plan)
        return {"dry_run": True, **plan}
    data = c.post(CANCEL_PATH, s.tr("cancel"), body, use_hashkey=True)
    res = {"dry_run": False, "ok": str(data.get("rt_cd", "1")) == "0",
           "msg": data.get("msg1"), **plan}
    log_event("cancel", mode="live", **res)
    return res


def modify(c: KisClient, order_no: str, org_no: str, price: int = 0, qty: int = 0,
           live: bool = False, exchange: str | None = None) -> dict:
    """미체결 지정가 주문 정정(가격/수량 변경). RVSE_CNCL_DVSN_CD='01'.
    취소와 같은 order-rvsecncl 엔드포인트·TR을 쓰되 구분코드만 01(정정).
      price>0 : 새 지정가(호가단위 자동 반올림). 0이면 가격 유지 불가 → price 필수.
      qty=0   : 잔량 전체를 새 가격으로 정정(QTY_ALL_ORD_YN='Y').
      qty>0   : 해당 수량만 정정(수량 축소 등, QTY_ALL_ORD_YN='N').
    order_no/org_no 는 주문응답의 ODNO/KRX_FWDG_ORD_ORGNO (NXT는 조회로 못 찾으니 발주 시 저장본 사용).
    ⚠️ 가격을 현재가 위로 올리면 즉시 체결될 수 있음(정정=신규주문 성격)."""
    s = c.s
    new_price = price
    if new_price > 0:
        from .tick import round_tick
        new_price = round_tick(new_price)
    # 안전검사: 정정 후 노출(한도/킬스위치). qty 미지정(잔량전체)이면 검사용으로 qty=1 보수 추정.
    check_order(s, "buy", order_no, qty or 1, new_price or 0)
    body = {
        "CANO": s.account, "ACNT_PRDT_CD": s.account_prod,
        "KRX_FWDG_ORD_ORGNO": org_no, "ORGN_ODNO": order_no,
        "ORD_DVSN": "00", "RVSE_CNCL_DVSN_CD": "01",   # 01=정정
        "ORD_QTY": str(qty), "ORD_UNPR": str(new_price),
        "QTY_ALL_ORD_YN": "Y" if qty == 0 else "N",
    }
    if exchange:
        body["EXCG_ID_DVSN_CD"] = exchange
    plan = {"env": s.env, "order_no": order_no, "org_no": org_no,
            "new_price": new_price, "qty": qty, "exchange": exchange or "(default)"}
    if s.dry_run or not live:
        log_event("modify", mode="dry-run", **plan)
        return {"dry_run": True, **plan}
    data = c.post(CANCEL_PATH, s.tr("cancel"), body, use_hashkey=True)
    out = data.get("output", {}) or {}
    res = {"dry_run": False, "ok": str(data.get("rt_cd", "1")) == "0",
           "order_no_new": out.get("ODNO"), "org_no_new": out.get("KRX_FWDG_ORD_ORGNO"),
           "msg": data.get("msg1"), "msg_cd": data.get("msg_cd"), **plan}
    log_event("modify", mode="live", **res)
    return res


def can_buy(c: KisClient, code: str, price: int = 0) -> dict:
    """매수가능조회 (현금기준 살 수 있는 수량). 자격(레버리지 사전교육/예탁금)은 미반영."""
    s = c.s
    params = {"CANO": s.account, "ACNT_PRDT_CD": s.account_prod, "PDNO": code,
              "ORD_UNPR": str(price), "ORD_DVSN": "01" if price == 0 else "00",
              "CMA_EVLU_AMT_ICLD_YN": "N", "OVRS_ICLD_YN": "N"}
    d = c.get(PSBL_PATH, s.tr("psbl"), params)
    o = d.get("output", {}) or {}
    return {"code": code, "rt_cd": d.get("rt_cd"), "msg": (d.get("msg1") or "").strip(),
            "ord_psbl_cash": o.get("ord_psbl_cash"), "max_buy_qty": o.get("max_buy_qty"),
            "nrcvb_buy_qty": o.get("nrcvb_buy_qty")}


def executions(c: KisClient, date_from: str | None = None, date_to: str | None = None,
               only_filled: bool = False) -> list[dict]:
    """주문체결조회(기간). date_from/to=YYYYMMDD(미지정=오늘). only_filled=True면 체결분만(CCLD_DVSN='01').
    ⚠️ NXT 프리/애프터 '미체결' 대기주문은 이 조회에 안 잡힌다(체결되면 잡힘). 미체결 추적은 저장 ledger 사용."""
    s = c.s
    today = f"{datetime.now(KST):%Y%m%d}"
    d1 = date_from or today
    d2 = date_to or d1
    params = {
        "CANO": s.account, "ACNT_PRDT_CD": s.account_prod,
        "INQR_STRT_DT": d1, "INQR_END_DT": d2,
        "SLL_BUY_DVSN_CD": "00", "INQR_DVSN": "00", "PDNO": "",
        "CCLD_DVSN": "01" if only_filled else "00", "ORD_GNO_BRNO": "", "ODNO": "",
        "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
        "CTX_AREA_FK100": "", "CTX_AREA_NK100": "",
    }
    rows = []
    while True:
        d = c.get(CCLD_PATH, s.tr("ccld"), params)
        for o in d.get("output1", []) or []:
            rows.append({
                "ord_dt": o.get("ord_dt"), "ord_tmd": o.get("ord_tmd"),
                "order_no": o.get("odno"), "code": o.get("pdno"), "name": o.get("prdt_name"),
                "side": o.get("sll_buy_dvsn_cd_name"),
                "ord_qty": o.get("ord_qty"), "ccld_qty": o.get("tot_ccld_qty"),
                "ord_unpr": o.get("ord_unpr"),
                "ccld_amt": o.get("tot_ccld_amt"), "avg_price": o.get("avg_prvs"),
                "status": o.get("ccld_yn") or o.get("ord_dvsn_name"),
            })
        # 페이지네이션(연속조회): tr_cont F/M 이면 다음 페이지
        if (d.get("tr_cont") or "") in ("F", "M") and d.get("ctx_area_nk100"):
            params["CTX_AREA_FK100"] = d.get("ctx_area_fk100", "")
            params["CTX_AREA_NK100"] = d.get("ctx_area_nk100", "")
            continue
        break
    log_event("executions", d1=d1, d2=d2, only_filled=only_filled, n=len(rows))
    return rows


def today_orders(c: KisClient) -> list[dict]:
    """당일 주문체결조회 — 주문/체결 상태 확인. (executions 의 오늘치 래퍼)"""
    return executions(c)
