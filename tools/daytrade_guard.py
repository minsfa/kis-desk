"""단타 가드 — 하루짜리 눌림 매수·목표 매도·손절·장마감 청산 자동 집행 + 텔레그램 보고.
2026-09-17 설계 (9/18 첫 가동). 계획 파일을 매 루프 다시 읽으므로 실행 중에 무장(armed)·가격을 바꿀 수 있다.

흐름 (KST)
  08:00~08:50  NXT 프리마켓 감시 — 통합시세(UN)로 후보 갭 방향을 08:05/08:30/08:45 보고. 주문 없음.
  08:50        plan.armed == true 면 각 leg 지정가 매수 발주(KRX 정규장 대기 → 09:00 시가부터 체결 대기)
  09:00~15:15  20초마다: 체결 확인 → 체결분에 목표가 지정가 매도 등록
                         현재가 ≤ 손절가 → 목표 매도 취소 후 시장가 청산
                         목표 매도 체결 → 완료
  15:15        미체결 매수 취소, 남은 포지션 시장가 청산, 손익 요약 보고
안전
  - 실주문은 --live 에 더해 config/.env DRY_RUN=false 여야 나감. 없으면 dry-run 로그만.
  - 오늘 체결된 수량만 판다(기존 보유분 절대 건드리지 않음).
  - leg 예산 합이 plan.budget_total 을 넘으면 발주 거부. STOP 킬스위치 존중.
  - MAX_TOTAL_EXPOSURE 는 보유 6천만+ 상태에서 무의미하므로 이 프로세스에서만 0(무제한)으로 둔다.
파일
  계획  data/daytrade/plan_<YYYY-MM-DD>.json   {"armed": bool, "budget_total": int, "legs": [...], "watch": [...]}
  상태  data/daytrade/state_<YYYY-MM-DD>.json  (체결·주문번호·이벤트·heartbeat)
  로그  logs/daytrade_<YYYY-MM-DD>.log
실행
  python tools/daytrade_guard.py --plan data/daytrade/plan_2026-09-18.json --wait --live   # 08:00까지 대기 후 가동
  python tools/daytrade_guard.py --plan ... --once --dry                                    # 1회 점검(주문 없음)
  python tools/daytrade_guard.py --check                                                   # heartbeat 확인(크론용)
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("MAX_TOTAL_EXPOSURE", "0")  # dotenv 는 기존 env 를 덮어쓰지 않음 → 이 프로세스만 캡 해제
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트

from src.kis.config import load_settings, PROJECT_ROOT, KILL_SWITCH
from src.kis.client import KisClient
from src.kis import orders
from src.kis.tick import round_tick

KST = timezone(timedelta(hours=9))
DT_DIR = PROJECT_ROOT / "data" / "daytrade"
LOG_DIR = PROJECT_ROOT / "logs"
TELEGRAM_TO = "1185882216"
POLL_SEC = 20
PRE_REPORT_HM = {(8, 5), (8, 30), (8, 45)}


def now():
    return datetime.now(KST)


def hm(t):
    return t.hour * 60 + t.minute


def parse_hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


# ── 보고/기록 ────────────────────────────────────────────────────
class Guard:
    def __init__(self, plan_path, live, dry):
        self.plan_path = Path(plan_path)
        self.live = live and not dry
        self.plan = self.load_plan()
        self.day = self.plan["date"]
        self.state_path = DT_DIR / f"state_{self.day}.json"
        self.log_path = LOG_DIR / f"daytrade_{self.day}.log"
        self.state = self.load_state()
        self.c = KisClient(load_settings("prod"))
        self.pre_reported = set()

    def load_plan(self):
        return json.loads(self.plan_path.read_text())

    def load_state(self):
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except Exception:
                pass
        return {"legs": {}, "events": [], "orders_placed": False, "closed_out": False, "heartbeat": None}

    def save(self):
        self.state["heartbeat"] = now().isoformat()
        DT_DIR.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def log(self, msg, tg=False):
        line = f"{now():%H:%M:%S} {msg}"
        print(line, flush=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        self.state["events"].append(line)
        if tg:
            self.telegram(msg)

    def telegram(self, msg):
        tag = "" if self.live else "[DRY] "
        try:
            subprocess.run(["openclaw", "message", "send", "--channel", "telegram", "--target", TELEGRAM_TO,
                            "--message", f"{tag}🛡 단타가드 {self.day}\n{msg}"],
                           capture_output=True, text=True, timeout=30)
        except Exception as e:
            print(f"telegram fail: {e}", flush=True)

    # ── 시세 ─────────────────────────────────────────────────────
    def quote(self, code, mkt="J"):
        for _ in range(3):
            try:
                d = self.c.get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                               {"FID_COND_MRKT_DIV_CODE": mkt, "FID_INPUT_ISCD": code})
                o = d.get("output") or {}
                if o.get("stck_prpr"):
                    return {"px": float(o["stck_prpr"]), "chg": float(o.get("prdy_ctrt") or 0),
                            "hi": o.get("stck_hgpr"), "lo": o.get("stck_lwpr"), "vol": o.get("acml_vol")}
            except Exception:
                pass
            time.sleep(0.6)
        return None

    def leg_state(self, code):
        return self.state["legs"].setdefault(code, {
            "buy_order_no": None, "buy_org_no": None, "buy_qty": 0, "filled": 0, "avg": 0.0,
            "sell_order_no": None, "sell_org_no": None, "sell_qty_placed": 0, "sold": 0, "sold_amt": 0.0,
            "status": "idle"})

    # ── 프리마켓 보고 ─────────────────────────────────────────────
    def premarket_report(self, force=False):
        t = now()
        key = (t.hour, t.minute)
        if not force and (key not in PRE_REPORT_HM or key in self.pre_reported):
            return
        self.pre_reported.add(key)
        lines = [f"■ 프리마켓 {t:%H:%M} (통합시세)"]
        for leg in self.plan["legs"] + self.plan.get("watch", []):
            q = self.quote(leg["code"], "UN") or self.quote(leg["code"], "J")
            time.sleep(0.3)
            if not q:
                lines.append(f"{leg['name']} 조회실패")
                continue
            extra = ""
            if "buy" in leg:
                gap = (q["px"] / leg["buy"] - 1) * 100
                extra = f" | 매수가 {leg['buy']:,} 까지 {gap:+.1f}%"
            lines.append(f"{leg['name']} {q['px']:,.0f} ({q['chg']:+.1f}%){extra}")
        armed = self.plan.get("armed")
        lines.append("무장: ON — 08:50 발주" if armed else "무장: OFF — 08:50 발주 안 함 (plan.armed=true 로 바꾸면 발주)")
        self.log("\n".join(lines), tg=True)

    # ── 발주 ─────────────────────────────────────────────────────
    def place_buys(self):
        if self.state["orders_placed"]:
            return
        if not self.plan.get("armed"):
            self.log("08:50 — 무장 OFF, 발주 생략", tg=True)
            self.state["orders_placed"] = True  # 오늘은 더 이상 시도하지 않음
            return
        if KILL_SWITCH.exists():
            self.log("🚨 킬스위치(STOP) 존재 — 발주 중단", tg=True)
            self.state["orders_placed"] = True
            return
        total = sum(l["qty"] * l["buy"] for l in self.plan["legs"])
        cap = int(self.plan.get("budget_total", 0) or 0)
        if cap and total > cap:
            self.log(f"🚨 예산 초과 {total:,} > {cap:,} — 발주 거부", tg=True)
            self.state["orders_placed"] = True
            return
        lines = []
        for leg in self.plan["legs"]:
            st = self.leg_state(leg["code"])
            price = round_tick(leg["buy"])
            try:
                r = orders.buy(self.c, leg["code"], int(leg["qty"]), price=price, market=False, live=self.live)
            except Exception as e:
                lines.append(f"❌ {leg['name']} 발주 실패: {e}")
                st["status"] = "error"
                continue
            time.sleep(0.4)
            if r.get("dry_run"):
                st.update({"buy_order_no": f"DRY{leg['code']}", "buy_qty": int(leg["qty"]), "status": "pending"})
                lines.append(f"[dry] {leg['name']} {leg['qty']}주 @{price:,} 매수 (모의)")
            elif r.get("ok"):
                st.update({"buy_order_no": r["order_no"], "buy_org_no": r["org_no"], "buy_qty": int(leg["qty"]), "status": "pending"})
                lines.append(f"📝 {leg['name']} {leg['qty']}주 @{price:,} 지정가 매수 접수 (주문 {r['order_no']})")
            else:
                st["status"] = "error"
                lines.append(f"❌ {leg['name']} 거부: {r.get('msg')}")
        self.state["orders_placed"] = True
        self.log("\n".join(lines) or "발주 대상 없음", tg=True)

    # ── 체결 반영 ─────────────────────────────────────────────────
    def sync_fills(self):
        try:
            rows = orders.executions(self.c)
        except Exception as e:
            self.log(f"체결조회 실패: {e}")
            return
        by_no = {r.get("order_no"): r for r in rows if r.get("order_no")}
        for leg in self.plan["legs"]:
            st = self.leg_state(leg["code"])
            if st["buy_order_no"] and st["buy_order_no"] in by_no:
                r = by_no[st["buy_order_no"]]
                filled = int(float(r.get("ccld_qty") or 0))
                if filled > st["filled"]:
                    st["filled"] = filled
                    st["avg"] = float(r.get("avg_price") or leg["buy"])
                    st["status"] = "filled" if filled >= st["buy_qty"] else "partial"
                    self.log(f"✅ {leg['name']} {filled}/{st['buy_qty']}주 체결 평단 {st['avg']:,.0f}", tg=True)
            if st["sell_order_no"] and st["sell_order_no"] in by_no:
                r = by_no[st["sell_order_no"]]
                sold = int(float(r.get("ccld_qty") or 0))
                if sold > st["sold"]:
                    st["sold"] = sold
                    st["sold_amt"] = float(r.get("ccld_amt") or 0)
                    if sold >= st["filled"]:
                        st["status"] = "done"
                        pnl = st["sold_amt"] - st["avg"] * sold
                        self.log(f"🎯 {leg['name']} {sold}주 매도 체결 — 손익 {pnl:+,.0f}원", tg=True)

    def place_target(self, leg, st):
        """체결분 중 아직 매도주문 안 낸 수량에 목표가 지정가 매도."""
        qty = st["filled"] - st["sell_qty_placed"]
        if qty <= 0 or st["sell_order_no"]:
            return
        price = round_tick(leg["target"])
        try:
            r = orders.sell(self.c, leg["code"], qty, price=price, market=False, live=self.live)
        except Exception as e:
            self.log(f"❌ {leg['name']} 목표매도 실패: {e}", tg=True)
            return
        time.sleep(0.4)
        if r.get("dry_run") or r.get("ok"):
            st.update({"sell_order_no": r.get("order_no") or f"DRYS{leg['code']}", "sell_org_no": r.get("org_no"),
                       "sell_qty_placed": st["filled"], "status": "target_placed"})
            self.log(f"🎯 {leg['name']} {qty}주 @{price:,} 목표 매도 등록", tg=True)
        else:
            self.log(f"❌ {leg['name']} 목표매도 거부: {r.get('msg')}", tg=True)

    def cancel_sell(self, leg, st):
        if st["sell_order_no"] and not str(st["sell_order_no"]).startswith("DRY"):
            try:
                orders.cancel(self.c, st["sell_order_no"], st["sell_org_no"], qty=0, live=self.live)
                time.sleep(0.4)
            except Exception as e:
                self.log(f"매도 취소 실패 {leg['name']}: {e}")
        st["sell_order_no"], st["sell_org_no"], st["sell_qty_placed"] = None, None, st["sold"]

    def cancel_buy(self, leg, st):
        if st["buy_order_no"] and not str(st["buy_order_no"]).startswith("DRY") and st["filled"] < st["buy_qty"]:
            try:
                orders.cancel(self.c, st["buy_order_no"], st["buy_org_no"], qty=0, live=self.live)
                time.sleep(0.4)
                self.log(f"🚫 {leg['name']} 미체결 매수 {st['buy_qty'] - st['filled']}주 취소")
            except Exception as e:
                self.log(f"매수 취소 실패 {leg['name']}: {e}")

    def market_sell_rest(self, leg, st, why):
        qty = st["filled"] - st["sold"]
        if qty <= 0:
            return
        self.cancel_sell(leg, st)
        try:
            r = orders.sell(self.c, leg["code"], qty, price=0, market=True, live=self.live)
        except Exception as e:
            self.log(f"❌ {leg['name']} 시장가 청산 실패: {e}", tg=True)
            return
        time.sleep(0.4)
        if r.get("dry_run") or r.get("ok"):
            st.update({"sell_order_no": r.get("order_no") or f"DRYM{leg['code']}", "sell_org_no": r.get("org_no"),
                       "sell_qty_placed": st["filled"], "status": why})
            self.log(f"📉 {leg['name']} {qty}주 시장가 청산 ({why})", tg=True)
        else:
            self.log(f"❌ {leg['name']} 청산 거부: {r.get('msg')}", tg=True)

    def manage_positions(self):
        for leg in self.plan["legs"]:
            st = self.leg_state(leg["code"])
            if st["filled"] <= st["sold"]:
                continue
            if st["status"] in ("stopped", "closed"):
                continue
            q = self.quote(leg["code"])
            time.sleep(0.3)
            if not q:
                continue
            if q["px"] <= leg["stop"]:
                self.market_sell_rest(leg, st, "stopped")
                continue
            self.place_target(leg, st)

    def close_out(self):
        if self.state["closed_out"]:
            return
        self.sync_fills()
        for leg in self.plan["legs"]:
            st = self.leg_state(leg["code"])
            self.cancel_buy(leg, st)
            if st["filled"] > st["sold"]:
                self.market_sell_rest(leg, st, "closed")
        self.state["closed_out"] = True
        self.save()
        time.sleep(3)
        self.sync_fills()
        self.summary()

    def summary(self):
        lines = ["■ 마감 요약"]
        tot = 0.0
        for leg in self.plan["legs"]:
            st = self.leg_state(leg["code"])
            if st["filled"] == 0:
                lines.append(f"{leg['name']} 미체결 (매수가 {leg['buy']:,})")
                continue
            pnl = st["sold_amt"] - st["avg"] * st["sold"] if st["sold"] else 0.0
            tot += pnl
            lines.append(f"{leg['name']} 매수 {st['filled']}주 @{st['avg']:,.0f} → 매도 {st['sold']}주 손익 {pnl:+,.0f} [{st['status']}]")
        lines.append(f"합계 {tot:+,.0f}원 (수수료·세금 전)")
        self.log("\n".join(lines), tg=True)

    # ── 메인 루프 ────────────────────────────────────────────────
    def run(self, once=False, wait=False):
        start, order_at, close_at = parse_hm(self.plan.get("start", "08:00")), parse_hm(self.plan.get("order_at", "08:50")), parse_hm(self.plan.get("close_at", "15:15"))
        if wait:
            while hm(now()) < start or now().strftime("%Y-%m-%d") != self.day:
                if now().strftime("%Y-%m-%d") > self.day:
                    self.log("계획 날짜가 지났음 — 종료")
                    return
                time.sleep(30)
        self.log(f"가동 시작 (live={self.live}) legs={[l['name'] for l in self.plan['legs']]} 예산 {self.plan.get('budget_total', 0):,}", tg=True)
        if once:
            self.premarket_report(force=True)
            self.save()
            return
        while True:
            try:
                self.plan = self.load_plan()
                t = hm(now())
                if t < order_at:
                    self.premarket_report()
                elif not self.state["orders_placed"]:
                    self.place_buys()
                elif t < close_at:
                    if t >= 9 * 60:
                        self.sync_fills()
                        self.manage_positions()
                else:
                    self.close_out()
                    self.save()
                    self.log("가드 종료")
                    return
                self.save()
            except Exception as e:
                self.log(f"루프 오류: {e}")
            time.sleep(POLL_SEC)


def check_heartbeat():
    day = date.today().isoformat()
    p = DT_DIR / f"plan_{day}.json"
    if not p.exists():
        print("OK 오늘 단타 계획 없음")
        return
    sp = DT_DIR / f"state_{day}.json"
    if not sp.exists():
        print("🚨 단타 가드 상태 파일 없음 — 가드가 안 떠 있음")
        return
    st = json.loads(sp.read_text())
    hb = st.get("heartbeat")
    if st.get("closed_out"):
        print("OK 가드 마감 완료")
        return
    age = (now() - datetime.fromisoformat(hb)).total_seconds() if hb else 1e9
    print(f"OK 가드 정상 (heartbeat {age:.0f}s 전)" if age < 180 else f"🚨 단타 가드 heartbeat {age/60:.0f}분 전 — 프로세스 확인 필요")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", help="계획 JSON 경로 (기본: data/daytrade/plan_<오늘>.json)")
    ap.add_argument("--live", action="store_true", help="실주문 (.env DRY_RUN=false 도 필요)")
    ap.add_argument("--dry", action="store_true", help="주문 없이 로그만")
    ap.add_argument("--once", action="store_true", help="1회 점검 후 종료")
    ap.add_argument("--wait", action="store_true", help="계획 start 시각까지 대기")
    ap.add_argument("--check", action="store_true", help="heartbeat 확인(크론용)")
    a = ap.parse_args()
    if a.check:
        check_heartbeat()
        return
    plan = a.plan or str(DT_DIR / f"plan_{date.today().isoformat()}.json")
    Guard(plan, live=a.live, dry=a.dry).run(once=a.once, wait=a.wait)


if __name__ == "__main__":
    main()
