"""H1 하버스 — 미국주식 기계적 왕복(round-trip) 안정성 테스트.

목적은 알파가 아니라 **무인 시스템이 매일 똑같이 안정적으로 도는가** 검증.
cron 2-leg: 개장 직후 buy_leg, 마감 직전 sell_leg. 각 leg는 짧은 프로세스로 독립 실행되며
매번 잔고(=ground truth)를 읽어 reconciliation이 공짜로 된다.

리포트(trading-bot-reliability) 반영 보강:
- 멱등성: 잔고/상태 체크 후 행동(KIS엔 client_order_id 없음 → 잔고가 멱등 기준)
- 에러분류: Retryable(네트워크/레이트리밋)→지수백오프+지터 재시도 / Fatal(거부·한도)→중단
- Circuit breaker: 연속 N회 실패 → OPEN(쿨다운 동안 skip)
- 일일 손실 한도 + STOP 킬스위치(overseas 안전게이트와 별개 다층)
- 전수 지표 로깅 → report()로 체결률·슬리피지·왕복비용·에러율 집계
주문 자체는 overseas.buy/sell(= check_order_usd 게이트 통과) 사용. DRY_RUN=false + live=True라야 실주문.
"""
from __future__ import annotations
import csv
import json
import random
import time
from datetime import datetime, timedelta, timezone

import requests

from .client import KisClient
from . import overseas
from .config import PROJECT_ROOT
from .safety import SafetyError
from .logging_util import log_event

KST = timezone(timedelta(hours=9))
DIR = PROJECT_ROOT / "data" / "us_stab"
STATE = DIR / "state.json"
BREAKER = DIR / "breaker.json"
METRICS = DIR / "metrics.csv"

# ---- 튜닝 상수 ----
SYMBOL, EXCG, QTY = "SOFI", "NASD", 1
BUY_SLIP, SELL_SLIP = 1.003, 0.997     # 체결되게 현재가 ±0.3% 지정가
FILL_POLL_SEC, FILL_TIMEOUT = 2, 30    # 체결확인 폴링 간격/타임아웃
CB_THRESHOLD, CB_COOLDOWN = 3, 1800    # 연속 3회 실패 → 30분 OPEN
MAX_DAILY_LOSS_USD = 5.0               # 1주 테스트 기준; 초과 시 신규매수 중단
RETRY_MAX, RETRY_BASE, RETRY_CAP = 4, 1.0, 20.0


def _today() -> str:
    return f"{datetime.now(KST):%Y-%m-%d}"


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)


def _save(path, obj):
    DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


# ---- Circuit breaker ----
def _breaker_state() -> dict:
    return _load(BREAKER, {"fails": 0, "opened_at": 0})


def _breaker_open() -> bool:
    b = _breaker_state()
    if b["fails"] >= CB_THRESHOLD and (time.time() - b.get("opened_at", 0)) < CB_COOLDOWN:
        return True
    return False


def _breaker_record(ok: bool):
    b = _breaker_state()
    if ok:
        b = {"fails": 0, "opened_at": 0}
    else:
        b["fails"] = b.get("fails", 0) + 1
        if b["fails"] >= CB_THRESHOLD:
            b["opened_at"] = time.time()
            log_event("us_stab_breaker_open", fails=b["fails"])
    _save(BREAKER, b)


# ---- 에러분류 + 재시도(지수 백오프 + Full Jitter) ----
_RETRYABLE = (requests.exceptions.RequestException, TimeoutError, ConnectionError)


def _safe_order(fn):
    """Retryable(네트워크/레이트리밋)만 백오프 재시도. Fatal(SafetyError/거부)은 즉시 전파."""
    last = None
    for attempt in range(RETRY_MAX):
        try:
            res = fn()
            # KIS 레이트리밋(EGW00201) 등은 ok=False로 올 수 있음 → 재시도
            if isinstance(res, dict) and not res.get("ok") and not res.get("dry_run"):
                msg = str(res.get("msg") or res.get("msg_cd") or "")
                if "EGW00201" in msg or "초당" in msg:
                    raise TimeoutError(f"rate-limit: {msg}")
            return res
        except SafetyError:
            raise                       # Fatal: 한도/킬스위치 — 재시도 안 함
        except _RETRYABLE as e:
            last = e
            if attempt == RETRY_MAX - 1:
                break
            wait = min(RETRY_CAP, RETRY_BASE * (2 ** attempt)) * random.random()
            log_event("us_stab_retry", attempt=attempt + 1, err=type(e).__name__, wait=round(wait, 2))
            time.sleep(wait)
    raise last if last else RuntimeError("주문 재시도 소진")


# ---- 잔고 헬퍼 (브로커=ground truth) ----
def _holding(c: KisClient, symbol: str) -> dict | None:
    bal = overseas.get_balance(c)
    for h in (bal.get("holdings") or []):
        if h.get("symbol") == symbol and float(h.get("qty") or 0) > 0:
            return h
    return None


def _price(c: KisClient, symbol: str, excg: str) -> float:
    return float(overseas.get_price(c, symbol, excg).get("last") or 0)


def _log_metric(row: dict):
    DIR.mkdir(parents=True, exist_ok=True)
    cols = ["ts", "leg", "symbol", "qty", "limit", "sent_ok", "order_no",
            "fill_price", "slippage_pct", "hold_min", "pnl_usd", "error", "breaker"]
    new = not METRICS.exists()
    with open(METRICS, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


# ---- legs ----
def buy_leg(c: KisClient, symbol=SYMBOL, excg=EXCG, qty=QTY, live=False) -> dict:
    ts = f"{datetime.now(KST):%Y-%m-%d %H:%M:%S}"
    st = _load(STATE, {})
    # 멱등: 오늘 이미 매수했거나 이미 보유 중이면 skip
    if st.get("date") == _today() and st.get("phase") in ("bought", "sold"):
        return {"leg": "buy", "skip": "이미 오늘 매수함", "phase": st.get("phase")}
    if _holding(c, symbol):
        return {"leg": "buy", "skip": "이미 보유 중"}
    # circuit breaker
    if _breaker_open():
        log_event("us_stab_skip_breaker", leg="buy")
        return {"leg": "buy", "skip": "circuit breaker OPEN"}
    # 일일 손실 한도
    if abs(min(0.0, st.get("daily_pnl", 0.0))) >= MAX_DAILY_LOSS_USD and st.get("date") == _today():
        return {"leg": "buy", "skip": f"일일손실한도(${MAX_DAILY_LOSS_USD}) 도달"}

    px = _price(c, symbol, excg)
    if px <= 0:
        return {"leg": "buy", "error": "시세 없음(장 마감/조회불가)"}
    limit = round(px * BUY_SLIP, 2)
    try:
        res = _safe_order(lambda: overseas.buy(c, symbol, qty, limit, excg=excg, live=live))
    except SafetyError as e:
        _log_metric({"ts": ts, "leg": "buy", "symbol": symbol, "qty": qty, "limit": limit,
                     "sent_ok": False, "error": f"SAFETY:{e}", "breaker": _breaker_state()["fails"]})
        _breaker_record(False)
        return {"leg": "buy", "error": f"안전차단: {e}"}
    except Exception as e:
        _log_metric({"ts": ts, "leg": "buy", "symbol": symbol, "qty": qty, "limit": limit,
                     "sent_ok": False, "error": f"{type(e).__name__}:{e}", "breaker": _breaker_state()["fails"]})
        _breaker_record(False)
        return {"leg": "buy", "error": str(e)}

    if res.get("dry_run"):   # 모의: 상태/지표 오염 없이 경로만 검증
        return {"leg": "buy", "ok": True, "limit": limit, "dry_run": True}
    ok = bool(res.get("ok"))
    fill = None
    if ok:
        deadline = time.time() + FILL_TIMEOUT
        while time.time() < deadline:
            h = _holding(c, symbol)
            if h:
                fill = float(h.get("avg_price") or 0)
                break
            time.sleep(FILL_POLL_SEC)
    slip = round((fill - limit) / limit * 100, 3) if fill else None
    _breaker_record(ok)
    if ok:
        _save(STATE, {"date": _today(), "phase": "bought", "symbol": symbol, "qty": qty,
                      "buy_limit": limit, "buy_fill": fill, "buy_order_no": res.get("order_no"),
                      "daily_pnl": st.get("daily_pnl", 0.0) if st.get("date") == _today() else 0.0})
    _log_metric({"ts": ts, "leg": "buy", "symbol": symbol, "qty": qty, "limit": limit,
                 "sent_ok": ok, "order_no": res.get("order_no"), "fill_price": fill,
                 "slippage_pct": slip, "error": "" if ok else res.get("msg"),
                 "breaker": _breaker_state()["fails"]})
    return {"leg": "buy", "ok": ok, "limit": limit, "fill": fill, "slippage_pct": slip,
            "order_no": res.get("order_no"), "dry_run": res.get("dry_run")}


def sell_leg(c: KisClient, symbol=SYMBOL, excg=EXCG, live=False) -> dict:
    ts = f"{datetime.now(KST):%Y-%m-%d %H:%M:%S}"
    st = _load(STATE, {})
    h = _holding(c, symbol)
    if not h:
        return {"leg": "sell", "skip": "보유 없음(이미 청산/미체결)"}
    if _breaker_open():
        return {"leg": "sell", "skip": "circuit breaker OPEN"}
    qty = int(float(h.get("qty")))
    px = _price(c, symbol, excg)
    if px <= 0:
        return {"leg": "sell", "error": "시세 없음"}
    limit = round(px * SELL_SLIP, 2)
    try:
        res = _safe_order(lambda: overseas.sell(c, symbol, qty, limit, excg=excg, live=live))
    except Exception as e:
        _log_metric({"ts": ts, "leg": "sell", "symbol": symbol, "qty": qty, "limit": limit,
                     "sent_ok": False, "error": f"{type(e).__name__}:{e}", "breaker": _breaker_state()["fails"]})
        _breaker_record(False)
        return {"leg": "sell", "error": str(e)}

    if res.get("dry_run"):
        return {"leg": "sell", "ok": True, "limit": limit, "dry_run": True}
    ok = bool(res.get("ok"))
    flat = False
    if ok:
        deadline = time.time() + FILL_TIMEOUT
        while time.time() < deadline:
            if not _holding(c, symbol):
                flat = True
                break
            time.sleep(FILL_POLL_SEC)
    # 왕복 손익(근사: 매수체결가 vs 매도 지정가) + 보유시간
    buy_fill = st.get("buy_fill")
    pnl = round((limit - buy_fill) * qty, 4) if (buy_fill and ok) else None
    slip = round((limit - px) / px * 100, 3)
    _breaker_record(ok)
    if ok:
        dp = (st.get("daily_pnl", 0.0) if st.get("date") == _today() else 0.0) + (pnl or 0.0)
        _save(STATE, {**st, "date": _today(), "phase": "sold", "sell_limit": limit,
                      "sell_order_no": res.get("order_no"), "daily_pnl": round(dp, 4)})
    _log_metric({"ts": ts, "leg": "sell", "symbol": symbol, "qty": qty, "limit": limit,
                 "sent_ok": ok, "order_no": res.get("order_no"), "fill_price": (limit if flat else None),
                 "slippage_pct": slip, "pnl_usd": pnl, "error": "" if ok else res.get("msg"),
                 "breaker": _breaker_state()["fails"]})
    return {"leg": "sell", "ok": ok, "limit": limit, "flat": flat, "pnl_usd": pnl,
            "order_no": res.get("order_no"), "dry_run": res.get("dry_run")}


def report() -> str:
    """안정성 지표 집계 — 체결률·슬리피지·왕복비용·에러·브레이커."""
    if not METRICS.exists():
        return "📊 H1 안정성 리포트 — 데이터 없음(아직 실행 전)"
    rows = list(csv.DictReader(open(METRICS, encoding="utf-8")))
    buys = [r for r in rows if r["leg"] == "buy"]
    sells = [r for r in rows if r["leg"] == "sell"]
    def _rate(rs):
        sent = [r for r in rs if r.get("sent_ok") == "True"]
        return f"{len(sent)}/{len(rs)}" if rs else "0/0"
    def _avg(rs, k):
        v = [float(r[k]) for r in rs if r.get(k) not in (None, "", "None")]
        return f"{sum(v)/len(v):+.3f}" if v else "—"
    errs = [r for r in rows if r.get("error")]
    pnls = [float(r["pnl_usd"]) for r in sells if r.get("pnl_usd") not in (None, "", "None")]
    L = ["📊 H1 하버스 안정성 리포트"]
    L.append(f"  사이클: 매수 {len(buys)} / 매도 {len(sells)} (왕복 {len(pnls)})")
    L.append(f"  체결성공률: 매수 {_rate(buys)} · 매도 {_rate(sells)}")
    L.append(f"  평균 슬리피지: 매수 {_avg(buys,'slippage_pct')}% · 매도 {_avg(sells,'slippage_pct')}%")
    L.append(f"  왕복 손익(USD): 합 {sum(pnls):+.3f} · 평균 {(sum(pnls)/len(pnls)) if pnls else 0:+.4f}")
    L.append(f"  에러: {len(errs)}건 · circuit breaker fails={_breaker_state()['fails']}{' (OPEN)' if _breaker_open() else ''}")
    if errs:
        L.append("  최근 에러: " + " / ".join(f"{e['ts'][-8:]} {e['leg']} {e['error'][:40]}" for e in errs[-3:]))
    L.append(f"💾 {METRICS}")
    return "\n".join(L)
