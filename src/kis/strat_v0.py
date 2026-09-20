"""전략 v0 — 보수적 눌림목 브래킷(매수+목표매도 동시). 페이퍼 또는 샘플 실거래.

진입 2종:
  C1 (눌림+매수벽): 대장주/저가ETF가 -dip% 도달 AND 호가 매수벽(통합 잔량비>=wall)일 때.
  C2 (전날급등→익일눌림): 전날 +surge%↑ 급등 종목 아침 스캔 → -dip% 보수 진입.
브래킷: 매수 지정가 체결되면 '즉시' 목표가(+target%) 매도 지정가를 건다(걸어놓고 기다림).
종목당 budget(원) 한도, 못 사면 스킵, 종목당 1회, 마감 전 미체결주문 취소+자기체결분만 청산
(계좌 기존 보유분은 절대 청산하지 않음 — 잔고 전체를 읽어 파는 짓 금지).
통합(UN)·SOR → NXT 프리마켓(08:00~) 포함. 잔고기반 체결확인. STOP파일=즉시중단.
실주문 발동 3중 조건: live 플래그 AND .env DRY_RUN=false AND 그날 사람이 'stratv0arm'으로 비번 무장.
무장 없으면(크론이 돌아도) PAPER로 동작 = 감시만, 실주문 0. 무장은 당일 자동만료(어제 무장 무효).
"""
from __future__ import annotations
import csv
import time
from datetime import datetime, timezone, timedelta

from .client import KisClient
from .config import TR_PRICE, PROJECT_ROOT, KILL_SWITCH
from . import market, orders
from .daily import VALUE_BASKET
from .tick import round_tick

KST = timezone(timedelta(hours=9))
DATA_DIR = PROJECT_ROOT / "data"
ARM_FILE = DATA_DIR / "state" / "stratv0_arm"   # 당일 라이브 '무장' 토큰(내용=오늘 날짜). 사람이 비번으로만 생성.
CHART = "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice"
MKT = "UN"


def _today_kst() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def _arm_ok() -> bool:
    """오늘 날짜로 '무장'됐는지. 토큰 내용이 오늘 날짜와 일치해야 True(어제 무장은 자동만료)."""
    try:
        return ARM_FILE.read_text(encoding="utf-8").strip() == _today_kst()
    except Exception:
        return False


def arm(pin: str) -> bool:
    """비번이 config/.env STRATV0_ARM_PIN 과 일치하면 오늘자 무장 토큰을 쓴다. 일치/생성=True."""
    import os
    expected = os.getenv("STRATV0_ARM_PIN", "")
    if not expected:
        raise ValueError("STRATV0_ARM_PIN 미설정 — config/.env 에 비번을 먼저 등록하세요")
    if str(pin).strip() != expected:
        return False
    ARM_FILE.parent.mkdir(parents=True, exist_ok=True)
    ARM_FILE.write_text(_today_kst(), encoding="utf-8")
    return True


def disarm() -> None:
    try:
        ARM_FILE.unlink()
    except FileNotFoundError:
        pass

C1_LEADERS = {
    "005930": "삼성전자", "000660": "SK하이닉스", "122630": "KODEX레버리지", "091160": "KODEX반도체",
    "233740": "코스닥150레버리지", "379800": "KODEX미국S&P500", "360750": "TIGER미국S&P500",
}
# ETF/레버리지(NXT 거래 불가 → 정규장에서만). 일반종목은 NXT 프리/애프터도 가능.
ETF_CODES = {"122630", "091160", "233740", "379800", "360750", "069500", "229200"}


def _session(now):
    hm = now.hour * 60 + now.minute
    if 8 * 60 <= hm < 9 * 60:        return "pre"     # NXT 프리(일반종목만)
    if 9 * 60 <= hm < 15 * 60 + 30:  return "reg"     # KRX 정규(전종목)
    if 16 * 60 <= hm < 20 * 60:      return "after"   # NXT 애프터(일반종목만)
    return "closed"


def _prev_surge_pct(c, code):
    d = c.get(CHART, "FHKST03010100", {
        "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": (datetime.now(KST) - timedelta(days=12)).strftime("%Y%m%d"),
        "FID_INPUT_DATE_2": datetime.now(KST).strftime("%Y%m%d"),
        "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
    out = d.get("output2") or []
    try:
        return (float(out[1]["stck_clpr"]) / float(out[2]["stck_clpr"]) - 1) * 100
    except Exception:
        return 0.0


def _price(c, code):
    d = c.get("/uapi/domestic-stock/v1/quotations/inquire-price", TR_PRICE,
              {"FID_COND_MRKT_DIV_CODE": MKT, "FID_INPUT_ISCD": code})
    o = d.get("output", {}) or {}
    return float(o.get("stck_prpr") or 0), float(o.get("stck_oprc") or 0)


def _nxt_ok(c, code) -> bool:
    """NXT 상장(=프리/애프터 거래 가능) 여부. NX 시세 현재가>0 이면 거래 가능."""
    try:
        d = c.get("/uapi/domestic-stock/v1/quotations/inquire-price", TR_PRICE,
                  {"FID_COND_MRKT_DIV_CODE": "NX", "FID_INPUT_ISCD": code})
        return float((d.get("output", {}) or {}).get("stck_prpr") or 0) > 0
    except Exception:
        return False


def _qty(price, budget):
    return int(budget // price) if 0 < price <= budget else 0


def run(c, budget=None, dip=None, target=None, wall=None, surge=None,
        until="15:20", poll=15, live=False) -> str:
    from . import stratcfg
    cfg = stratcfg.load()                       # 설정파일 = 기본값(인자 명시시 인자 우선)
    budget = cfg["budget"] if budget is None else budget
    dip = cfg["dip"] if dip is None else dip
    target = cfg["target"] if target is None else target
    wall = cfg["wall"] if wall is None else wall
    surge = cfg["surge"] if surge is None else surge
    excl = set(cfg.get("exclude", []))
    c1 = {**C1_LEADERS, **cfg.get("c1_extra", {})}   # 추가워치 병합
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"stratv0_{datetime.now(KST):%Y-%m-%d}.csv"
    new = not path.exists()
    f = open(path, "a", newline="", encoding="utf-8"); w = csv.writer(f)
    if new:
        w.writerow(["ts", "mode", "strat", "code", "name", "event", "qty", "buy", "target", "note"])
    armed = bool(live) and (not c.s.dry_run)
    if armed and not _arm_ok():          # 라이브라도 사람이 비번으로 '무장'하지 않으면 실주문 금지(감시만)
        armed = False
        print("[v0] ⚠️ 미무장 — 오늘 라이브 주문 안 함(감시만). "
              "무장: ./.venv/bin/python -m src.cli stratv0arm (비번 입력)", flush=True)
    mode = "LIVE" if armed else "PAPER"

    # 승인 게이트: live면 항상 ON, paper도 approved.json 있으면 ON. 게이트 ON이면 승인 종목만 장전.
    from . import approve
    today = datetime.now(KST).strftime("%Y-%m-%d")
    approved = approve.load_for(today)
    gate = bool(live) or approve.PATH.exists()

    legs, skipped, c2hits, seq = {}, [], [], {}

    def addleg(strat, code, name, lim, qty, nxt, ftgt=None):
        i = seq.get(code, 0); seq[code] = i + 1   # 같은 종목 여러 트랑쉐 → 고유키
        L = {"strat": strat, "code": code, "st": "WAIT", "name": name,
             "lim": round_tick(lim, up=False), "qty": int(qty), "nxt": nxt}  # 매수=호가단위 내림
        if ftgt:
            L["ftgt"] = round_tick(ftgt, up=True)                            # 목표=호가단위 올림
        legs[f"{strat}:{code}:{i}"] = L

    if gate:                                    # 제안→승인→장전: 승인된 종목만
        for code, info in approved.items():
            if not isinstance(info, dict):
                info = {"name": info}
            name = info.get("name") or code
            if code in excl:
                continue
            cur, op = _price(c, code); r = op or cur
            nxt = (code not in ETF_CODES) and _nxt_ok(c, code)
            tranches = info.get("legs") or ([{"price": info["price"], "target": info.get("target")}]
                                            if info.get("price") else [])
            if tranches:                        # 지정단가/분할매수
                for lg in tranches:
                    price = int(lg["price"]); q = int(lg.get("qty") or _qty(price, budget))
                    if q >= 1:
                        addleg("AP", code, name, price, q, nxt, lg.get("target"))
                    else:
                        skipped.append(name)
            else:                               # 시가-dip% 단일
                q = _qty(r, budget)
                if q >= 1:
                    addleg("AP", code, name, round(r * (1 - dip / 100)), q, nxt)
                else:
                    skipped.append(name)
    else:                                       # 게이트 없음(페이퍼 자동스캔): C2 전날급등 + C1 대장
        for code, name in VALUE_BASKET.items():
            if code in excl:
                continue
            try:
                sp = _prev_surge_pct(c, code)
            except Exception:
                sp = 0.0
            if sp >= surge:
                cur, op = _price(c, code); r = op or cur; q = _qty(r, budget)
                if q >= 1:
                    addleg("C2", code, f"{name}(+{sp:.0f}%)", round(r * (1 - dip / 100)), q, _nxt_ok(c, code))
                    c2hits.append(f"{name}+{sp:.0f}%x{q}")
                else:
                    skipped.append(name)
        for code, name in c1.items():
            if code in excl:
                continue
            cur, op = _price(c, code); r = op or cur; q = _qty(r, budget)
            if q >= 1:
                addleg("C1", code, name, round(r * (1 - dip / 100)), q, (code not in ETF_CODES) and _nxt_ok(c, code))
            else:
                skipped.append(name)

    gtxt = f"GATE 승인{len(approved)}종목" if gate else "자동스캔(게이트off)"
    print(f"[v0 {mode}] {datetime.now(KST):%H:%M} {gtxt} budget{budget:,} dip-{dip}% 목표+{target}% 벽>={wall} ~{until}", flush=True)
    print(f"  감시: {[(L['strat'], L['name'], L['qty'], L['lim'], 'NXT가능' if L.get('nxt') else '정규장만') for L in legs.values()] or '없음'}", flush=True)
    if not gate:
        print(f"  C2급등: {c2hits or '없음'} / 스킵(예산초과): {skipped}", flush=True)

    def log(s, code, L, ev, note=""):
        w.writerow([datetime.now(KST).isoformat(timespec="seconds"), mode, s, code, L["name"],
                    ev, L.get("qty"), L.get("lim"), L.get("tgt"), note]); f.flush()

    def fills():
        """주문번호별 체결수량 — 트랑쉐별 체결을 주문번호로 추적(잔고 아님). 실패시 {}."""
        try:
            return {o["order_no"]: int(float(o.get("ccld_qty") or 0)) for o in orders.today_orders(c)}
        except Exception:
            return {}

    th, tm = (int(x) for x in until.split(":"))
    last = {}
    while True:
        now = datetime.now(KST)
        if (now.hour, now.minute) >= (th, tm):
            break
        if KILL_SWITCH.exists():
            print("[KILL] STOP 감지 — 중단", flush=True); break
        sess = _session(now)
        exch = "SOR" if sess in ("pre", "after") else None  # 정규장=KRX(None)
        fl = fills() if armed else {}
        for L in legs.values():
            code, s = L["code"], L["strat"]
            try:
                cur, _ = _price(c, code)
            except Exception:
                continue
            if cur <= 0:
                continue
            last[code] = cur
            L["tgt"] = L.get("ftgt") or round_tick(L["lim"] * (1 + target / 100), up=True)
            st = L["st"]
            if st == "WAIT" and cur <= L["lim"]:
                if s in ("C1", "AP"):
                    try:
                        if (market.get_orderbook(c, code, mkt=MKT).get("bid_ask_ratio") or 0) < wall:
                            continue
                    except Exception:
                        continue
                if sess == "closed":
                    continue
                if sess in ("pre", "after") and not L.get("nxt"):
                    continue  # NXT 시간엔 NXT 상장종목만(ETF·미상장 일반종목 제외)
                if not armed:  # 페이퍼: 매수+목표매도 동시 '걸어놓음'
                    L["st"] = "SELL_PLACED"
                    log(s, code, L, "BUY+SELL", f"paper bracket {sess}")
                    print(f"[{now:%H:%M:%S}] (paper)BUY {L['name']}x{L['qty']}@{L['lim']:,} +목표@{L['tgt']:,} [{sess}]", flush=True)
                    continue
                try:
                    b = orders.buy(c, code, L["qty"], price=L["lim"], market=False, live=True, exchange=exch)
                except Exception as e:
                    L["st"] = "FAILED"; log(s, code, L, "BUY_ERR", str(e)[:40]); continue
                if b.get("ok"):
                    L["st"] = "ORDERED"; L["ono"] = b.get("order_no"); L["org"] = b.get("org_no")
                    log(s, code, L, "BUY", f"no={b.get('order_no')}")
                    print(f"[{now:%H:%M:%S}] BUY {s} {L['name']}x{L['qty']}@{L['lim']:,}", flush=True)
                else:
                    L["st"] = "FAILED"; log(s, code, L, "BUY_REJECT", (b.get("msg") or "")[:40])
                    print(f"[{now:%H:%M:%S}] REJECT {L['name']}: {b.get('msg')}", flush=True)
            elif st == "ORDERED" and armed:        # 이 주문번호의 체결수량으로 트랑쉐별 체결확인
                fq = fl.get(L.get("ono"), 0)
                if fq >= 1:
                    sq = min(fq, L["qty"])
                    try:
                        sr = orders.sell(c, code, sq, price=L["tgt"], market=False, live=True, exchange=exch)
                        if sr.get("ok"):
                            L.update(st="SELL_PLACED", sono=sr.get("order_no"), sorg=sr.get("org_no"), sqty=sq)
                            log(s, code, L, "FILLED+SELL", "목표매도등록")
                            print(f"[{now:%H:%M:%S}] FILLED {L['name']}x{sq} → 목표@{L['tgt']:,} 등록", flush=True)
                        else:
                            L.update(st="HOLD", sqty=sq); log(s, code, L, "SELL_FAIL", (sr.get("msg") or "")[:30])
                    except Exception:
                        L.update(st="HOLD", sqty=sq)
            elif st == "SELL_PLACED":
                if not armed:
                    if cur >= L["tgt"]:
                        L["st"] = "DONE"; log(s, code, L, "SOLD", "paper +target")
                        print(f"[{now:%H:%M:%S}] (paper)SOLD {L['name']}@{L['tgt']:,}", flush=True)
                elif fl.get(L.get("sono"), 0) >= L.get("sqty", L["qty"]):
                    L["st"] = "DONE"; log(s, code, L, "SOLD", "목표체결")
                    print(f"[{now:%H:%M:%S}] SOLD {L['name']}@{L['tgt']:,}", flush=True)
            elif st == "HOLD" and armed:  # 매도 등록 실패분 재시도
                try:
                    sr = orders.sell(c, code, L.get("sqty") or L["qty"], price=L["tgt"], market=False, live=True, exchange=exch)
                    if sr.get("ok"):
                        L.update(st="SELL_PLACED", sono=sr.get("order_no"), sorg=sr.get("org_no"))
                except Exception:
                    pass
        time.sleep(poll)

    if armed:  # 마감 전: 미체결 주문 취소 → stratv0 '자기 체결분'만 청산(계좌 기존 보유분 보호)
        fl = fills()  # 청산 시점 주문번호별 체결수량 스냅샷
        for L in legs.values():
            if L["st"] == "ORDERED" and L.get("ono"):
                try:
                    orders.cancel(c, L["ono"], L.get("org") or "", L["qty"], live=True)
                    log(L["strat"], L["code"], L, "CANCEL", "미체결매수취소")
                except Exception: pass
            if L["st"] in ("SELL_PLACED", "HOLD") and L.get("sono"):
                try: orders.cancel(c, L["sono"], L.get("sorg") or "", L.get("sqty") or L["qty"], live=True)
                except Exception: pass
        # 종목별 'stratv0 순보유 = 오늘 매수체결 − 목표매도체결'만 청산(잔고 전체 _held 절대 사용 금지)
        own = {}
        for L in legs.values():
            bought = min(fl.get(L.get("ono"), 0), L["qty"])  # 이 레그가 실제 매수 체결한 수량
            sold = fl.get(L.get("sono"), 0)                  # 목표매도로 이미 체결된 수량
            rem = bought - sold
            if rem > 0:
                own[L["code"]] = own.get(L["code"], 0) + rem
        for code, q in own.items():    # 자기 인트라데이 잔량만 시장가 인근 청산
            if q < 1:
                continue
            cur = last.get(code) or _price(c, code)[0]
            px = round_tick(cur * 0.99, up=False)
            try:
                orders.sell(c, code, q, price=px, market=False, live=True, exchange=None)
                log("AP", code, {"name": code, "qty": q, "lim": px}, "CLOSE_SELL", "종료청산(자기체결분)")
            except Exception: pass
    f.close()
    done = sum(1 for L in legs.values() if L["st"] == "DONE")
    nf = sum(1 for L in legs.values() if L["st"] not in ("WAIT",))
    return f"[v0 {mode} 종료] {datetime.now(KST):%H:%M} 감시{len(legs)} 진입{nf} 완료{done} · 로그 {path.name}"
