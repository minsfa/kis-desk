"""미국 PoC 단일 래퍼 — OpenClaw가 파라미터 없이 안전하게 실행할 1개 명령.

고정값: SCHD 1주, AMEX. 흐름: 시세·실시간점검 → 매수가능 → dry-run 계획
→ (라이브 조건 충족 시) 매수→체결확인→미체결취소→매도→체결확인 → 요약.

라이브는 (--live AND .env DRY_RUN=false AND 주문가능시간) 모두 만족할 때만.
즉 OpenClaw 혼자서는 DRY_RUN=true인 한 절대 실매매 못 함(사람이 게이트).
"""
from __future__ import annotations
import time

from .client import KisClient
from . import overseas, report

SYMB = "SCHD"
EXCG = "AMEX"
QTY = 1


def _poll_fill(c: KisClient, order_no: str, polls: int = 5, wait: int = 3) -> dict:
    for i in range(polls):
        for o in overseas.today_orders(c, EXCG):
            if o.get("order_no") == order_no:
                try:
                    ccld = int(float(o.get("ccld_qty") or 0))
                except Exception:
                    ccld = 0
                if ccld >= QTY:
                    return {"done": True, "ccld_qty": ccld, "price": o.get("ccld_price")}
        if i < polls - 1:
            time.sleep(wait)
    return {"done": False}


def run(c: KisClient, live: bool = False, buy_off: float = 0.003,
        sell_off: float = 0.003) -> str:
    L = ["=== KIS 미국 PoC (SCHD 1주) ==="]
    p = overseas.get_price(c, SYMB, EXCG)
    last = float(p["last"]) if p.get("last") else 0.0
    # 주의: ordy 필드는 장 개장여부 신호가 아님(개장 중에도 '매도불가' 표기). 정보로만 표시.
    L.append(f"[1] 시세: last=${last} ({p.get('rate')}%) / tvol={p.get('tvol')} / ordy={p.get('orderable')}")

    cb = overseas.can_buy(c, SYMB, round(last, 2), EXCG)
    L.append(f"[2] 매수가능: ${cb.get('ord_psbl_usd')} / 최대 {cb.get('max_qty')}주 / 환율 {cb.get('exrt')}")

    buy_price = round(last * (1 + buy_off), 2)
    sell_price = round(last * (1 - sell_off), 2)
    dry = overseas.buy(c, SYMB, QTY, buy_price, excg=EXCG, live=False)
    L.append(f"[3] 주문계획(dry): 매수 {QTY}주 @${buy_price} ≈ ${dry.get('est_usd')} (USD한도 ${c.s.max_order_usd})")

    # 라이브 게이트: --live + DRY_RUN=false 만으로 결정(ordy로 막지 않음).
    # 장 마감/권한 문제는 KIS 서버가 주문 거부로 알려주므로 fail-closed로 처리.
    go_live = live and (not c.s.dry_run)
    if not go_live:
        why = "DRY_RUN=true" if c.s.dry_run else "--live 아님"
        L.append(f"[4] 라이브 미실행({why}) → dry-run 리허설만. 끝.")
        return "\n".join(L)

    # ---- 라이브 1주 왕복 ----
    b = overseas.buy(c, SYMB, QTY, buy_price, excg=EXCG, live=True)
    L.append(f"[4] 매수전송 ok={b.get('ok')} no={b.get('order_no')} msg={b.get('msg')}")
    if not b.get("ok"):
        L.append("    → 매수 실패, 중단"); return "\n".join(L)

    f = _poll_fill(c, b.get("order_no"))
    L.append(f"[5] 매수 체결확인: {f}")
    if not f.get("done"):
        cx = overseas.cancel(c, SYMB, b.get("order_no"), QTY, excg=EXCG, live=True)
        L.append(f"    → 미체결, 취소 ok={cx.get('ok')} msg={cx.get('msg')}. 중단")
        return "\n".join(L)

    s = overseas.sell(c, SYMB, QTY, sell_price, excg=EXCG, live=True)
    L.append(f"[6] 매도전송 ok={s.get('ok')} no={s.get('order_no')} msg={s.get('msg')}")
    fs = _poll_fill(c, s.get("order_no"))
    L.append(f"[7] 매도 체결확인: {fs}")
    L.append("[8] 요약\n" + report.summarize_today())
    return "\n".join(L)
