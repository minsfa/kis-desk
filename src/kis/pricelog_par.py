"""병렬 폴링 레이트리밋 테스트 — N개 종목을 '동시에' 조회해 초당 처리 상한 탐색.

KisClient의 직렬 throttle을 우회하고, 매 라운드 N개 요청을 ThreadPool로 동시 발사.
초당 한도(실전 20/s) 초과 시 KIS가 EGW00201('초당 거래건수 초과')로 일부 거부 → 그 지점 탐색.
읽기 전용(주문 없음). 성공분은 시세 CSV에도 적재.
"""
from __future__ import annotations
import csv
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import requests

from .client import KisClient
from .config import TR_PRICE, PROJECT_ROOT

KST = timezone(timedelta(hours=9))
DATA_DIR = PROJECT_ROOT / "data"
PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"

# 유동성 높은 국내 종목 풀 (30+개)
POOL = [
    "005930", "000660", "373220", "207940", "005380", "000270", "068270", "035420",
    "035720", "005490", "051910", "006400", "028260", "105560", "055550", "086790",
    "316140", "024110", "030200", "017670", "015760", "034730", "003550", "012330",
    "011200", "009150", "032830", "018260", "010130", "360750", "069500", "379800",
]


def _fetch(host, headers, code):
    t0 = time.monotonic()
    try:
        r = requests.get(f"{host}{PRICE_PATH}", headers=headers,
                         params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
                         timeout=10)
        d = r.json()
        rt = str(d.get("rt_cd", "1"))
        msg = (d.get("msg1") or "")
        price = (d.get("output") or {}).get("stck_prpr")
        rl = ("초당" in msg) or (str(d.get("msg_cd", "")) == "EGW00201")
        return {"code": code, "ok": rt == "0" and bool(price), "rl": rl,
                "msg": msg, "price": price, "lat": time.monotonic() - t0}
    except Exception as e:
        return {"code": code, "ok": False, "rl": False, "err": str(e),
                "lat": time.monotonic() - t0}


def collect(c: KisClient, n: int, until_hm: str, heartbeat: int = 60) -> str:
    """N종목을 초당 1회 동시 폴링해 until_hm(HH:MM, KST)까지 CSV에 연속 누적."""
    headers = c._headers(TR_PRICE)
    host = c.s.host
    syms = POOL[:max(1, min(n, len(POOL)))]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"pricelog_{datetime.now(KST):%Y-%m-%d}.csv"
    new = not path.exists()
    fcsv = open(path, "a", newline="", encoding="utf-8")
    w = csv.writer(fcsv)
    if new:
        w.writerow(["ts", "code", "price"])
    th, tm = (int(x) for x in until_hm.split(":"))
    print(f"[수집시작] {len(syms)}종목 · 초당1회 · ~{until_hm}까지 · {path.name}", flush=True)
    ok = rl = err = rounds = 0
    last_hb = time.monotonic()
    while True:
        now = datetime.now(KST)
        if (now.hour, now.minute) >= (th, tm):
            break
        r0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=len(syms)) as ex:
            res = list(ex.map(lambda cd: _fetch(host, headers, cd), syms))
        for x in res:
            if x.get("ok"):
                ok += 1
                w.writerow([now.isoformat(timespec="seconds"), x["code"], x["price"]])
            elif x.get("rl"):
                rl += 1
            else:
                err += 1
        rounds += 1
        fcsv.flush()
        if time.monotonic() - last_hb >= heartbeat:
            print(f"[{now:%H:%M:%S}] rounds={rounds} ok={ok} rl={rl} err={err} "
                  f"(누적 {sum(1 for _ in open(path))-1}행)", flush=True)
            last_hb = time.monotonic()
        time.sleep(max(0, 1.0 - (time.monotonic() - r0)))
    fcsv.close()
    total = sum(1 for _ in open(path)) - 1
    return (f"[수집종료] {datetime.now(KST):%H:%M:%S} · rounds={rounds} · "
            f"성공 {ok} · 레이트리밋 {rl} · 에러 {err} · CSV 누적 {total}행")


def run(c: KisClient, counts: list[int], rounds: int) -> str:
    headers = c._headers(TR_PRICE)  # 토큰/appkey 포함
    host = c.s.host
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"pricelog_{datetime.now(KST):%Y-%m-%d}.csv"
    new = not path.exists()
    fcsv = open(path, "a", newline="", encoding="utf-8")
    w = csv.writer(fcsv)
    if new:
        w.writerow(["ts", "code", "price"])

    L = ["=== 병렬 폴링 레이트리밋 램프 테스트 (실전 한도 20/s) ===",
         f"라운드/단계 {rounds}회, 풀 {len(POOL)}종목"]
    for n in counts:
        if n > len(POOL):
            L.append(f"[{n}종목] 풀 부족 — 스킵"); continue
        syms = POOL[:n]
        ok = rl = err = 0
        lats = []
        worst_round_s = 0.0
        for _ in range(rounds):
            r0 = time.monotonic()
            with ThreadPoolExecutor(max_workers=n) as ex:
                res = list(ex.map(lambda cd: _fetch(host, headers, cd), syms))
            for x in res:
                lats.append(x["lat"])
                if x.get("ok"):
                    ok += 1
                    w.writerow([datetime.now(KST).isoformat(timespec="seconds"),
                                x["code"], x["price"]])
                elif x.get("rl"):
                    rl += 1
                else:
                    err += 1
            worst_round_s = max(worst_round_s, time.monotonic() - r0)
            time.sleep(max(0, 1.0 - (time.monotonic() - r0)))  # 초당 1라운드
        tag = "✅한도내" if rl == 0 else f"⚠️레이트리밋 {rl}건"
        L.append(f"[{n:>2}종목] 성공 {ok}/{n*rounds} · 레이트리밋 {rl} · 기타에러 {err} · "
                 f"라운드최대 {worst_round_s*1000:.0f}ms · {tag}")
    fcsv.close()
    L.append(f"적재: {path.name} (누적 {sum(1 for _ in open(path))-1}행)")
    return "\n".join(L)
