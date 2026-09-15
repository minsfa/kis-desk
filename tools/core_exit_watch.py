"""코어 반도체(삼성전자·SK하이닉스) 트림 — 양방향 매도하한 감시. 매매 없음, 알림만.
2026-09-16 설계. 신호 시에만 첫 줄이 이모지로 시작, 평소 'OK'(침묵). thesis_watch.py 패턴 준수.

원리 (사용자 요구: "이벤트를 기다리되, 트리거가 강해지면 매도하한도 올라가고 약해지면 내려간다")
  1) 기준선 = 200일선(바닥, 이 밑으로는 안 내려감). 기준 고점 = 감시 시작 후 최고 종가(시작 시 20일 고가).
       하한(floor) = 200일선 + K(레벨) × (기준 고점 − 200일선)
       레벨 0: K=0    (하한 = 200일선 그대로)
       레벨 1: K=0.25 (200일선과 고점의 1/4 지점)
       레벨 2: K=0.5
       레벨 3: K=0.8  (고점 바로 밑 — 사실상 즉시 매도 대기)
  2) 레벨은 약세 신호 점수로 정한다 (0~2점→L0, 3~4→L1, 5~6→L2, 7+→L3). 점수는 매일 다시 계산.
       자동 신호: 외국인 5일 누적 순매도(≥2조 +1, ≥5조 +2) · 20일선 아래 +1 · 60일선 아래 +1
                  · 고점 대비 −30% 이상 +1 · 하이닉스 PBR≥9 +1
       수동 신호: 이벤트 판정(--verdict DATE neg|pos|neutral: neg +2, pos −1)
                  · 뉴스 플래그(--news "내용" --pts 1)
  3) 하한은 양방향 — 레벨이 오르면(신호 강화·악재 판정) 위로, 내리면(신호 약화·호재 판정) 아래로.
     가격이 오르면 기준 고점이 올라가 하한도 따라 오름. 가격이 내려도 기준 고점은 안 내려감.
  4) 현재가 < 하한  → 📉 트림 물량 매도 지시 요청 (삼성 62→50주=12주, 하이닉스 5→3주=2주)
     현재가 ≥ 상방 게이트(삼성 290,000 / 하이닉스 2,000,000) → 🎯 같은 물량 익절 지시 요청
     이벤트 경과 후 판정 미입력 → 📅 판정 입력 요청
     레벨·하한 변동 → ⬆️/⬇️ 통보
  5) 트림이 체결되어 보유수량이 줄면 ✅ 확인 후 기준 고점을 다시 잡아 다음 단계 감시.

실행 예:
  python tools/core_exit_watch.py                       # 크론용 (상태 갱신 + 알림)
  python tools/core_exit_watch.py --verdict 2026-10-08 neg   # 이벤트 판정 입력
  python tools/core_exit_watch.py --news "AI capex 속도조절론" --pts 1
  python tools/core_exit_watch.py --show                # 상태만 출력(갱신 없음)
상태 파일: data/state/core_exit.json
크론: OpenClaw kis-core-exit — 평일 15:05 KST (동시호가 전, 당일 매도 지시 가능)
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트

from src.kis.config import load_settings, STATE_DIR
from src.kis.client import KisClient
from src.kis.fundamentals import get_fundamentals
from src.kis.market import get_balance
from src.kis.daily import download

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "daily"
INV_DIR = ROOT / "data" / "investor_history"
STATE_PATH = STATE_DIR / "core_exit.json"

TARGETS = [
    # name, code, 1차 트림 수량, 상방 익절 게이트, PBR 과열 기준(None=미사용)
    {"name": "삼성전자", "code": "005930", "trim": 12, "gate_up": 290_000, "pbr_hot": None},
    {"name": "SK하이닉스", "code": "000660", "trim": 2, "gate_up": 2_000_000, "pbr_hot": 9.0},
]
K_BY_LEVEL = {0: 0.0, 1: 0.25, 2: 0.5, 3: 0.8}
FX_SELL_1, FX_SELL_2 = 2.0, 5.0      # 외국인 5일 누적 순매도(조원) 임계
DRAWDOWN = -30.0                     # 고점 대비 %
STALE_DAYS = 1                       # 일봉 캐시 허용 나이(일)

# 이벤트 — 경과 후 사람이 판정 입력(neg/pos/neutral). codes=None 이면 두 종목 공통.
EVENTS = [
    (date(2026, 9, 17), "FOMC 결과(새벽 3시) — 반도체 반응", None),
    (date(2026, 10, 8), "삼성전자 3Q 잠정실적 (HBM4 매출 3배 전제)", ["005930"]),
    (date(2026, 10, 29), "SK하이닉스 3Q 실적·주주환원안 (날짜 추정)", ["000660"]),
    (date(2026, 10, 30), "삼성전자 확정실적 + 배당 이사회", ["005930"]),
]
VERDICT_PTS = {"neg": 2, "pos": -1, "neutral": 0}


# ── 상태 ─────────────────────────────────────────────────────────
def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except Exception:
            pass
    return {"floors": {}, "verdicts": {}, "news": [], "qty_ref": {}, "below_days": {}}


def save_state(st):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


# ── 데이터 ───────────────────────────────────────────────────────
def _closes(c, code):
    """data/daily/<code>.csv 종가(과거→최신). 하루 이상 오래되면 갱신."""
    path = DAILY_DIR / f"{code}.csv"
    rows, stale = [], True
    if path.exists():
        try:
            rows = list(csv.DictReader(open(path)))
            last = max(r["date"] for r in rows)
            stale = (date.today() - date(int(last[:4]), int(last[4:6]), int(last[6:8]))).days > STALE_DAYS
        except Exception:
            rows, stale = [], True
    if stale:
        try:
            end = date.today()
            download(c, code, (end - timedelta(days=420)).strftime("%Y%m%d"), end.strftime("%Y%m%d"))
            rows = list(csv.DictReader(open(path)))
        except Exception:
            if not rows and path.exists():
                rows = list(csv.DictReader(open(path)))
    rows.sort(key=lambda r: r["date"])
    return [int(r["close"]) for r in rows if r.get("close")]


def _ma(closes, n):
    return sum(closes[-n:]) / n if closes and len(closes) >= n else None


def _foreign_5d(code):
    """외국인 5일 누적 순매수(조원). 음수=순매도. 데이터 없으면 None."""
    path = INV_DIR / f"{code}.csv"
    if not path.exists():
        return None
    try:
        rows = list(csv.DictReader(open(path)))
        rows.sort(key=lambda r: r["date"])
        vals = [float(r["foreign_netval"]) for r in rows[-5:] if r.get("foreign_netval")]
        return sum(vals) / 1e6 if vals else None  # 백만원 → 조원
    except Exception:
        return None


def _quote(c, code):
    for _ in range(3):
        try:
            f = get_fundamentals(c, code)
            if f.get("price"):
                return f
        except Exception:
            pass
        time.sleep(1.0)
    return None


def _holdings(c):
    for _ in range(4):
        try:
            b = get_balance(c)
            if b.get("holdings"):
                return {h["code"]: int(h["qty"]) for h in b["holdings"]}
        except Exception:
            pass
        time.sleep(2.5)
    return None


# ── 판정 ─────────────────────────────────────────────────────────
def level_of(score):
    return 0 if score <= 2 else 1 if score <= 4 else 2 if score <= 6 else 3


def score_target(t, px, pbr, closes, fx5, st):
    """(점수, [사유]) — 자동 + 수동 신호 합산."""
    pts, why = 0, []
    if fx5 is not None and fx5 <= -FX_SELL_2:
        pts += 2; why.append(f"외인5일 {fx5:+.1f}조(+2)")
    elif fx5 is not None and fx5 <= -FX_SELL_1:
        pts += 1; why.append(f"외인5일 {fx5:+.1f}조(+1)")
    ma20, ma60 = _ma(closes, 20), _ma(closes, 60)
    if ma20 and px < ma20:
        pts += 1; why.append("20일선↓(+1)")
    if ma60 and px < ma60:
        pts += 1; why.append("60일선↓(+1)")
    if closes:
        hi = max(closes[-250:])
        dd = (px / hi - 1) * 100
        if dd <= DRAWDOWN:
            pts += 1; why.append(f"고점대비 {dd:.0f}%(+1)")
    if t["pbr_hot"] and pbr and pbr >= t["pbr_hot"]:
        pts += 1; why.append(f"PBR {pbr:.1f}(+1)")
    for d, desc, codes in EVENTS:
        v = st["verdicts"].get(d.isoformat())
        if v and (codes is None or t["code"] in codes):
            p = VERDICT_PTS.get(v, 0)
            if p:
                pts += p; why.append(f"{d:%m/%d} {v}({p:+d})")
    for n in st["news"]:
        if n.get("pts"):
            pts += int(n["pts"]); why.append(f"뉴스 {n['text'][:12]}({int(n['pts']):+d})")
    return max(pts, 0), why


def compute_floor(t, px, ma200, level, closes, st, today, persist=True):
    """양방향 하한. 반환 (floor, prev_floor, prev_level, ref_high).
    floor = MA200 + K(level) × (기준고점 − MA200), 기준고점 = 감시 시작 후 최고 종가(시작 시 20일 고가)."""
    code = t["code"]
    ent = st["floors"].get(code) or {}
    prev_floor, prev_level = ent.get("floor"), ent.get("level")
    ref_high = ent.get("ref_high") or (max(closes[-20:]) if closes else px)
    ref_high = max(ref_high, px)
    if ma200 is None:
        return prev_floor, prev_floor, prev_level, ref_high
    floor = ma200 + K_BY_LEVEL[level] * max(ref_high - ma200, 0)
    floor = max(floor, ma200)
    if persist:
        st["floors"][code] = {"floor": round(floor), "level": level, "ma200": round(ma200),
                              "ref_high": ref_high, "updated": today.isoformat(),
                              "since": ent.get("since") or today.isoformat()}
    return round(floor), prev_floor, prev_level, ref_high


def check_events(today, st):
    """(임박 목록, 판정 필요 목록)"""
    soon, need = [], []
    for d, desc, _ in EVENTS:
        dd = (d - today).days
        if 0 < dd <= 14:
            soon.append(f"D-{dd:<3} {d:%m/%d}  {desc}")
        if dd <= 0 and dd >= -10 and not st["verdicts"].get(d.isoformat()):
            need.append(f"📅 {d:%m/%d} {desc} — 판정 미입력. 예) --verdict {d.isoformat()} neg|pos|neutral")
    return soon, need


# ── 메인 ─────────────────────────────────────────────────────────
def run(show_only=False):
    today = date.today()
    st = load_state()
    c = KisClient(load_settings("prod"))
    held = _holdings(c) or {}

    alerts, notes, info = [], [], []
    for t in TARGETS:
        code, nm = t["code"], t["name"]
        q = _quote(c, code)
        time.sleep(0.3)
        if not q:
            info.append(f"{nm} 시세 조회실패")
            continue
        px = float(q["price"])
        pbr = float(q.get("pbr") or 0) or None
        closes = _closes(c, code)
        time.sleep(0.3)
        ma200 = _ma(closes, 200)
        fx5 = _foreign_5d(code)
        qty = held.get(code)

        # 트림 체결 감지 → 하한 재설정
        ref = st["qty_ref"].get(code)
        if qty is not None:
            if ref is not None and qty < ref and not show_only:
                notes.append(f"✅ {nm} 보유 {ref}→{qty}주 감소 확인 — 하한을 현 레벨 기준으로 재설정")
                st["floors"].pop(code, None)
                st["below_days"].pop(code, None)
            if not show_only:
                st["qty_ref"][code] = qty

        score, why = score_target(t, px, pbr, closes, fx5, st)
        level = level_of(score)
        floor, prev_floor, prev_level, ref_high = compute_floor(
            t, px, ma200, level, closes, st, today, persist=not show_only)

        trim = min(t["trim"], qty) if qty else t["trim"]
        tag = f"{nm} {px:,.0f}"
        why_s = " · ".join(why) or "신호 없음"
        if floor:
            gap = (px / floor - 1) * 100
            if px < floor:
                n = st["below_days"].get(code, 0) + (0 if show_only else 1)
                if not show_only:
                    st["below_days"][code] = n
                alerts.append(f"📉 {tag} < 하한 {floor:,.0f} (L{level}, {n}일째) — {trim}주 매도 지시?")
            else:
                if not show_only:
                    st["below_days"][code] = 0
                info.append(f"{tag} / 하한 {floor:,.0f} ({gap:+.1f}%) L{level} 점수{score}")
        if px >= t["gate_up"]:
            alerts.append(f"🎯 {tag} ≥ 상방 게이트 {t['gate_up']:,} — {trim}주 익절 지시?")
        if floor is not None and prev_floor is None and not show_only:
            notes.append(f"⬆️ {nm} 하한 최초 설정 {floor:,.0f} (L{level}: {why_s}; 200일선 {ma200:,.0f}, 기준고점 {ref_high:,.0f})")
        elif floor is not None and prev_floor is not None and (floor != prev_floor or level != prev_level):
            arrow = "⬆️" if floor > prev_floor or (floor == prev_floor and level > (prev_level or 0)) else "⬇️"
            notes.append(f"{arrow} {nm} 하한 {prev_floor:,.0f}→{floor:,.0f}, 레벨 L{prev_level}→L{level} [{why_s}]")
        if show_only:
            info.append(f"   신호: {why_s}; 200일선 {ma200:,.0f}, 기준고점 {ref_high:,.0f}" if ma200 else "   200일선 계산불가")

    soon, need = check_events(today, st)
    if not show_only:
        save_state(st)

    urgent = alerts + need + notes
    lines = []
    if urgent:
        lines += urgent
    if soon:
        lines.append("■ 다가오는 이벤트 (2주 내)")
        lines += soon
    if urgent:
        print("\n".join(lines))
        if info:
            print("--- 참고: " + " · ".join(info))
        return
    print(f"OK 하한 유지 ({today:%m/%d})" + (" | " + " · ".join(info) if info else ""))
    if soon:
        print("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verdict", nargs=2, metavar=("DATE", "VERDICT"), help="이벤트 판정 입력 (neg|pos|neutral)")
    ap.add_argument("--news", help="뉴스 플래그 텍스트")
    ap.add_argument("--pts", type=int, default=1, help="뉴스 플래그 점수 (기본 1, 해제는 0)")
    ap.add_argument("--clear-news", action="store_true", help="뉴스 플래그 전부 삭제")
    ap.add_argument("--show", action="store_true", help="상태만 출력(갱신 없음)")
    a = ap.parse_args()

    if a.verdict or a.news or a.clear_news:
        st = load_state()
        if a.verdict:
            d, v = a.verdict
            if v not in VERDICT_PTS:
                sys.exit("verdict는 neg|pos|neutral")
            st["verdicts"][d] = v
            print(f"판정 저장: {d} = {v} ({VERDICT_PTS[v]:+d}점)")
        if a.clear_news:
            st["news"] = []
            print("뉴스 플래그 삭제")
        if a.news:
            st["news"] = [n for n in st["news"] if n["text"] != a.news]
            if a.pts:
                st["news"].append({"date": date.today().isoformat(), "text": a.news, "pts": a.pts})
            print(f"뉴스 플래그: {a.news} ({a.pts:+d}점)")
        save_state(st)
        return
    run(show_only=a.show)


if __name__ == "__main__":
    main()
