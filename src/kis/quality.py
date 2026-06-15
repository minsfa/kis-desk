"""사업 질·재무 건전성 축(Phase E) — '가치 함정' 판별.

NAV/PBR은 "자산이 싸다"만 본다. 본업이 사양·적자·고부채면 그 싼 게 함정일 수 있다.
이 모듈은 KIS 재무 API로 수익성·성장·부채를 받아 quality 점수와 함정 플래그를 만든다.

데이터(연결 기준, 분기 누적):
  · 재무비율 FHKST66430300 : ROE(roe_val), 부채비율(lblt_rate), 매출증가율(grs), 영업이익증가율(bsop_prfi_inrt)
  · 안정성비율 FHKST66430600: 차입금의존도(bram_depn), 유동비율(crnt_rate)

⚠️ 연결 기준이라 지주사 자체 재무가 아니라 그룹 합산이다(자회사 부실/부채 포함).
   그래서 NAV에서 이 부채를 빼면 자회사 시총과 이중계산 → 빼지 않고 '리스크 축'으로만 쓴다.
"""
from __future__ import annotations

from .client import KisClient
from .logging_util import log_event

FR_PATH = "/uapi/domestic-stock/v1/finance/financial-ratio"   # FHKST66430300
ST_PATH = "/uapi/domestic-stock/v1/finance/stability-ratio"   # FHKST66430600


def _f(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def get_quality(c: KisClient, code: str) -> dict:
    """수익성·성장·부채 최신값 + 적자이력. div=0(분기 누적)."""
    params = {"FID_DIV_CLS_CODE": "0", "fid_cond_mrkt_div_code": "J", "fid_input_iscd": code}
    fr = c.get(FR_PATH, "FHKST66430300", params).get("output") or []
    st = c.get(ST_PATH, "FHKST66430600", params).get("output") or []
    if not fr:
        return {"code": code, "error": "재무비율 없음"}

    cur = fr[0]                       # 최신 기간(내림차순 가정 — 아래서 보정)
    # stac_yymm 큰 게 최신이도록 정렬
    fr_sorted = sorted(fr, key=lambda r: r.get("stac_yymm") or "", reverse=True)
    cur = fr_sorted[0]
    roe = _f(cur.get("roe_val"))
    debt_ratio = _f(cur.get("lblt_rate"))         # 부채비율
    sales_grw = _f(cur.get("grs"))                # 매출증가율
    op_grw = _f(cur.get("bsop_prfi_inrt"))        # 영업이익증가율
    # 적자이력: 최근 ~8기간 중 ROE<0 (절대 손익 대신 ROE 음수로 판정)
    roes = [_f(r.get("roe_val")) for r in fr_sorted[:8]]
    loss_hist = any(x is not None and x < 0 for x in roes)
    # 매출 다년 정체: 최근 8기간 매출증가율 평균이 ~0 근처
    grs_vals = [_f(r.get("grs")) for r in fr_sorted[:8] if _f(r.get("grs")) is not None]
    sales_cagr_proxy = round(sum(grs_vals) / len(grs_vals), 1) if grs_vals else None

    borrow_dep = None
    if st:
        st_sorted = sorted(st, key=lambda r: r.get("stac_yymm") or "", reverse=True)
        borrow_dep = _f(st_sorted[0].get("bram_depn"))   # 차입금의존도(%)

    res = {
        "code": code, "period": cur.get("stac_yymm"),
        "roe": roe, "debt_ratio": debt_ratio, "borrow_dep": borrow_dep,
        "sales_grw": sales_grw, "op_grw": op_grw,
        "loss_hist": loss_hist, "sales_avg_grw": sales_cagr_proxy,
    }
    log_event("quality", code=code, roe=roe, borrow_dep=borrow_dep, loss_hist=loss_hist)
    return res


# ---- 점수화(튜닝 상수) ----
def quality_points(q: dict) -> tuple[float, list[str]]:
    """quality 점수(주로 감점) + 사유 태그. NAV/수급과 합산하면 함정주가 가라앉는다."""
    if q.get("error"):
        return 0.0, ["재무없음"]
    pts = 0.0
    notes = []
    roe = q.get("roe")
    if roe is not None:
        if roe < 0:
            pts -= 10; notes.append(f"적자(ROE{roe:.0f})")
        elif roe < 5:
            pts -= 4; notes.append(f"저ROE{roe:.1f}")
        elif roe < 10:
            pts += 2
        else:
            pts += 6; notes.append(f"고ROE{roe:.0f}")
    # 적자이력 감점은 '아직 회복 미흡(ROE<8)'일 때만 — 이미 고ROE면 과거 손실 무관
    if q.get("loss_hist") and roe is not None and 0 <= roe < 8:
        pts -= 5; notes.append("적자이력")
    op = q.get("op_grw")
    if op is not None:
        if op < 0:
            pts -= 6; notes.append(f"영익감소{op:.0f}%")
        elif op >= 20:
            pts += 3; notes.append(f"영익+{op:.0f}%")
    bd = q.get("borrow_dep")
    if bd is not None:
        if bd > 40:
            pts -= 6; notes.append(f"고차입{bd:.0f}%")
        elif bd > 30:
            pts -= 3; notes.append(f"차입{bd:.0f}%")
    return round(pts, 1), notes


def is_value_trap(discount: float | None, pbr: float | None, q: dict) -> bool:
    """싸다(NAV할인≥40% 또는 PBR≤0.5) + 본업부실(저ROE/적자이력/감익/고차입) = 함정."""
    if q.get("error"):
        return False
    cheap = ((discount is not None and discount >= 0.4)
             or (pbr is not None and 0 < pbr <= 0.5))
    roe = q.get("roe")
    op = q.get("op_grw")
    bd = q.get("borrow_dep")
    # 현재 지속 부실만 함정으로(과거 1회 적자이력은 제외): 저ROE / 회복 안 된 감익 / 과중 차입
    weak = ((roe is not None and roe < 5)
            or (op is not None and op < 0 and (roe is None or roe < 8))
            or (bd is not None and bd > 40))
    return bool(cheap and weak)


def summary(c: KisClient, codes: dict[str, str]) -> str:
    """지주사 바스켓 재무 질 표(ROE·부채·성장)."""
    lines = ["🏭 지주사 사업 질·재무 (연결, 최신 분기누적)",
             "  종목(코드)  ROE  부채비율  차입의존  매출↑  영익↑  플래그"]
    rows = []
    for code, name in codes.items():
        try:
            q = get_quality(c, code)
        except Exception as e:
            q = {"code": code, "error": str(e)}
        q["name"] = name
        rows.append(q)
    rows.sort(key=lambda r: (r.get("roe") is None, -(r.get("roe") or -99)))
    for q in rows:
        if q.get("error"):
            lines.append(f"  {q['name']}({q['code']}): 재무 조회불가")
            continue
        roe = q.get("roe"); dr = q.get("debt_ratio"); bd = q.get("borrow_dep")
        sg = q.get("sales_grw"); og = q.get("op_grw")
        _, notes = quality_points(q)
        flag = "·".join(notes) if notes else "—"
        lines.append(f"  {q['name']}({q['code']})  "
                     f"{roe if roe is not None else '—':>5}  "
                     f"{dr if dr is not None else '—':>6}  "
                     f"{bd if bd is not None else '—':>6}  "
                     f"{sg if sg is not None else '—':>6}  "
                     f"{og if og is not None else '—':>6}  {flag}")
    return "\n".join(lines)
