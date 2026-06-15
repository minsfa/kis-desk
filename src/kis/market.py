"""시세·잔고 조회 (read-only). 모의/실전 양쪽 동작."""
from __future__ import annotations

from .client import KisClient
from .config import TR_PRICE
from .logging_util import log_event


def get_price(c: KisClient, code: str) -> dict:
    """주식 현재가. code 예: '005930'(삼성전자), '122630'(KODEX 레버리지)."""
    path = "/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    data = c.get(path, TR_PRICE, params)
    out = data.get("output", {})
    res = {
        "code": code,
        "price": out.get("stck_prpr"),         # 현재가
        "change_pct": out.get("prdy_ctrt"),    # 전일대비율
        "volume": out.get("acml_vol"),         # 누적거래량
        "high": out.get("stck_hgpr"),
        "low": out.get("stck_lwpr"),
    }
    log_event("price", **res)
    return res


def get_orderbook(c: KisClient, code: str, mkt: str = "J") -> dict:
    """호가창 — 매수/매도 잔량(매수벽 '받침' 판단용). 잔량비>1이면 매수 우위.
    mkt: 'J'=KRX, 'UN'=통합(NXT포함, 프리마켓 반영), 'NX'=NXT."""
    path = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
    d = c.get(path, "FHKST01010200", {"FID_COND_MRKT_DIV_CODE": mkt, "FID_INPUT_ISCD": code})
    o = d.get("output1", {}) or {}
    ta = float(o.get("total_askp_rsqn") or 0)
    tb = float(o.get("total_bidp_rsqn") or 0)
    res = {
        "code": code,
        "bid1": o.get("bidp1"), "ask1": o.get("askp1"),
        "total_bid_qty": tb, "total_ask_qty": ta,
        "bid_ask_ratio": round(tb / ta, 3) if ta else None,  # >1 = 매수벽 우위
    }
    log_event("orderbook", **res)
    return res


def get_balance(c: KisClient) -> dict:
    """주식 잔고조회. 보유종목 + 예수금 요약."""
    path = "/uapi/domestic-stock/v1/trading/inquire-balance"
    params = {
        "CANO": c.s.account,
        "ACNT_PRDT_CD": c.s.account_prod,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "00",
        "CTX_AREA_FK100": "",
        "CTX_AREA_NK100": "",
    }
    data = c.get(path, c.s.tr("balance"), params)
    holdings = [
        {
            "code": h.get("pdno"),
            "name": h.get("prdt_name"),
            "qty": h.get("hldg_qty"),
            "avg_price": h.get("pchs_avg_pric"),
            "eval_pnl": h.get("evlu_pfls_amt"),
            "pnl_pct": h.get("evlu_pfls_rt"),
        }
        for h in data.get("output1", [])
    ]
    summary = (data.get("output2") or [{}])[0]
    res = {
        "holdings": holdings,
        "deposit": summary.get("dnca_tot_amt"),          # 예수금총액
        "eval_total": summary.get("tot_evlu_amt"),       # 총평가금액
        "pnl_total": summary.get("evlu_pfls_smtl_amt"),  # 평가손익합계
    }
    log_event("balance", deposit=res["deposit"], eval_total=res["eval_total"],
              n_holdings=len(holdings))
    return res
