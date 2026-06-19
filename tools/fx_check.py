"""달러원(USD/KRW) FX 트리거 체크 — FRED DEXKOUS.
크론용. 트리거선(1,450/1,400) 근접/하향 돌파 시에만 🚨 알림, 평소엔 'OK'(침묵).
src/ 밖(tools/)이라 라이브 매매 무결성 게이트와 무관."""
import os
import sys
from pathlib import Path

import requests

THRESHOLDS = [1450.0, 1400.0]   # 하향 돌파 감시선(원화 강세 전환)
WATCH = 1460.0                  # 이 이하면 보고(접근/돌파), 위면 침묵


def _fred_key() -> str | None:
    k = os.getenv("FRED_API_KEY")
    if k:
        return k
    env = Path(__file__).resolve().parent.parent / "config" / ".env"
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("FRED_API_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


def latest_usdkrw(key: str):
    r = requests.get("https://api.stlouisfed.org/fred/series/observations", params={
        "series_id": "DEXKOUS", "api_key": key, "sort_order": "desc",
        "limit": 7, "file_type": "json",
    }, timeout=10)
    r.raise_for_status()
    for o in r.json().get("observations", []):
        try:
            return float(o["value"]), o["date"]   # 결측("."")은 skip
        except (ValueError, TypeError):
            continue
    return None, None


def main():
    key = _fred_key()
    if not key:
        print("OK (FRED 키 없음 — 체크 불가)"); return
    try:
        rate, date = latest_usdkrw(key)
    except Exception as e:
        print(f"OK (환율 조회 실패: {e})"); return
    if rate is None:
        print("OK (환율 데이터 없음)"); return
    crossed = [t for t in THRESHOLDS if rate <= t]
    if rate <= WATCH:
        msg = f"🚨 달러원 {rate:,.1f}원 ({date})"
        if crossed:
            msg += (f" — {min(crossed):,.0f}원 선 하향 돌파! 원화 강세 전환 = "
                    "외국인 한국주식 유입 우호 신호(삼성·하이닉스 수급 점검 권장).")
        else:
            msg += " — 1,450 트리거 근접. 주시 필요."
        print(msg)
    else:
        print(f"OK 달러원 {rate:,.1f}원 ({date}) — 트리거(1,450) 미도달, 침묵")


if __name__ == "__main__":
    main()
