"""KRX OPEN API 헬퍼 — 유가증권(KOSPI) 일별 전종목 시세 조회 (읽기전용).

엔드포인트(승인됨):
  GET https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd
  헤더: AUTH_KEY: <KRX_AUTH_KEY>     (대문자 정확히)
  파라미터: basDd=YYYYMMDD          (쿼리스트링)
  응답: {"OutBlock_1":[ {종목...}, ... ]}  — 그 날짜 전 종목(KOSPI). 전부 문자열.

⚠️ 전일 데이터가 익일 08시 갱신. 당일/장중/주말/휴일은 OutBlock_1 빈 배열 반환.
   → graceful 처리(빈 dict 반환, 예외 던지지 않음). 죽지 말 것.

키는 config/.env 의 KRX_AUTH_KEY (fx_check.py 와 동일 로딩 패턴).

이 모듈은 src/ 밖(tools/)이라 라이브 매매 무결성 게이트와 무관.
단위: ACC_TRDVAL/MKTCAP 은 원, 종가는 원, 거래량/상장주식수는 주.
"""
from __future__ import annotations

import os
from pathlib import Path

import requests

KRX_STK_BYDD = "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd"

# 반도체 쏠림 계산용 (삼성전자 / SK하이닉스)
SAMSUNG = "005930"
HYNIX = "000660"


def _auth_key() -> str | None:
    """KRX_AUTH_KEY 를 환경변수 → config/.env 순으로 로드 (fx_check.py 패턴)."""
    k = os.getenv("KRX_AUTH_KEY")
    if k:
        return k.strip()
    env = Path(__file__).resolve().parent.parent / "config" / ".env"
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("KRX_AUTH_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def fetch_stk_bydd(bas_dd: str, key: str | None = None, timeout: int = 15) -> list[dict]:
    """basDd(YYYYMMDD) 하루치 유가증권(KOSPI) 전 종목 원본 행 리스트 반환.
    데이터 없음(주말/휴일/당일/미갱신)이면 빈 리스트. 키 없으면 RuntimeError."""
    if key is None:
        key = _auth_key()
    if not key:
        raise RuntimeError("KRX_AUTH_KEY 없음 (config/.env 확인)")
    r = requests.get(KRX_STK_BYDD, headers={"AUTH_KEY": key},
                     params={"basDd": bas_dd}, timeout=timeout)
    r.raise_for_status()
    return r.json().get("OutBlock_1") or []


def _i(v) -> int | None:
    """문자열 정수 → int, 실패/빈값 시 None."""
    try:
        s = str(v).replace(",", "").strip()
        return int(s) if s else None
    except (ValueError, TypeError):
        return None


def aggregate_kospi(rows: list[dict]) -> dict | None:
    """전 종목 원본 행 → KOSPI 집계 지표 dict. 빈 입력이면 None.

    반환(전부 원/주 단위, 비율은 %):
      kospi_mktcap : Σ MKTCAP   (MKT_NM=="KOSPI")        — 원
      kospi_value  : Σ ACC_TRDVAL (KOSPI)                — 원
      turnover     : kospi_value / kospi_mktcap × 100     — %
      semi_value   : 삼성전자+SK하이닉스 ACC_TRDVAL 합     — 원
      semi_val_share: semi_value / kospi_value × 100       — %
      sam_close/sam_value, hynix_close/hynix_value         — KIS 교차검증용
    """
    kospi = [r for r in rows if r.get("MKT_NM") == "KOSPI"]
    if not kospi:
        return None

    mktcap = sum(v for r in kospi if (v := _i(r.get("MKTCAP"))) is not None)
    value = sum(v for r in kospi if (v := _i(r.get("ACC_TRDVAL"))) is not None)

    by_code = {r.get("ISU_CD"): r for r in kospi}
    sam = by_code.get(SAMSUNG, {})
    hynix = by_code.get(HYNIX, {})
    sam_value = _i(sam.get("ACC_TRDVAL")) or 0
    hynix_value = _i(hynix.get("ACC_TRDVAL")) or 0
    semi_value = sam_value + hynix_value

    out = {
        "kospi_mktcap": mktcap or None,
        "kospi_value": value or None,
        "turnover": (value / mktcap * 100) if mktcap else None,
        "semi_value": semi_value or None,
        "semi_val_share": (semi_value / value * 100) if value else None,
        # 교차검증용 개별 종목 값
        "sam_close": _i(sam.get("TDD_CLSPRC")),
        "sam_value": sam_value or None,
        "hynix_close": _i(hynix.get("TDD_CLSPRC")),
        "hynix_value": hynix_value or None,
    }
    return out


def fetch_kospi_metrics(bas_dd: str, key: str | None = None) -> dict | None:
    """basDd 하루치 KOSPI 집계 지표 반환. 데이터 없으면 None (graceful)."""
    rows = fetch_stk_bydd(bas_dd, key=key)
    return aggregate_kospi(rows)
