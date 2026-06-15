"""동시호가/프리마켓 실시간 모니터 — 예상체결가 + 호가 잔량 불균형을 N초 간격으로 캡처.

8:30~09:00 KRX 장전 동시호가(레버리지 ETF) 또는 08:00~08:50 NXT 프리마켓(단일종목)에서
예상체결가(antc_cnpr)·예상체결대비·총매수/매도잔량을 폴링해 CSV 적재 + 콘솔 중계.
investor 라벨(외국인/기관)은 동시호가 중 익명이라 불가 — 잔량 불균형이 수급압력 프록시.
사용: python -m src.cli auctionmon <code> [--mkt J|UN] [--sec 10] [--until 09:00]
"""
from __future__ import annotations
import csv
import time
from datetime import datetime, timezone, timedelta

from .client import KisClient
from .config import PROJECT_ROOT

KST = timezone(timedelta(hours=9))
HOGA = "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn"
DATA_DIR = PROJECT_ROOT / "data"


def snapshot(c: KisClient, code: str, mkt: str = "J") -> dict:
    d = c.get(HOGA, "FHKST01010200", {"FID_COND_MRKT_DIV_CODE": mkt, "FID_INPUT_ISCD": code})
    o = d.get("output1", {}) or {}
    ask = int(o.get("total_askp_rsqn") or 0)   # 총 매도잔량
    bid = int(o.get("total_bidp_rsqn") or 0)   # 총 매수잔량
    imb = (bid - ask) / (bid + ask) * 100 if (bid + ask) else 0.0  # +면 매수우위
    return {
        "t": datetime.now(KST).strftime("%H:%M:%S"),
        "antc": o.get("antc_cnpr"),            # 예상체결가
        "antc_vrss": o.get("antc_cntg_vrss"),  # 예상체결 대비
        "antc_qty": o.get("antc_cnqn"),        # 예상체결량
        "bid_rem": bid, "ask_rem": ask,
        "imb_pct": round(imb, 1),              # 잔량 불균형(매수우위 %)
        "cur": o.get("stck_prpr"),             # 현재가(거래 중일 때)
    }


def run(c: KisClient, code: str, mkt: str = "J", sec: int = 10, until: str = "09:00") -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"auction_{code}_{datetime.now(KST):%Y-%m-%d}.csv"
    new = not path.exists()
    f = open(path, "a", newline="", encoding="utf-8"); w = csv.writer(f)
    cols = ["t", "antc", "antc_vrss", "antc_qty", "bid_rem", "ask_rem", "imb_pct", "cur"]
    if new:
        w.writerow(cols)
    th, tm = (int(x) for x in until.split(":"))
    print(f"[동시호가 모니터] {code} ({mkt}) {sec}초 간격 ~{until}  | antc=예상체결가 imb=매수우위%", flush=True)
    last_antc = None
    while True:
        now = datetime.now(KST)
        if (now.hour, now.minute) >= (th, tm):
            break
        try:
            s = snapshot(c, code, mkt)
        except Exception as e:
            print(f"  {datetime.now(KST):%H:%M:%S} err {str(e)[:40]}", flush=True)
            time.sleep(sec); continue
        w.writerow([s[k] for k in cols]); f.flush()
        arrow = ""
        if last_antc and s["antc"] and s["antc"] != last_antc:
            try:
                arrow = " ↑" if float(s["antc"]) > float(last_antc) else " ↓"
            except Exception:
                pass
        last_antc = s["antc"]
        print(f"  {s['t']} 예상 {s['antc']}({s['antc_vrss']}){arrow}  매수잔량 {s['bid_rem']:,} / 매도 {s['ask_rem']:,}  불균형 {s['imb_pct']:+}%", flush=True)
        time.sleep(sec)
    f.close()
    return f"[동시호가 모니터 종료] 적재: {path}"
