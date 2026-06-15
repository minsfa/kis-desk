"""승인 게이트 — '제안→승인→장전' 흐름의 승인목록 관리(data/approved.json).

propose가 후보를 제안하면, 윤기 님이 승인한 종목만 여기 기록된다(=총알 장전).
다음 거래일 stratv0는 이 목록의 종목만 자동주문한다(승인 0개면 무거래).
목록은 '대상 거래일' 기준. 저녁(16시 이후) 승인은 다음 영업일용으로 잡힌다.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone

from .config import PROJECT_ROOT
from .tick import round_tick

KST = timezone(timedelta(hours=9))
PATH = PROJECT_ROOT / "data" / "approved.json"


def target_date(now: datetime | None = None) -> str:
    """승인이 적용될 거래일. 장 마감 후(16시~) 승인이면 다음 영업일."""
    d = now or datetime.now(KST)
    if d.hour >= 16:
        d = d + timedelta(days=1)
    while d.weekday() >= 5:        # 토(5)·일(6) 건너뜀
        d = d + timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _read() -> dict:
    if PATH.exists():
        try:
            return json.loads(PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": "", "codes": {}}


def _save(o: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(o, ensure_ascii=False, indent=2), encoding="utf-8")


def add(code: str, name: str | None = None, price: int | None = None,
        target: int | None = None, qty: int | None = None, replace: bool = False) -> dict:
    """승인 추가/갱신. price 지정시 트랑쉐(분할매수) 1개 추가(여러번 호출=여러 단가).
    price 미지정=시가-dip% 단일. target=목표가, qty=수량(미지정시 예산/단가). replace=기존 트랑쉐 교체."""
    o = _read(); td = target_date()
    if o.get("date") != td:            # 날짜 바뀌면 새 목록으로 리셋
        o = {"date": td, "codes": {}}
    rec = o["codes"].get(code)
    if not isinstance(rec, dict):      # 기존 문자열/없음 → dict로
        rec = {"name": rec if isinstance(rec, str) else code}
    if name:
        rec["name"] = name
    rec.setdefault("name", code)
    if price is not None:
        leg = {"price": round_tick(price, up=False)}      # 매수=호가단위 내림
        if target is not None:
            leg["target"] = round_tick(target, up=True)   # 목표=호가단위 올림
        if qty is not None:
            leg["qty"] = int(qty)
        if replace:
            rec["legs"] = [leg]
        else:
            rec.setdefault("legs", []).append(leg)
    o["codes"][code] = rec
    _save(o); return o


def remove(code: str) -> dict:
    o = _read(); o.get("codes", {}).pop(code, None); _save(o); return o


def clear() -> dict:
    o = {"date": target_date(), "codes": {}}; _save(o); return o


def current() -> dict:
    return _read()


def load_for(date_str: str) -> dict:
    """해당 거래일에 승인된 {code:name}. 날짜 불일치면 빈 dict(=장전 안 됨)."""
    o = _read()
    return dict(o.get("codes", {})) if o.get("date") == date_str else {}


def summary(o: dict | None = None) -> str:
    o = o or _read()
    codes = o.get("codes") or {}
    if not codes:
        return f"✅ 승인목록({o.get('date') or '-'}): 없음 — 승인 안 하면 그날 무거래"
    parts = []
    for c, info in codes.items():
        if not isinstance(info, dict):
            info = {"name": info}
        lgs = info.get("legs")
        if not lgs and info.get("price"):       # 구형 단일가 호환
            lgs = [{"price": info["price"], "target": info.get("target")}]
        if not lgs:
            parts.append(f"{info.get('name', c)}({c}) 시가-dip%")
        else:
            seg = ", ".join(
                f"{int(l['price']):,}" + (f"→{int(l['target']):,}" if l.get("target") else "")
                + (f"x{l['qty']}주" if l.get("qty") else "")
                for l in lgs)
            parts.append(f"{info.get('name', c)}({c}) [{seg}]")
    return f"✅ 승인목록({o['date']}): " + " · ".join(parts)
