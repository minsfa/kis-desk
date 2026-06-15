"""펀더멘털 스냅샷 — 지주사 저평가 실측(Phase A).

KIS inquire-price(FHKST01010100) 응답에는 현재가뿐 아니라
PBR/PER/EPS/시가총액/상장주식수/52주·250일 고저가 다 들어있는데
market.get_price 는 가격만 뽑고 나머지를 버린다. 여기서 그걸 살려
지주사 유니버스의 '실측 PBR·시총' 테이블을 만든다.

용도: LLM 리포트가 주장하는 "PBR 0.1~0.4배" 같은 수치를 우리 손으로 재검증.
모든 값은 KIS가 주는 그대로(연결 자기자본 기준 PBR). NAV(보유 지분가치) 할인율은
Phase B(nav.py)에서 별도 산출 — 여기선 단일 API로 나오는 것만 다룬다.
"""
from __future__ import annotations
import csv
from datetime import datetime, timedelta, timezone

from .client import KisClient
from .config import TR_PRICE, PROJECT_ROOT
from .logging_util import log_event

KST = timezone(timedelta(hours=9))
HOLDCO_DIR = PROJECT_ROOT / "data" / "holdco"

# 지주사·지주성격 유니버스. 출처: 사용자 리포트(워치리스트 후보) + 대표 지주사 보강.
# ※ 종목코드/명은 검증된 표기. PBR 등 수치는 전부 실행 시점 KIS 실측으로 채운다.
HOLDCO_BASKET = {
    # --- 리포트 거명: 극저평가 / 시총<지분가치 ---
    "058650": "세아홀딩스",
    "004990": "롯데지주",
    "004800": "효성",
    "001040": "CJ",
    "402340": "SK스퀘어",
    "034730": "SK",
    "028260": "삼성물산",
    # --- 리포트 거명: 중대형 우량 ---
    "000880": "한화",
    "003550": "LG",
    "006260": "LS",
    "000150": "두산",
    "267250": "HD현대",
    # --- 리포트 거명: 중소형 + 자체사업 ---
    "003380": "하림지주",
    "010780": "아이에스동서",
    "054800": "아이디스홀딩스",
    "001430": "세아베스틸지주",
    "002020": "코오롱",
    "000210": "DL",
    "060980": "HL홀딩스",
    "034310": "NICE",
    # --- 보강: 대표 지주사(리포트 누락분, 비교군) ---
    "078930": "GS",
    "383800": "LX홀딩스",
    "000240": "한국앤컴퍼니",
    "180640": "한진칼",
    "072710": "농심홀딩스",
    "001630": "종근당홀딩스",
    "009970": "영원무역홀딩스",
    "036570": "엔씨소프트",  # (대조용 비지주 — 필요시 제거) → 일단 제외 대상
}
# 대조용으로 잘못 들어가기 쉬운 비지주 종목은 즉시 제거(명시적으로).
HOLDCO_BASKET.pop("036570", None)


def wide_basket() -> dict[str, str]:
    """확장 유니버스 = 큐레이션 27 ∪ DART 자동발굴('홀딩스/지주' 상장사 ~100+).

    금융지주·외국계 등 NAV 산출 불가 종목도 포함되나 cov 0%/n-a 로 자연 탈락한다.
    """
    from . import dart
    basket = dict(HOLDCO_BASKET)
    try:
        for code, name in dart.list_holding_companies().items():
            basket.setdefault(code, name)
    except Exception:
        pass
    return basket


def _f(v) -> float | None:
    """KIS 문자열 숫자 → float (빈값/0/하이픈 안전)."""
    try:
        x = float(v)
        return x
    except (TypeError, ValueError):
        return None


def get_fundamentals(c: KisClient, code: str) -> dict:
    """단일 종목 펀더멘털. inquire-price 응답 풀파싱.

    반환: price/pbr/per/eps/bps/시총(억원)/상장주식수/52주·250일 위치 등.
    bps 는 응답에 없으면 price/pbr 로 역산.
    """
    path = "/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}
    d = c.get(path, TR_PRICE, params)
    o = d.get("output", {}) or {}

    price = _f(o.get("stck_prpr"))
    pbr = _f(o.get("pbr"))
    per = _f(o.get("per"))
    eps = _f(o.get("eps"))
    bps = _f(o.get("bps"))
    if bps is None and price and pbr:
        bps = round(price / pbr)  # PBR=주가/BPS → BPS=주가/PBR

    mktcap_eok = _f(o.get("hts_avls"))   # 시가총액(억원)
    shares = _f(o.get("lstn_stcn"))      # 상장주식수
    w52_hi, w52_lo = _f(o.get("w52_hgpr")), _f(o.get("w52_lwpr"))
    # 52주 밴드 내 위치(0=바닥,1=천장) — '낙폭과대'와 교차검증용
    pos_52w = None
    if price and w52_hi and w52_lo and w52_hi > w52_lo:
        pos_52w = round((price - w52_lo) / (w52_hi - w52_lo), 3)

    res = {
        "code": code,
        "price": price,
        "change_pct": _f(o.get("prdy_ctrt")),
        "pbr": pbr,
        "per": per,
        "eps": eps,
        "bps": bps,
        "mktcap_eok": mktcap_eok,        # 억원
        "shares": shares,
        "w52_high": w52_hi,
        "w52_low": w52_lo,
        "pos_52w": pos_52w,
        "settle_month": o.get("stac_month"),  # 결산월
    }
    log_event("fundamentals", code=code, pbr=pbr, per=per, mktcap_eok=mktcap_eok)
    return res


def snapshot(c: KisClient, basket: dict[str, str] | None = None) -> dict:
    """유니버스 전체 펀더멘털을 받아 PBR 오름차순으로 정렬 + CSV 저장.

    반환: {"date","rows":[...],"csv": path, "errors":[...]}
    rows 각 항목에 name 병합.
    """
    basket = basket or HOLDCO_BASKET
    rows, errors = [], []
    for code, name in basket.items():
        try:
            f = get_fundamentals(c, code)
            f["name"] = name
            rows.append(f)
        except Exception as e:  # 한 종목 실패가 전체를 막지 않게
            errors.append({"code": code, "name": name, "error": str(e)})

    # PBR 오름차순(None은 뒤로). 0 이하(데이터 이상)도 뒤로.
    def _pbr_key(r):
        p = r.get("pbr")
        return (p is None or p <= 0, p if (p and p > 0) else float("inf"))
    rows.sort(key=_pbr_key)

    day = f"{datetime.now(KST):%Y-%m-%d}"
    HOLDCO_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = HOLDCO_DIR / f"fundamentals_{day}.csv"
    cols = ["code", "name", "price", "change_pct", "pbr", "per", "eps", "bps",
            "mktcap_eok", "shares", "w52_high", "w52_low", "pos_52w", "settle_month"]
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in cols})

    return {"date": day, "rows": rows, "csv": str(csv_path), "errors": errors}


def _eok_to_human(eok: float | None) -> str:
    """억원 → 사람이 읽는 단위(조/억)."""
    if not eok:
        return "-"
    if eok >= 10000:
        return f"{eok/10000:.1f}조"
    return f"{eok:,.0f}억"


def summary(c: KisClient, basket: dict[str, str] | None = None,
            low_pbr: float = 0.5) -> str:
    """텔레그램/openclaw 보고용 텍스트. PBR 오름차순 표 + 저PBR 강조."""
    snap = snapshot(c, basket)
    rows = snap["rows"]
    day = snap["date"]
    lines = [f"📊 지주사 펀더멘털 실측 — {day} (PBR 오름차순, KIS inquire-price)"]
    lines.append("  순위 종목(코드)  PBR  PER  시총  52주위치")
    valid = [r for r in rows if r.get("pbr") and r["pbr"] > 0]
    for i, r in enumerate(rows, 1):
        pbr = r.get("pbr")
        pbr_s = f"{pbr:.2f}" if pbr and pbr > 0 else "—"
        per = r.get("per")
        per_s = f"{per:.1f}" if per and per > 0 else "—"
        cap = _eok_to_human(r.get("mktcap_eok"))
        pos = r.get("pos_52w")
        pos_s = f"{pos*100:.0f}%" if pos is not None else "—"
        flag = " 🔻" if (pbr and 0 < pbr <= low_pbr) else ""
        lines.append(f"  {i:>2}. {r['name']}({r['code']})  PBR {pbr_s}  PER {per_s}  "
                     f"시총 {cap}  52w {pos_s}{flag}")
    n_low = sum(1 for r in valid if r["pbr"] <= low_pbr)
    lines.append(f"\n🔻 PBR≤{low_pbr}: {n_low}/{len(valid)}종목. "
                 f"(NAV 할인율은 Phase B nav.py에서 별도 산출)")
    if snap["errors"]:
        errs = ", ".join(f"{e['name']}({e['code']})" for e in snap["errors"])
        lines.append(f"⚠️ 조회 실패: {errs}")
    lines.append(f"💾 {snap['csv']}")
    return "\n".join(lines)
