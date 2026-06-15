"""approved.json 의 종목을 '감시 없이' 지정가로 바로 거는 실행기 + 잔고기반 체결확인.

stratv0 는 watch-and-trigger(현재가가 지정가 닿을 때 발주)지만, 진입조건이 이미 확정된 건은
여기서 처음부터 호가창에 지정가를 얹는다(resting). NXT 미체결은 daily-ccld 조회로 못 찾으므로
발주 시 order_no/org_no 를 data/orders_live_<날짜>.json 에 저장해 정정/취소에 쓴다.
체결확인은 잔고기반(holdings qty>0) + 가용현금(can_buy.ord_psbl_cash) 변화로 추론한다.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta

from .client import KisClient
from .config import PROJECT_ROOT
from . import orders, market, approve
from .tick import round_tick

KST = timezone(timedelta(hours=9))
DATA_DIR = PROJECT_ROOT / "data"
# ETF/레버리지는 NXT 거래 불가 → 정규장(KRX)만.
ETF_CODES = {"122630", "091160", "233740", "379800", "360750", "069500", "229200"}


def _ledger_path(date_str: str | None = None):
    d = date_str or datetime.now(KST).strftime("%Y-%m-%d")
    return DATA_DIR / f"orders_live_{d}.json"


def _load_ledger(date_str: str | None = None) -> list[dict]:
    p = _ledger_path(date_str)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_ledger(rows: list[dict], date_str: str | None = None) -> None:
    p = _ledger_path(date_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def session_routing(now: datetime | None = None):
    """현재 KST 세션 → (세션명, 거래소 라우팅). pre/after=SOR(NXT), reg=None(KRX)."""
    n = now or datetime.now(KST)
    hm = n.hour * 60 + n.minute
    if 8 * 60 <= hm < 8 * 60 + 50:
        return "nxt_pre", "SOR"
    if 9 * 60 <= hm < 15 * 60 + 30:
        return "kr_reg", None
    if 16 * 60 <= hm < 20 * 60:
        return "nxt_after", "SOR"
    return "closed", None


def _legs_of(info) -> list[dict]:
    if not isinstance(info, dict):
        return []
    legs = info.get("legs")
    if not legs and info.get("price"):
        legs = [{"price": info["price"], "target": info.get("target")}]
    return legs or []


def place_approved(c: KisClient, live: bool = False, budget: int = 100000) -> dict:
    """approved.json 의 각 leg 를 지정가 매수로 즉시 발주(감시 X). order_no 저장.
    ETF는 NXT 세션이면 스킵(거래불가). qty 미지정 leg 는 budget//price."""
    o = approve.current()
    codes = o.get("codes") or {}
    sess, exch = session_routing()
    placed, skipped = [], []
    ledger = _load_ledger()
    for code, info in codes.items():
        name = (info.get("name") if isinstance(info, dict) else None) or code
        for i, lg in enumerate(_legs_of(info)):
            price = round_tick(int(lg["price"]), up=False)
            qty = int(lg.get("qty") or (budget // price if price > 0 else 0))
            if qty < 1:
                skipped.append((name, code, "수량0(예산부족)"))
                continue
            if exch == "SOR" and code in ETF_CODES:
                skipped.append((name, code, "ETF는 NXT 거래불가"))
                continue
            if sess == "closed":
                skipped.append((name, code, "장 시간 아님"))
                continue
            try:
                r = orders.buy(c, code, qty, price=price, market=False, live=live, exchange=exch)
            except Exception as e:
                skipped.append((name, code, f"발주실패 {str(e)[:40]}"))
                continue
            rec = {
                "ts": datetime.now(KST).isoformat(timespec="seconds"),
                "leg": f"{code}:{i}", "code": code, "name": name,
                "side": "buy", "qty": qty, "price": price,
                "target": int(lg["target"]) if lg.get("target") else None,
                "exchange": exch or "KRX", "session": sess,
                "order_no": r.get("order_no"), "org_no": r.get("org_no"),
                "dry_run": r.get("dry_run", False), "ok": r.get("ok"),
            }
            placed.append(rec)
            if not rec["dry_run"]:        # dry-run은 ledger에 안 남김(실주문만 추적)
                ledger.append(rec)
    _save_ledger(ledger)
    return {"session": sess, "routing": exch or "KRX", "placed": placed,
            "skipped": skipped, "ledger": str(_ledger_path())}


def update_on_modify(old_order_no: str, new_order_no: str | None,
                     new_price: int | None = None, new_qty: int | None = None,
                     date_str: str | None = None) -> bool:
    """정정 성공 후 ledger의 해당 주문 order_no/price/qty 를 갱신(정정은 새 ODNO 발급)."""
    rows = _load_ledger(date_str)
    hit = False
    for r in rows:
        if r.get("order_no") == old_order_no:
            if new_order_no:
                r["order_no"] = new_order_no
            if new_price:
                r["price"] = int(new_price)
            if new_qty:
                r["qty"] = int(new_qty)
            r["modified_ts"] = datetime.now(KST).isoformat(timespec="seconds")
            hit = True
    if hit:
        _save_ledger(rows, date_str)
    return hit


def fill_status(c: KisClient, date_str: str | None = None) -> dict:
    """leg별 체결/미체결 — 1차: 체결내역(ccld, order_no 매칭), 2차: 미체결은 저장 ledger 기준.
    NXT 미체결은 ccld에 안 떠도 ledger 의 발주수량으로 pending 표기."""
    ledger = _load_ledger(date_str)
    d = (date_str or datetime.now(KST).strftime("%Y-%m-%d")).replace("-", "")
    ex = orders.executions(c, d, d)                       # 오늘 체결내역
    by_ono = {e["order_no"]: e for e in ex}
    rows = []
    for r in ledger:
        if r.get("side") != "buy":
            continue
        ono = r.get("order_no")
        e = by_ono.get(ono)
        filled = int(float(e.get("ccld_qty") or 0)) if e else 0
        ordered = int(r.get("qty") or 0)
        rows.append({
            "leg": r["leg"], "code": r["code"], "name": r["name"],
            "price": r["price"], "ordered": ordered, "filled": filled,
            "pending": max(0, ordered - filled), "order_no": ono,
            "avg_price": (e or {}).get("avg_price"),
            "state": "체결완료" if filled >= ordered and ordered > 0
                     else ("일부체결" if filled > 0 else "미체결(대기)"),
        })
    bal = market.get_balance(c)
    return {"date": d, "deposit": bal.get("deposit"),
            "rows": rows, "fills_today": ex, "ledger": str(_ledger_path(date_str))}


def place_targets(c: KisClient, live: bool = False, date_str: str | None = None) -> dict:
    """체결된 leg에 대해 +목표가(target) 매도 지정가를 건다. 보유수량 한도 내에서만."""
    ledger = _load_ledger(date_str)
    bal = market.get_balance(c)
    held = {h.get("code"): int(float(h.get("qty") or 0)) for h in bal.get("holdings", [])}
    sess, exch = session_routing()
    placed, skipped = [], []
    for r in ledger:
        code, tgt = r["code"], r.get("target")
        if not tgt:
            skipped.append((r["name"], code, "목표가 없음"))
            continue
        avail = held.get(code, 0)
        qty = min(int(r.get("qty") or 0), avail)
        if qty < 1:
            skipped.append((r["name"], code, "미체결/보유0"))
            continue
        if exch == "SOR" and code in ETF_CODES:
            skipped.append((r["name"], code, "ETF NXT 불가"))
            continue
        try:
            sr = orders.sell(c, code, qty, price=int(tgt), market=False, live=live, exchange=exch)
            held[code] = avail - qty  # 같은 종목 다음 leg가 중복 매도 안 하게 차감
            placed.append({"code": code, "name": r["name"], "qty": qty, "target": int(tgt),
                           "order_no": sr.get("order_no"), "ok": sr.get("ok")})
        except Exception as e:
            skipped.append((r["name"], code, f"발주실패 {str(e)[:40]}"))
    return {"placed": placed, "skipped": skipped}
