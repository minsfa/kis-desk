"""주가 로깅 테스트 — N개 종목을 간격 T로 폴링해 CSV 적재 + 처리량/레이트리밋/지연 측정.

읽기 전용(주문 없음). 시세 시계열 축적의 토대.
※ KisClient가 초당 호출한도(실전 20/s)에 맞춰 자동 throttle 하므로, 너무 타이트한 간격은
   클라이언트가 알아서 늦춘다(레이트리밋 회피). 이 테스트로 실제 달성 처리량을 확인한다.
출력: data/pricelog_YYYY-MM-DD.csv (ts,code,price)
"""
from __future__ import annotations
import csv
import time
from datetime import datetime, timezone, timedelta

from .client import KisClient
from . import market
from .config import PROJECT_ROOT

KST = timezone(timedelta(hours=9))
DATA_DIR = PROJECT_ROOT / "data"

PRESET = ["360750", "316140", "379800", "024110", "030200",
          "069500", "005930", "000660", "035420", "035720"]


def symbols_for(n: int) -> list[str]:
    return PRESET[:max(1, min(n, len(PRESET)))]


def run(c: KisClient, symbols: list[str], interval: float, rounds: int) -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"pricelog_{datetime.now(KST):%Y-%m-%d}.csv"
    new = not path.exists()
    f = open(path, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new:
        w.writerow(["ts", "code", "price"])

    n_ok = n_err = 0
    lat = []
    t_start = time.monotonic()
    for r in range(rounds):
        rt0 = time.monotonic()
        for code in symbols:
            c0 = time.monotonic()
            try:
                price = market.get_price(c, code).get("price")
                if price in (None, "", "0"):
                    n_err += 1
                else:
                    n_ok += 1
                    w.writerow([datetime.now(KST).isoformat(timespec="seconds"), code, price])
            except Exception:
                n_err += 1
            lat.append(time.monotonic() - c0)
        # 목표 간격 맞추기(남은 시간만 대기)
        rem = interval - (time.monotonic() - rt0)
        if rem > 0 and r < rounds - 1:
            time.sleep(rem)
    f.close()
    total_t = time.monotonic() - t_start
    q = n_ok + n_err
    return "\n".join([
        f"=== 주가 로깅 테스트 ===",
        f"종목 {len(symbols)}개 × {rounds}라운드, 목표간격 {interval}s",
        f"쿼리 {q}건 (성공 {n_ok} / 실패 {n_err})",
        f"총 소요 {total_t:.2f}s · 라운드당 평균 {total_t/rounds:.3f}s",
        f"쿼리 지연 avg {sum(lat)/len(lat)*1000:.0f}ms / max {max(lat)*1000:.0f}ms" if lat else "지연 n/a",
        f"실효 처리량 ≈ {q/total_t:.1f} 쿼리/s (실전 한도 20/s)",
        f"적재: {path.name} (총 {sum(1 for _ in open(path))-1}행 누적)",
    ])
