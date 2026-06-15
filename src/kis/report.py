"""당일 활동 요약 — openclaw가 읽어 텔레그램으로 보고할 수 있는 텍스트 생성.

logs/YYYY-MM-DD.jsonl 을 읽어 주문/체결/에러를 집계한다. (실호출 없음)
"""
from __future__ import annotations
import json
from datetime import datetime, timezone, timedelta

from .config import LOG_DIR

KST = timezone(timedelta(hours=9))


def summarize_today() -> str:
    day = f"{datetime.now(KST):%Y-%m-%d}"
    path = LOG_DIR / f"{day}.jsonl"
    if not path.exists():
        return f"[KIS PoC {day}] 활동 없음 (로그 파일 없음)"

    orders_live, orders_dry, errors = [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("kind") in ("order", "us_order"):
            (orders_live if r.get("mode") == "live" else orders_dry).append(r)
        elif r.get("kind") == "error":
            errors.append(r)

    live_ok = [o for o in orders_live if o.get("ok")]
    live_fail = [o for o in orders_live if not o.get("ok")]

    lines = [f"📊 KIS PoC 요약 — {day}"]
    lines.append(f"• 실주문: {len(orders_live)}건 (성공 {len(live_ok)} / 실패 {len(live_fail)})")
    lines.append(f"• dry-run: {len(orders_dry)}건")
    for o in orders_live:
        mark = "✅" if o.get("ok") else "❌"
        sym = o.get("code") or o.get("symbol")
        lines.append(f"  {mark} {o.get('side')} {sym} x{o.get('qty')} "
                     f"@{o.get('price')} → {o.get('msg') or ''} (no={o.get('order_no')})")
    if errors:
        lines.append(f"• 에러 {len(errors)}건: " + "; ".join(
            f"{e.get('op','')}:{e.get('msg') or e.get('msg_cd') or e.get('status')}"
            for e in errors[:5]))
    return "\n".join(lines)
