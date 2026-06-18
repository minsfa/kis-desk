"""월간 에너지 DCA 하버스 — 매일 체크, 월 1회 매수(딥 강화·고점 절반), 월간 원장.

"매일 단타"(us_stab) 대신 더 긴 렌즈: 미국 에너지 자산(XLE/XOM)을 매달 한 번 달러로 적립.
- 매일 cron이 `check` → 시세·딥 모니터링. **이번 달 미매수 + (딥 or 월말)** 이면 그날 매수.
- 딥(최근 범위 하단)이면 강화(×1.2), 고점(상단)이면 절반(×0.5), 평소 ×1.0.
- 원유 선물 ETF(USO) 회피 — 롤오버 함정. 주식형(XLE 섹터ETF + XOM 대장주)만.
- 월간 원장(ledger.csv)으로 매수 기록 → `report`로 리뷰.
주문은 overseas.buy(USD 안전게이트) 경유. 소액부터(현 MAX_ORDER_USD 내 = XLE 위주, XOM은 한도 상향 필요).
"""
from __future__ import annotations
import csv
import subprocess
from datetime import datetime, timedelta, timezone

from .client import KisClient
from . import overseas
from .regime import _us_closes
from .config import PROJECT_ROOT
from .safety import SafetyError
from .logging_util import log_event

KST = timezone(timedelta(hours=9))
DIR = PROJECT_ROOT / "data" / "dca"
LEDGER = DIR / "ledger.csv"

# ---- 튜닝 ----
BUDGET_USD = 660.0                         # 월 예산(100만원 ≈ $660)
UNIVERSE = [("XLE", "AMS", 0.70), ("XOM", "NYS", 0.30)]  # (티커, 거래소, 기본비중)
DIP_LOW, HIGH = 0.40, 0.80                # 최근 범위 내 위치: <0.4 딥강화 / >0.8 고점절반
MONTHEND_DAY = 25                         # 이 날 이후엔 딥 없어도 월 적립 집행
BUY_SLIP = 1.002                          # 체결되게 현재가 +0.2% 지정가


# 실거래 직전 트레이딩 코드 무결성 게이트 — 커밋되지 않은 src/·전략 수정이 있으면
# 라이브 주문을 거부한다. (예: 운영봇이 BUDGET/UNIVERSE 등을 임의 수정 → 검토 없이 집행되는 사고 차단)
# 변경은 반드시 git 커밋(=사람 검토)을 거쳐야 라이브에 반영된다. 읽기/드라이런은 영향 없음.
_GUARDED_PATHS = ["src", "config/strategy.json"]


def _src_dirty() -> str | None:
    """src/·전략 파일에 커밋 안 된 변경이 있으면 그 목록(문자열) 반환, 깨끗하면 None."""
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain", "--"] + _GUARDED_PATHS,
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return f"git 상태확인 실패: {out.stderr.strip() or 'rc=' + str(out.returncode)}"
        dirty = out.stdout.strip()
        return dirty or None
    except Exception as e:
        return f"git 확인 예외: {e}"


def _assert_live_integrity():
    dirty = _src_dirty()
    if dirty:
        raise SafetyError(
            "트레이딩 코드 무결성 위반 — src/·전략에 커밋 안 된 변경 존재로 라이브 매매 거부.\n"
            "  변경분을 커밋(사람 검토)한 뒤 재시도하세요.\n" + dirty
        )


def _month() -> str:
    return f"{datetime.now(KST):%Y-%m}"


def _position(cl: list[float]) -> float | None:
    """최근 ~5개월 범위 내 위치(0=바닥,1=천장)."""
    if len(cl) < 20:
        return None
    lo, hi = min(cl), max(cl)
    return (cl[0] - lo) / (hi - lo) if hi > lo else 0.5


def _ledger_rows() -> list[dict]:
    if not LEDGER.exists():
        return []
    return list(csv.DictReader(open(LEDGER, encoding="utf-8")))


def _bought_this_month() -> bool:
    m = _month()
    return any(r.get("month") == m and r.get("action") == "buy" for r in _ledger_rows())


def _log(row: dict, live: bool = True):
    if not live:        # dry-run은 원장 오염 금지(경로만 검증)
        return
    DIR.mkdir(parents=True, exist_ok=True)
    cols = ["ts", "month", "action", "ticker", "qty", "price", "usd", "strength", "pos", "note"]
    new = not LEDGER.exists()
    with open(LEDGER, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def check(c: KisClient, budget: float = BUDGET_USD, live: bool = False) -> dict:
    """매일 호출. 모니터링 + 조건 충족 시 월 1회 매수."""
    if live:
        _assert_live_integrity()   # 라이브일 때만: 커밋 안 된 트레이딩 코드 변경이면 거부
    ts = f"{datetime.now(KST):%Y-%m-%d %H:%M:%S}"
    # 1) 모니터링: 각 티커 현재가·위치
    mon = {}
    for tk, excd, w in UNIVERSE:
        try:
            cl = _us_closes(c, tk, excd)
            mon[tk] = {"last": cl[0] if cl else None, "pos": _position(cl)}
        except Exception as e:
            mon[tk] = {"err": str(e)}
    poss = [m["pos"] for m in mon.values() if m.get("pos") is not None]
    if not poss:
        return {"skip": "시세 없음(미국장 마감/조회불가)", "mon": mon}
    avg_pos = sum(poss) / len(poss)

    # 2) 이번 달 이미 매수?
    if _bought_this_month():
        return {"status": "이번달 매수 완료", "month": _month(), "avg_pos": round(avg_pos, 2), "mon": mon}

    # 3) 매수 강도/타이밍
    is_dip = avg_pos < DIP_LOW
    is_high = avg_pos > HIGH
    strength = 1.2 if is_dip else (0.5 if is_high else 1.0)
    near_monthend = datetime.now(KST).day >= MONTHEND_DAY
    if not (is_dip or near_monthend):
        return {"status": "대기(딥 기다리는 중)", "avg_pos": round(avg_pos, 2),
                "note": f"딥<{DIP_LOW} 또는 {MONTHEND_DAY}일 이후 집행", "mon": mon}

    # 4) 매수 실행
    reason = "딥강화" if is_dip else ("월말집행" if near_monthend else "정상")
    placed = []
    for tk, excd, w in UNIVERSE:
        m = mon.get(tk, {})
        px = m.get("last")
        if not px:
            placed.append({"ticker": tk, "skip": "시세없음"}); continue
        target = budget * strength * w
        qty = int(target // px)
        if qty < 1:
            _log({"ts": ts, "month": _month(), "action": "skip", "ticker": tk, "price": px,
                  "usd": round(target, 2), "strength": strength, "pos": round(m.get("pos") or 0, 2),
                  "note": f"예산부족(<1주, 강도{strength})"}, live)
            placed.append({"ticker": tk, "skip": f"예산부족(target ${target:.0f} < 1주 ${px:.0f})"})
            continue
        limit = round(px * BUY_SLIP, 2)
        try:
            res = overseas.buy(c, tk, qty, limit, excg=excd, live=live)
            ok = bool(res.get("ok") or res.get("dry_run"))
            _log({"ts": ts, "month": _month(), "action": "buy", "ticker": tk, "qty": qty,
                  "price": limit, "usd": round(qty * limit, 2), "strength": strength,
                  "pos": round(m.get("pos") or 0, 2),
                  "note": f"{reason} {res.get('order_no','')}"}, live)
            placed.append({"ticker": tk, "ok": ok, "qty": qty, "limit": limit,
                           "order_no": res.get("order_no"), "dry_run": res.get("dry_run")})
        except SafetyError as e:
            _log({"ts": ts, "month": _month(), "action": "blocked", "ticker": tk, "qty": qty,
                  "price": limit, "usd": round(qty * limit, 2), "note": f"안전차단:{e}"}, live)
            placed.append({"ticker": tk, "blocked": str(e)})
        except Exception as e:
            placed.append({"ticker": tk, "error": str(e)})
    return {"status": f"매수 실행({reason}, 강도{strength})", "avg_pos": round(avg_pos, 2),
            "budget": budget, "placed": placed}


def report(c: KisClient | None = None) -> str:
    rows = [r for r in _ledger_rows() if r.get("action") == "buy"]
    L = ["💵 에너지 DCA 원장 — 월간 적립 기록"]
    if not rows:
        return "\n".join(L + ["  (아직 매수 기록 없음)"])
    # 월별 집계
    bym = {}
    for r in rows:
        bym.setdefault(r["month"], []).append(r)
    L.append("  월별:")
    for m in sorted(bym):
        items = " · ".join(f"{r['ticker']} {r['qty']}주@${r['price']}" for r in bym[m])
        usd = sum(float(r["usd"]) for r in bym[m])
        L.append(f"   {m}: {items}  (${usd:.0f})")
    # 티커별 누적 평단·수량
    L.append("  누적(티커별 평단):")
    by_tk = {}
    for r in rows:
        d = by_tk.setdefault(r["ticker"], {"qty": 0, "usd": 0.0})
        d["qty"] += int(float(r["qty"])); d["usd"] += float(r["usd"])
    tot_inv = 0.0; tot_val = 0.0
    for tk, d in by_tk.items():
        avg = d["usd"] / d["qty"] if d["qty"] else 0
        line = f"   {tk}: {d['qty']}주 · 평단 ${avg:.2f} · 투입 ${d['usd']:.0f}"
        tot_inv += d["usd"]
        if c is not None:
            try:
                px = float(overseas.get_price(c, tk, dict(UNIVERSE_EXCH).get(tk, "AMS")).get("last") or 0)
                if px > 0:
                    val = px * d["qty"]; tot_val += val
                    line += f" · 현재 ${px:.2f} → 평가 ${val:.0f} ({(val/d['usd']-1)*100:+.1f}%)"
            except Exception:
                pass
        L.append(line)
    L.append(f"  총 투입 ${tot_inv:.0f}" + (f" · 총 평가 ${tot_val:.0f} ({(tot_val/tot_inv-1)*100:+.1f}%)" if tot_val else ""))
    L.append(f"💾 {LEDGER}")
    return "\n".join(L)


UNIVERSE_EXCH = [(tk, ex) for tk, ex, _ in UNIVERSE]
