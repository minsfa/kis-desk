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
import statistics
import subprocess
from datetime import datetime, timedelta, timezone

from .client import KisClient
from . import overseas
from .regime import _us_closes
from .config import PROJECT_ROOT, US_EXCH
from .safety import SafetyError
from .logging_util import log_event

KST = timezone(timedelta(hours=9))
DIR = PROJECT_ROOT / "data" / "dca"
LEDGER = DIR / "ledger.csv"

# ---- 튜닝 ----
BUDGET_USD = 660.0                         # 월 예산(100만원 ≈ $660)
# (티커, 주문거래소 OVRS_EXCG_CD, 기본비중). 주문/잔고는 이 코드(NASD/NYSE/AMEX),
# 시세조회(_us_closes/get_price)는 US_EXCH로 시세코드(NAS/NYS/AMS)로 변환해 사용.
UNIVERSE = [("XLE", "AMEX", 0.70), ("XOM", "NYSE", 0.30)]
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
    """[구버전, 미사용] 최근 범위 내 위치. min-max는 추세장에 약해 _strength로 대체됨."""
    if len(cl) < 20:
        return None
    lo, hi = min(cl), max(cl)
    return (cl[0] - lo) / (hi - lo) if hi > lo else 0.5


# === 추세인지 가치 사이징 (Discussion #8) — min-max 대체 ===
# 핵심: 변동성 정규화 z-score(딥) × 추세 댐퍼(falling knife 방지) × 상대강도 틸트.
Z_WIN = 20            # z-score 윈도우(일)
Z_FULL = 2.0          # |z|=2σ에서 최대 보정
DIP_MAX = 2.0         # 최저 z(쌈)에서 매수배수 상한
TREND_N = 90          # 추세 MA(일). KIS 해외일봉 ~100개 상한 → 200일 대신 90일
SLOPE_SHIFT = 5       # 추세 기울기 판정용 시프트
RS_LB = 63            # 상대강도 룩백(~3개월)
RS_FAST = 50          # XLE/XLK 비율 추세 MA
DIP_TRIGGER = 1.15    # 블렌드 강도 이 이상이면 월중에도 즉시 매수(좋은 진입)
STRENGTH_CAP = 1.8    # 월 총지출 = budget×블렌드강도, 상한(자금/리스크 통제)
USE_VALUATION = True  # 거시 가치 오버레이(macro.valuation_factor) 적용. False면 기술적 사이징만


def _zfactor(cl: list[float]) -> tuple[float, float | None]:
    """z-score 딥팩터 [0.5,2.0]. 싸면(z<0)>1, 비싸면(z>0)<1. 변동성 정규화라 범위 드리프트 면역."""
    if len(cl) < Z_WIN:
        return 1.0, None
    win = cl[:Z_WIN]
    mu = statistics.fmean(win)
    sd = statistics.pstdev(win)
    if sd == 0:
        return 1.0, 0.0
    z = (cl[0] - mu) / sd
    if z <= 0:
        f = 1.0 + (DIP_MAX - 1.0) * min(1.0, -z / Z_FULL)   # [1.0, 2.0]
    else:
        f = 1.0 - 0.5 * min(1.0, z / Z_FULL)                # [0.5, 1.0]
    return f, round(z, 2)


def _trendfactor(cl: list[float]) -> tuple[float, str]:
    """추세 댐퍼: 가격<하락중 MA=하락추세 0.5(falling knife 방지) / 가격<상승 MA=눌림 0.8 / 상승 1.0."""
    if len(cl) < TREND_N + SLOPE_SHIFT:
        return 1.0, "n/a"
    sma_now = statistics.fmean(cl[:TREND_N])
    sma_prev = statistics.fmean(cl[SLOPE_SHIFT:SLOPE_SHIFT + TREND_N])
    below = cl[0] < sma_now
    falling = sma_now < sma_prev
    if below and falling:
        return 0.5, "하락추세"
    if below:
        return 0.8, "눌림"
    return 1.0, "상승"


def _rsfactor(cl: list[float], spy: list[float], xlk: list[float]) -> tuple[float, str]:
    """상대강도 틸트: 에너지가 SPY 대비 우위 & XLE/XLK 비율>50일선 → 1.2 / 열위 → 0.85 / 중립 1.0."""
    if len(cl) <= RS_LB or len(spy) <= RS_LB or len(xlk) < RS_FAST:
        return 1.0, "n/a"
    relret = (cl[0] / cl[RS_LB]) / (spy[0] / spy[RS_LB]) - 1
    n = min(len(cl), len(xlk), RS_FAST)
    ratio = [cl[i] / xlk[i] for i in range(n)]
    up = relret > 0 and ratio[0] > statistics.fmean(ratio)
    dn = relret < 0 and ratio[0] < statistics.fmean(ratio)
    return (1.2 if up else (0.85 if dn else 1.0)), f"vsSPY{relret*100:+.0f}%"


def _strength(cl: list[float], spy: list[float], xlk: list[float]) -> tuple[float, dict]:
    """종목 매수강도 = 딥 × 추세 × RS, [0.3,2.5]. (강도, 상세)."""
    dipf, z = _zfactor(cl)
    trf, trs = _trendfactor(cl)
    rsf, rss = _rsfactor(cl, spy, xlk)
    s = max(0.3, min(2.5, dipf * trf * rsf))
    return s, {"z": z, "dip": round(dipf, 2), "trend": trf, "trend_s": trs,
               "rs": rsf, "rs_s": rss}


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
    # 1) 모니터링 + 추세인지 가치 사이징. SPY/XLK는 상대강도 벤치마크(1회 조회).
    try:
        spy = _us_closes(c, "SPY", "AMS")
        xlk = _us_closes(c, "XLK", "AMS")
    except Exception:
        spy, xlk = [], []
    mon = {}
    strengths = {}
    for tk, excd, w in UNIVERSE:
        try:
            cl = _us_closes(c, tk, US_EXCH.get(excd, excd))  # 주문코드→시세코드
            if not cl:
                mon[tk] = {"err": "시세없음"}; continue
            s, detail = _strength(cl, spy, xlk)
            strengths[tk] = s
            mon[tk] = {"last": cl[0], "strength": round(s, 2), **detail}
        except Exception as e:
            mon[tk] = {"err": str(e)}
    if not strengths:
        return {"skip": "시세 없음(미국장 마감/조회불가)", "mon": mon}

    # 2) 이번 달 이미 매수?
    if _bought_this_month():
        return {"status": "이번달 매수 완료", "month": _month(), "mon": mon}

    # 2.5) 거시 가치 오버레이(섹터 공통배수). 실패하면 1.0 중립(매매 안 막음).
    val_f, val_why = 1.0, ["오버레이 꺼짐"]
    if USE_VALUATION:
        try:
            from . import macro
            val_f, val_why = macro.valuation_factor()
        except Exception as e:
            val_f, val_why = 1.0, [f"macro 오류→중립({e})"]
        strengths = {tk: max(0.3, min(2.5, s * val_f)) for tk, s in strengths.items()}

    # 3) 블렌드 강도(=월 총지출 배수) + 매수 타이밍
    wsum = sum(w for tk, _, w in UNIVERSE if tk in strengths)
    blended = sum(w * strengths[tk] for tk, _, w in UNIVERSE if tk in strengths) / (wsum or 1)
    near_monthend = datetime.now(KST).day >= MONTHEND_DAY
    if not (blended >= DIP_TRIGGER or near_monthend):
        return {"status": "대기(좋은 진입 기다리는 중)", "blended": round(blended, 2),
                "note": f"블렌드강도<{DIP_TRIGGER}(저평가 약함) 또는 {MONTHEND_DAY}일 이후 집행",
                "mon": mon}

    # 4) 매수 실행 — 종목별 강도로 사이징(falling knife 자동 축소). 총지출은 STRENGTH_CAP로 캡.
    scale = min(1.0, STRENGTH_CAP / blended) if blended > 0 else 1.0
    reason = "딥강화" if blended >= DIP_TRIGGER else "월말집행"
    placed = []
    for tk, excd, w in UNIVERSE:
        m = mon.get(tk, {})
        px = m.get("last")
        if not px:
            placed.append({"ticker": tk, "skip": "시세없음"}); continue
        strength = round(strengths.get(tk, 1.0) * scale, 3)   # 종목별 유효강도
        target = budget * w * strength
        qty = int(target // px)
        if qty < 1:
            _log({"ts": ts, "month": _month(), "action": "skip", "ticker": tk, "price": px,
                  "usd": round(target, 2), "strength": round(strength, 2), "pos": m.get("z"),
                  "note": f"예산부족(<1주, 강도{round(strength,2)})"}, live)
            placed.append({"ticker": tk, "skip": f"예산부족(target ${target:.0f} < 1주 ${px:.0f})"})
            continue
        limit = round(px * BUY_SLIP, 2)
        try:
            res = overseas.buy(c, tk, qty, limit, excg=excd, live=live)
            ok = bool(res.get("ok") or res.get("dry_run"))
            _log({"ts": ts, "month": _month(), "action": "buy", "ticker": tk, "qty": qty,
                  "price": limit, "usd": round(qty * limit, 2), "strength": round(strength, 2),
                  "pos": m.get("z"),
                  "note": f"{reason} {res.get('order_no','')}"}, live)
            placed.append({"ticker": tk, "ok": ok, "qty": qty, "limit": limit,
                           "order_no": res.get("order_no"), "dry_run": res.get("dry_run")})
        except SafetyError as e:
            _log({"ts": ts, "month": _month(), "action": "blocked", "ticker": tk, "qty": qty,
                  "price": limit, "usd": round(qty * limit, 2), "note": f"안전차단:{e}"}, live)
            placed.append({"ticker": tk, "blocked": str(e)})
        except Exception as e:
            placed.append({"ticker": tk, "error": str(e)})
    return {"status": f"매수 실행({reason}, 블렌드강도{round(blended,2)})",
            "blended": round(blended, 2), "valuation": round(val_f, 2),
            "val_why": val_why, "budget": budget, "placed": placed}


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

    # 현재가는 해외 잔고(usbalance)의 보유별 now_price에서 가져온다 — 거래소 무관·정확.
    # (단일 시세 호출은 종목 상장 거래소가 기본값과 다르면 빈값→0→합계 왜곡되므로 사용 안 함.)
    now_by_tk: dict[str, float] = {}
    if c is not None:
        for excg in ("NASD", "NYSE", "AMEX"):   # 잔고는 거래소별 조회 — 보유 종목이 흩어져 있어 모두 합침
            try:
                bal = overseas.get_balance(c, excg=excg)
            except Exception:
                continue
            for h in bal.get("holdings", []):
                sym = h.get("symbol")
                try:
                    px = float(h.get("now_price") or 0)
                except (TypeError, ValueError):
                    px = 0.0
                if sym and px > 0:
                    now_by_tk[sym] = px

    tot_inv = 0.0
    tot_val = 0.0          # 현재가를 구한 종목들의 평가 합
    tot_val_inv = 0.0      # 그 종목들의 투입 합(손익률을 같은 모수로 계산하려고 분리)
    have_price = False
    for tk, d in by_tk.items():
        avg = d["usd"] / d["qty"] if d["qty"] else 0
        line = f"   {tk}: {d['qty']}주 · 평단 ${avg:.2f} · 투입 ${d['usd']:.0f}"
        tot_inv += d["usd"]
        px = now_by_tk.get(tk, 0.0)
        if px > 0:
            val = px * d["qty"]
            tot_val += val
            tot_val_inv += d["usd"]
            have_price = True
            line += f" · 현재 ${px:.2f} → 평가 ${val:.0f} ({(val/d['usd']-1)*100:+.1f}%)"
        elif c is not None:
            # 현재가 미상 — 거짓 -%를 만들지 않는다. 평단가로 대체 표기(평가는 합계에서 제외).
            line += " · 현재가 미상(잔고에 없음)"
        L.append(line)

    summary = f"  총 투입 ${tot_inv:.0f}"
    if have_price:
        # 손익률은 현재가를 구한 종목들의 투입(tot_val_inv) 대비로만 계산 — 미상 종목이 섞여도 왜곡 없음.
        pnl = (tot_val / tot_val_inv - 1) * 100 if tot_val_inv else 0.0
        summary += f" · 총 평가 ${tot_val:.0f} ({pnl:+.1f}%)"
        if tot_val_inv < tot_inv:   # 일부 종목 현재가 미상 → 평가 합은 부분 합임을 명시
            summary += " · 일부 현재가 미상(평가/손익은 시세 확인분만)"
    L.append(summary)
    L.append(f"💾 {LEDGER}")
    return "\n".join(L)


UNIVERSE_EXCH = [(tk, ex) for tk, ex, _ in UNIVERSE]
