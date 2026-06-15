"""주문 안전게이트 — 킬스위치 · 하드 한도 · 일일 카운터."""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .config import Settings, KILL_SWITCH, STATE_DIR

KST = timezone(timedelta(hours=9))


class SafetyError(Exception):
    """주문을 차단해야 하는 안전 위반."""


def _counter_path() -> Path:
    return STATE_DIR / f"daily_count_{datetime.now(KST):%Y-%m-%d}.json"


def _read_count() -> int:
    p = _counter_path()
    if not p.exists():
        return 0
    try:
        return int(json.loads(p.read_text()).get("count", 0))
    except Exception:
        return 0


def bump_count() -> int:
    p = _counter_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    n = _read_count() + 1
    p.write_text(json.dumps({"count": n}), encoding="utf-8")
    return n


def check_order(s: Settings, side: str, code: str, qty: int, est_price: float) -> None:
    """주문 직전 안전검사. 위반 시 SafetyError. (한도 검사는 dry-run에서도 수행)"""
    if KILL_SWITCH.exists():
        raise SafetyError(f"킬스위치 활성({KILL_SWITCH}). 모든 주문 차단됨.")

    if qty <= 0:
        raise SafetyError(f"수량이 0 이하: {qty}")

    est_amount = qty * est_price
    if est_amount > s.max_order_amount:
        raise SafetyError(
            f"1회 주문금액 한도 초과: 추정 {est_amount:,.0f}원 > 한도 {s.max_order_amount:,}원"
        )

    n = _read_count()
    if n >= s.max_daily_trades:
        raise SafetyError(f"일일 매매횟수 한도 도달: {n}/{s.max_daily_trades}")


def check_order_usd(s: Settings, side: str, symb: str, qty: int, est_price_usd: float) -> None:
    """해외(USD) 주문 안전검사. 한도는 USD 기준(MAX_ORDER_USD)."""
    if KILL_SWITCH.exists():
        raise SafetyError(f"킬스위치 활성({KILL_SWITCH}). 모든 주문 차단됨.")
    if qty <= 0:
        raise SafetyError(f"수량이 0 이하: {qty}")
    est = qty * est_price_usd
    if est > s.max_order_usd:
        raise SafetyError(
            f"1회 주문금액(USD) 한도 초과: 추정 ${est:,.2f} > 한도 ${s.max_order_usd:,.2f}"
        )
    n = _read_count()
    if n >= s.max_daily_trades:
        raise SafetyError(f"일일 매매횟수 한도 도달: {n}/{s.max_daily_trades}")
