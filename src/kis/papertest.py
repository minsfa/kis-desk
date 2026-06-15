"""포워드 페이퍼테스트 — 눌림목 매수 전략을 실시간 폴링으로 종이 시뮬레이션(주문 X).

전략: 매일 시가 기준 -5%/-10% 지정가 매수를 가정. 장중 현재가가 그 한계 이하로 내려가면
'체결'로 간주(체결가=한계), 이후 +target% 반등 시 '매도', 못 오르면 종가(또는 종료시각)에 청산.
모든 체결/청산을 data/papertrades_YYYY-MM-DD.csv 에 기록. 실제 주문은 전혀 안 나감.
"""
from __future__ import annotations
import csv
import time
from datetime import datetime, timezone, timedelta

from .client import KisClient
from .config import TR_PRICE, PROJECT_ROOT

KST = timezone(timedelta(hours=9))
DATA_DIR = PROJECT_ROOT / "data"
PRICE_PATH = "/uapi/domestic-stock/v1/quotations/inquire-price"

# 변동성 큰 레버리지 ETF 중심(눌림목 -5% 트리거가 의미있게 발생)
WATCH = {"122630": "KODEX레버리지", "233740": "KODEX코스닥150레버리지"}


def _quote(c: KisClient, code: str):
    d = c.get(PRICE_PATH, TR_PRICE, {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    o = d.get("output", {}) or {}
    return float(o.get("stck_prpr") or 0), float(o.get("stck_oprc") or 0)


def run(c: KisClient, codes: dict[str, str], dips=(5, 10), target=1.0,
        until="15:20", poll=10) -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"papertrades_{datetime.now(KST):%Y-%m-%d}.csv"
    new = not path.exists()
    f = open(path, "a", newline="", encoding="utf-8")
    w = csv.writer(f)
    if new:
        w.writerow(["ts", "code", "name", "dip", "event", "ref_open", "fill", "exit", "pnl_pct", "reason"])

    ref = {}
    legs = {}
    for code in codes:
        cur, op = _quote(c, code)
        r = op or cur
        ref[code] = r
        for dip in dips:
            legs[(code, dip)] = {"st": "WAIT", "lim": round(r * (1 - dip / 100)), "fill": None}
    dipstr = "/".join(str(d) for d in dips)
    print(f"[페이퍼시작] {datetime.now(KST):%H:%M} · {list(codes.values())} · 시가기준 -{dipstr}% · 목표 +{target}% · ~{until}", flush=True)
    for code, name in codes.items():
        print(f"  {name}({code}) 시가{ref[code]:,.0f} → -5%={legs[(code,5)]['lim']:,} / -10%={legs[(code,10)]['lim']:,}", flush=True)

    th, tm = (int(x) for x in until.split(":"))
    last = {}
    while True:
        now = datetime.now(KST)
        if (now.hour, now.minute) >= (th, tm):
            break
        for code, name in codes.items():
            cur, _ = _quote(c, code)
            if cur <= 0:
                continue
            last[code] = cur
            for dip in dips:
                L = legs[(code, dip)]
                if L["st"] == "WAIT" and cur <= L["lim"]:
                    L["st"] = "HOLD"; L["fill"] = L["lim"]
                    w.writerow([now.isoformat(timespec="seconds"), code, name, dip, "FILL",
                                ref[code], L["fill"], "", "", "체결(저점터치)"]); f.flush()
                    print(f"[{now:%H:%M:%S}] FILL {name} -{dip}% @ {L['fill']:,}", flush=True)
                elif L["st"] == "HOLD" and cur >= L["fill"] * (1 + target / 100):
                    pnl = (cur / L["fill"] - 1) * 100
                    L["st"] = "DONE"
                    w.writerow([now.isoformat(timespec="seconds"), code, name, dip, "SELL",
                                ref[code], L["fill"], round(cur), round(pnl, 2), f"+{target}%반등"]); f.flush()
                    print(f"[{now:%H:%M:%S}] SELL {name} -{dip}% @ {cur:,.0f} (+{pnl:.2f}%)", flush=True)
        time.sleep(poll)

    # 종료시각: 남은 보유분 마지막가로 청산
    for (code, dip), L in legs.items():
        if L["st"] == "HOLD":
            cur = last.get(code, L["fill"])
            pnl = (cur / L["fill"] - 1) * 100
            w.writerow([datetime.now(KST).isoformat(timespec="seconds"), code, codes[code], dip,
                        "SELL", ref[code], L["fill"], round(cur), round(pnl, 2), "종료청산"])
            print(f"[종료청산] {codes[code]} -{dip}% @ {cur:,.0f} ({pnl:+.2f}%)", flush=True)
    f.close()
    n_fill = sum(1 for L in legs.values() if L["st"] != "WAIT")
    return f"[페이퍼종료] {datetime.now(KST):%H:%M} · 체결레그 {n_fill} · 로그 {path.name}"
