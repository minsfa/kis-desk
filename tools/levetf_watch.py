"""단일종목(삼성전자·SK하이닉스) 레버리지 ETF/ETN 과열 모니터 — 크론용.

배경: 금감원장(2026-06-22)이 "단일종목 레버리지 ETF가 회전율 200% 육박,
증권사만 수수료 챙기고 투자자 실익 없음"을 최대 우려로 지적 → 그 회전율·
거래대금을 추적한다. KRX OPEN API(ETF/ETN 일별 전종목)를 받아 삼성전자/
SK하이닉스 기반 단일종목 레버리지·인버스 상품을 골라낸다.

회전율 극단(어떤 상품 100%↑) 또는 합계 거래대금 급증 시 🚨 한 줄, 평소 OK(침묵).
--show 로 회전율 내림차순 상세 표를 항상 출력. fail-soft(빈배열/실패 시 OK).

src/ 밖(tools/)이라 라이브 매매 무결성 게이트와 무관. KRX 호출은 전부 읽기전용.

지표(각 상품):
  회전율(%) = ACC_TRDVAL / AUM × 100  (일간 회전율. ETF AUM=INVSTASST_NETASST_TOTAMT,
              ETN AUM=INDIC_VAL_AMT). 100%↑ = 하루에 순자산보다 많이 거래된 극단 과열.
  NAV괴리(%) = (종가 - NAV) / NAV × 100  (ETF=NAV, ETN=PER1SECU_INDIC_VAL 주당지표가치).

⚠️ KRX 는 전일치가 익일 08시 갱신 — 당일/주말/휴일은 빈 배열 → graceful(OK).
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

from tools import krx_api

KST = timezone(timedelta(hours=9))

# 단일종목 식별 키워드
STOCK_KW = {"삼성전자": "삼성", "SK하이닉스": "하이닉스", "하이닉스": "하이닉스"}
LEV_KW = ("레버리지", "2X", "2x", "2배")
INV_KW = ("인버스", "-2X", "-2x", "곱버스")

# 트리거 임계선
TURN_HOT = 100.0     # 어떤 상품 일간 회전율 100%↑ = 극단 과열
TURN_WARN = 200.0    # 금감원장 지적선(200% 육박)
VAL_SURGE = 5.0      # 합계 총거래대금 5조원↑ 시 거래 폭증으로 간주


def _f(v) -> float | None:
    """문자열 숫자 → float, 실패 시 None."""
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def _base_stock(nm: str) -> str | None:
    """종목명에서 기초 단일종목(삼성/하이닉스) 판별. 못 찾으면 None.
    'SK하이닉스'를 먼저 확인(부분일치 우선순위)."""
    if "하이닉스" in nm:
        return "하이닉스"
    if "삼성전자" in nm:
        return "삼성"
    return None


def _is_leverage(nm: str) -> bool:
    return any(k in nm for k in LEV_KW)


def _is_inverse(nm: str) -> bool:
    return any(k in nm for k in INV_KW)


def _classify(nm: str, idx_nm: str) -> tuple[str, str] | None:
    """(기초종목, 방향) 반환. 단일종목 레버리지/인버스가 아니면 None.

    1차: 종목명에 (삼성/하이닉스) AND (레버리지/2X/2배 또는 인버스/-2X).
    2차(보강): 종목명 패턴이 안 맞으면 IDX_IND_NM(기초지수명)이 삼성전자/SK하이닉스
              단일종목지수이고 이름에 레버리지/인버스가 있는지로 보강."""
    base = _base_stock(nm)
    lev = _is_leverage(nm)
    inv = _is_inverse(nm)

    # 2차 보강: 이름에 단일종목명이 없어도 기초지수가 단일종목지수면 인정
    if base is None and idx_nm:
        if "하이닉스" in idx_nm:
            base = "하이닉스"
        elif "삼성전자" in idx_nm:
            base = "삼성"
    if base is None:
        return None
    # 단일종목지수 기반인지 확인(혼합/밸류체인/채권혼합 등 배제):
    # 이름이나 기초지수명에 "단일종목" 또는 단일종목지수 형태가 있어야 함.
    single = ("단일종목" in nm) or ("단일종목" in idx_nm) or \
             (base == "하이닉스" and "하이닉스" in (idx_nm or "") and "밸류체인" not in nm) or \
             (base == "삼성" and "삼성전자" in (idx_nm or ""))
    # 레버리지/인버스 여부(이름 기준 우선, 없으면 지수명 보강)
    if not (lev or inv):
        if "레버리지" in (idx_nm or ""):
            lev = True
        elif "인버스" in (idx_nm or ""):
            inv = True
    if not (lev or inv):
        return None
    if not single:
        return None
    # 인버스 우선(곱버스 "인버스2X"는 "2X"도 포함하므로 inv 가 lev 보다 우선).
    return base, ("인버스" if inv else "레버리지")


def _collect(bas_dd: str, key: str | None = None) -> list[dict]:
    """ETF+ETN 전종목 fetch → 단일종목 레버리지/인버스 상품 리스트.
    각 항목: name, code, kind(ETF/ETN), direction(레버리지/인버스),
             base(삼성/하이닉스), turnover(%), trdval(원), aum(원), fluc(%), gap(NAV괴리%).
    데이터 없음(빈 배열)이면 빈 리스트."""
    out: list[dict] = []

    etf = krx_api.fetch_etf_bydd(bas_dd, key=key)
    for r in etf:
        nm = r.get("ISU_NM", "")
        cls = _classify(nm, r.get("IDX_IND_NM", ""))
        if not cls:
            continue
        base, direction = cls
        aum = _f(r.get("INVSTASST_NETASST_TOTAMT"))
        val = _f(r.get("ACC_TRDVAL"))
        close = _f(r.get("TDD_CLSPRC"))
        nav = _f(r.get("NAV"))
        out.append({
            "name": nm, "code": r.get("ISU_CD"), "kind": "ETF",
            "direction": direction, "base": base,
            "turnover": (val / aum * 100) if (aum and val is not None) else None,
            "trdval": val, "aum": aum, "fluc": _f(r.get("FLUC_RT")),
            "gap": ((close - nav) / nav * 100) if (nav and close is not None) else None,
        })

    etn = krx_api.fetch_etn_bydd(bas_dd, key=key)
    for r in etn:
        nm = r.get("ISU_NM", "")
        cls = _classify(nm, r.get("IDX_IND_NM", ""))
        if not cls:
            continue
        base, direction = cls
        aum = _f(r.get("INDIC_VAL_AMT"))
        val = _f(r.get("ACC_TRDVAL"))
        close = _f(r.get("TDD_CLSPRC"))
        ival = _f(r.get("PER1SECU_INDIC_VAL"))  # 주당지표가치(ETN 의 NAV 대응)
        out.append({
            "name": nm, "code": r.get("ISU_CD"), "kind": "ETN",
            "direction": direction, "base": base,
            "turnover": (val / aum * 100) if (aum and val is not None) else None,
            "trdval": val, "aum": aum, "fluc": _f(r.get("FLUC_RT")),
            "gap": ((close - ival) / ival * 100) if (ival and close is not None) else None,
        })

    out.sort(key=lambda x: (x["turnover"] is None, -(x["turnover"] or 0)))
    return out


# ─────────────────────────────────────────────────────────────────────────
# 집계 헬퍼 (market_log.py 재사용용)
# ─────────────────────────────────────────────────────────────────────────
def aggregate(items: list[dict]) -> dict:
    """단일종목 레버리지/인버스 상품 리스트 → 합계 지표.
    반환:
      total_val   : 삼성+하이닉스 단일종목 레버리지/인버스 ETF+ETN 총거래대금(원)
      wavg_turn   : 그 상품들 거래대금 가중 평균 회전율(%)
      sam_val/hynix_val : 계열별 총거래대금(원)
      sam_turn/hynix_turn : 계열별 거래대금 가중 평균 회전율(%)
      count       : 잡힌 상품 수."""
    def _wavg(rows):
        num = sum((r["trdval"] or 0) * r["turnover"]
                  for r in rows if r["trdval"] and r["turnover"] is not None)
        den = sum(r["trdval"] for r in rows if r["trdval"] and r["turnover"] is not None)
        return (num / den) if den else None

    sam = [r for r in items if r["base"] == "삼성"]
    hynix = [r for r in items if r["base"] == "하이닉스"]
    total_val = sum(r["trdval"] for r in items if r["trdval"])
    return {
        "total_val": total_val or None,
        "wavg_turn": _wavg(items),
        "sam_val": sum(r["trdval"] for r in sam if r["trdval"]) or None,
        "hynix_val": sum(r["trdval"] for r in hynix if r["trdval"]) or None,
        "sam_turn": _wavg(sam),
        "hynix_turn": _wavg(hynix),
        "count": len(items),
    }


def collect_aggregate(bas_dd: str, key: str | None = None) -> dict | None:
    """market_log.py 재사용용: 해당일 단일종목 레버리지 합계 지표 반환.
    데이터 없음/상품 0개면 None (graceful)."""
    items = _collect(bas_dd, key=key)
    if not items:
        return None
    return aggregate(items)


# ─────────────────────────────────────────────────────────────────────────
# 영업일 추정
# ─────────────────────────────────────────────────────────────────────────
def _latest_biz_dd(key: str | None) -> tuple[str, list[dict]]:
    """전일치 익일08시 갱신 → 오늘부터 거슬러 데이터가 있는 첫 영업일 탐색.
    (basDd, items) 반환. 최대 8일 거슬러 시도, 없으면 (마지막시도일, [])."""
    today = datetime.now(KST).date()
    last_dd = ""
    for off in range(1, 9):
        dd = (today - timedelta(days=off)).strftime("%Y%m%d")
        last_dd = dd
        try:
            items = _collect(dd, key=key)
        except Exception:
            continue
        if items:
            return dd, items
    return last_dd, []


# ─────────────────────────────────────────────────────────────────────────
# 출력
# ─────────────────────────────────────────────────────────────────────────
def _fmt_date(s: str) -> str:
    return f"{s[:4]}/{s[4:6]}/{s[6:8]}" if len(s) == 8 else s


def _val_s(v) -> str:
    """원 → 억/조 환산 표기."""
    if v is None:
        return "—"
    jo = v / 1e12
    if abs(jo) >= 1:
        return f"{jo:,.2f}조"
    return f"{v / 1e8:,.0f}억"


def _show(bas_dd: str, items: list[dict]):
    """회전율 내림차순 상세 표 + 계열 합계."""
    print(f"단일종목(삼성전자·SK하이닉스) 레버리지/인버스 ETF·ETN 과열 모니터 "
          f"[{_fmt_date(bas_dd)}]  (회전율 내림차순)")
    print("  회전율 = 일간 거래대금/순자산 ×100 (100%↑ = 하루에 순자산보다 많이 거래)\n")
    hdr = (f"{'종목명':<34} {'코드':>6} {'구분':>10} {'기초':>5} "
           f"{'회전율%':>9} {'거래대금':>10} {'AUM':>10} {'등락%':>7} {'NAV괴리%':>9}")
    print(hdr)
    print("─" * len(hdr))
    for r in items:
        turn = f"{r['turnover']:,.1f}" if r["turnover"] is not None else "—"
        fluc = f"{r['fluc']:+.2f}" if r["fluc"] is not None else "—"
        gap = f"{r['gap']:+.2f}" if r["gap"] is not None else "—"
        kind = f"{r['kind']}/{r['direction']}"
        # 한글 폭 보정 위해 ljust 대신 단순 정렬(가독성 우선)
        print(f"{r['name']:<34} {r['code']:>6} {kind:>10} {r['base']:>5} "
              f"{turn:>9} {_val_s(r['trdval']):>10} {_val_s(r['aum']):>10} "
              f"{fluc:>7} {gap:>9}")

    agg = aggregate(items)
    print("\n[계열 합계]")
    for base, vkey, tkey in (("삼성", "sam_val", "sam_turn"),
                             ("하이닉스", "hynix_val", "hynix_turn")):
        v = agg[vkey]
        t = agg[tkey]
        t_s = f"{t:,.1f}%" if t is not None else "—"
        print(f"  {base}계열: 총거래대금 {_val_s(v):>10}  거래대금가중 평균회전율 {t_s}")
    tw = f"{agg['wavg_turn']:,.1f}%" if agg["wavg_turn"] is not None else "—"
    print(f"  전체({agg['count']}종목): 총거래대금 {_val_s(agg['total_val']):>10}  "
          f"가중 평균회전율 {tw}")


# ─────────────────────────────────────────────────────────────────────────
def main():
    want_show = "--show" in sys.argv

    try:
        key = krx_api._auth_key()
    except Exception:
        key = None
    if not key:
        print("OK (KRX_AUTH_KEY 없음 — 체크 불가)")
        return

    try:
        bas_dd, items = _latest_biz_dd(key)
    except Exception as e:
        print(f"OK (조회 실패: {str(e)[:60]})")
        return

    if not items:
        print(f"OK (단일종목 레버리지 데이터 없음 — 최근 영업일까지 빈 배열, "
              f"마지막 시도 {_fmt_date(bas_dd)})")
        return

    if want_show:
        _show(bas_dd, items)

    agg = aggregate(items)
    total_jo = (agg["total_val"] or 0) / 1e12
    # 회전율 극단 상품 추출
    hot = [r for r in items if r["turnover"] is not None and r["turnover"] >= TURN_HOT]
    triggered = bool(hot) or total_jo >= VAL_SURGE

    if triggered:
        flags = []
        if hot:
            top = hot[0]
            over_warn = [r for r in hot if r["turnover"] >= TURN_WARN]
            if over_warn:
                flags.append(f"회전율 {TURN_WARN:.0f}%↑ {len(over_warn)}종목"
                             f"(최고 {top['name']} {top['turnover']:,.0f}%)")
            else:
                flags.append(f"회전율 {TURN_HOT:.0f}%↑ {len(hot)}종목"
                             f"(최고 {top['name']} {top['turnover']:,.0f}%)")
        if total_jo >= VAL_SURGE:
            flags.append(f"총거래대금 {total_jo:,.1f}조 폭증")
        wavg = agg["wavg_turn"]
        wavg_s = f"평균회전율 {wavg:,.0f}%" if wavg is not None else ""
        print(f"🚨 단일종목 레버리지/인버스 ETP 과열 [{_fmt_date(bas_dd)}] "
              f"총거래대금 {total_jo:,.2f}조 {wavg_s} — {', '.join(flags)}. "
              f"회전율 급등 = 단기 투기 과열, 투자자 실익 점검 권장.")
    elif want_show:
        print(f"\nOK [{_fmt_date(bas_dd)}] 총거래대금 {total_jo:,.2f}조 — "
              f"트리거(회전율 {TURN_HOT:.0f}% / 총대금 {VAL_SURGE:.0f}조) 미도달.")
    else:
        print(f"OK 단일종목 레버리지 총거래대금 {total_jo:,.2f}조 "
              f"[{_fmt_date(bas_dd)}] — 트리거 미도달, 침묵")


if __name__ == "__main__":
    main()
