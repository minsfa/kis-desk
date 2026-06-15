"""통합 PoC — 시간대 자동 감지로 미국/국내/NXT 사고팔기 (최종 정리본).

세션(KST):
  nxt_pre   08:00~08:50  국내 NXT 프리마켓 → SOR 라우팅, 지정가(+tick)
  kr_main   09:00~15:30  국내 메인(KRX)     → 시장가
  nxt_after 16:00~20:00  국내 NXT 애프터    → SOR 라우팅, 지정가(+tick)
  us_reg    22:30~05:00  미국 정규장(EDT)   → 해외 지정가
  closed    그 외        → dry 미리보기만(체결 불가)

체결확인은 잔고(balance) 기반. 라이브는 (--live AND .env DRY_RUN=false)만.
OpenClaw는 이 명령 하나(`pocall`)만 시각 맞춰 실행하면 됨.
"""
from __future__ import annotations
import time
from datetime import datetime, timezone, timedelta

from .client import KisClient
from . import market, orders, overseas, report
from .tick import round_tick

KST = timezone(timedelta(hours=9))

# 종목 (일반=비레버리지). SCHD(ETF)는 해외ETP 미신청이라 미국은 일반주식 T 사용.
KR_ETF = ("360750", "TIGER 미국S&P500")
KR_STOCK = ("316140", "우리금융지주")
US_STOCK = ("T", "NYSE", "AT&T")


def detect_session(now: datetime | None = None) -> str:
    n = now or datetime.now(KST)
    hm = n.hour * 60 + n.minute
    if 8 * 60 <= hm < 8 * 60 + 50:
        return "nxt_pre"
    if 9 * 60 <= hm < 15 * 60 + 30:
        return "kr_main"
    if 16 * 60 <= hm < 20 * 60:
        return "nxt_after"
    if hm >= 22 * 60 + 30 or hm < 5 * 60:
        return "us_reg"
    return "closed"


def _kr_held(c, code):
    for h in market.get_balance(c)["holdings"]:
        if h.get("code") == code:
            try:
                return int(float(h.get("qty") or 0))
            except Exception:
                return 0
    return 0


def _us_held(c, sym):
    for h in overseas.get_balance(c, excg="NYSE")["holdings"]:
        if h.get("symbol") == sym:
            try:
                return int(float(h.get("qty") or 0))
            except Exception:
                return 0
    return 0


def _kr_roundtrip(c, code, name, route, ordtype, live, L):
    try:
        prev = int(float(market.get_price(c, code)["price"]))
    except Exception as e:
        L.append(f"  시세오류 {e}"); return
    L.append(f"  현재가 {prev:,}원 | route={route} type={ordtype}")
    if not live:
        L.append(f"  [dry] {name} 1주 {ordtype}"); return
    if ordtype == "market":
        b = orders.buy(c, code, 1, market=True, live=True, exchange=route)
    else:
        b = orders.buy(c, code, 1, price=round_tick(prev * 1.005, up=True),
                       market=False, live=True, exchange=route)
    L.append(f"  매수 ok={b.get('ok')} no={b.get('order_no')} msg={b.get('msg')}")
    if not b.get("ok"):
        return
    q = 0
    for _ in range(10):
        time.sleep(2); q = _kr_held(c, code)
        if q >= 1:
            break
    if q < 1:
        cx = orders.cancel(c, b.get("order_no"), b.get("org_no") or "", 1, live=True)
        L.append(f"  미체결→취소 ok={cx.get('ok')}"); return
    L.append("  매수체결 1주")
    if ordtype == "market":
        sr = orders.sell(c, code, 1, market=True, live=True, exchange=route)
    else:
        sr = orders.sell(c, code, 1, price=round_tick(prev * 0.995, up=False),
                         market=False, live=True, exchange=route)
    L.append(f"  매도 ok={sr.get('ok')} no={sr.get('order_no')} msg={sr.get('msg')}")
    for _ in range(10):
        time.sleep(2); q = _kr_held(c, code)
        if q == 0:
            break
    L.append(f"  청산 보유={q}" + (" (flat)" if q == 0 else " ⚠️미청산"))


def _us_roundtrip(c, live, L):
    sym, ex, name = US_STOCK
    try:
        p = overseas.get_price(c, sym, ex); last = float(p["last"])
    except Exception as e:
        L.append(f"  시세오류 {e}"); return
    L.append(f"  {name}({sym}) last=${last} tvol={p.get('tvol')}")
    if not live:
        L.append(f"  [dry] {sym} 1주 지정가 ~${last}"); return
    b = overseas.buy(c, sym, 1, round(last * 1.003, 2), excg=ex, live=True)
    L.append(f"  매수 ok={b.get('ok')} no={b.get('order_no')} msg={b.get('msg')}")
    if not b.get("ok"):
        return
    q = 0
    for _ in range(10):
        time.sleep(2); q = _us_held(c, sym)
        if q >= 1:
            break
    if q < 1:
        overseas.cancel(c, sym, b.get("order_no"), 1, excg=ex, live=True)
        L.append("  미체결→취소"); return
    L.append("  매수체결 1주")
    sr = overseas.sell(c, sym, 1, round(last * 0.997, 2), excg=ex, live=True)
    L.append(f"  매도 ok={sr.get('ok')} no={sr.get('order_no')} msg={sr.get('msg')}")
    for _ in range(10):
        time.sleep(2); q = _us_held(c, sym)
        if q == 0:
            break
    L.append(f"  청산 보유={q}" + (" (flat)" if q == 0 else " ⚠️미청산"))


def run(c: KisClient, session: str | None = None, live: bool = False) -> str:
    sess = session or detect_session()
    go_live = live and (not c.s.dry_run)
    L = [f"=== 통합 PoC | 세션={sess} | {'LIVE' if go_live else 'DRY'} | {datetime.now(KST):%H:%M KST} ==="]

    if sess in ("nxt_pre", "nxt_after"):
        for code, name in (KR_STOCK, KR_ETF):
            L.append(f"\n[NXT-{sess} · {name}]")
            _kr_roundtrip(c, code, name, "SOR", "limit", go_live, L)
    elif sess == "kr_main":
        for code, name in (KR_ETF, KR_STOCK):
            L.append(f"\n[KR메인 · {name}]")
            _kr_roundtrip(c, code, name, "KRX", "market", go_live, L)
    elif sess == "us_reg":
        L.append("\n[US 정규장]")
        _us_roundtrip(c, go_live, L)
    else:
        L.append("\n장 시간 아님(closed) → dry 미리보기(국내 메인 기준), 체결 불가")
        for code, name in (KR_ETF, KR_STOCK):
            L.append(f"\n[preview · {name}]")
            _kr_roundtrip(c, code, name, "KRX", "market", False, L)

    L.append("\n요약:\n" + report.summarize_today())
    return "\n".join(L)
