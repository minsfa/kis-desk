"""전 호출/주문 JSONL 감사 로깅. logs/YYYY-MM-DD.jsonl 에 1줄씩 append."""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta
from .config import LOG_DIR

KST = timezone(timedelta(hours=9))


def _now():
    return datetime.now(KST)


def log_event(kind: str, **fields) -> None:
    """kind 예: token, price, balance, order, error. 민감정보(키)는 넣지 말 것."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": _now().isoformat(timespec="seconds"), "kind": kind, **fields}
    path = LOG_DIR / f"{_now():%Y-%m-%d}.jsonl"
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
