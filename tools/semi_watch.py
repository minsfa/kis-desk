"""삼전닉스(삼성전자·SK하이닉스) 양방향 변곡 신호 감시 — 크론용.
🔥고하이(급등·신고가) / ❄️고다운(급락·60일선 이탈) 신호 시만 알림, 평소 'OK'(침묵).
외국인 5일 수급은 맥락으로 첨부. src/ 밖(tools/)이라 라이브 게이트 무관(import만)."""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트

from src.kis.config import load_settings
from src.kis.client import KisClient
from src.kis.fundamentals import get_fundamentals
from src.kis.regime import _kr_closes
from src.kis import investor

STOCKS = [("삼성전자", "005930"), ("SK하이닉스", "000660")]


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def analyze(c, code):
    f = get_fundamentals(c, code)
    cl = _kr_closes(c, code)
    px = f.get("price") or (cl[0] if cl else None)
    chg = f.get("change_pct") or 0.0
    ma60 = statistics.fmean(cl[:60]) if len(cl) >= 60 else None
    hi = max(cl) if cl else None
    z = None
    if len(cl) >= 20:
        sd = statistics.pstdev(cl[:20])
        z = (cl[0] - statistics.fmean(cl[:20])) / sd if sd else 0.0
    rows = investor.fetch(c, code)
    frgn5 = sum(_num(r.get("frgn_ntby_tr_pbmn")) for r in rows[:5]) / 100.0  # 억원
    return {
        "px": px, "chg": chg, "z": z, "frgn5": frgn5,
        "vs_ma": (px / ma60 - 1) * 100 if (ma60 and px) else None,
        "below_ma": bool(ma60 and px and px < ma60),
        "new_high": bool(hi and px and px >= hi * 0.999),
    }


def main():
    c = KisClient(load_settings("prod"))
    lines, tags = [], set()
    for nm, code in STOCKS:
        try:
            a = analyze(c, code)
        except Exception as e:
            lines.append(f"{nm}: 조회실패 {str(e)[:40]}"); continue
        sig = []
        if a["chg"] <= -3:
            sig.append("❄️급락"); tags.add("down")
        if a["below_ma"]:
            sig.append("❄️60일선이탈"); tags.add("down")
        if a["chg"] >= 5:
            sig.append("🔥급등"); tags.add("high")
        if a["new_high"] and a["chg"] >= 2:
            sig.append("🔥신고가"); tags.add("high")
        vs = f"{a['vs_ma']:+.0f}%" if a["vs_ma"] is not None else "n/a"
        z = f"{a['z']:+.1f}" if a["z"] is not None else "n/a"
        lines.append(f"  {nm}: {a['px']:,.0f} {a['chg']:+.1f}% | 60일선{vs} z{z} | "
                     f"외국인5일 {a['frgn5']:+,.0f}억 {' '.join(sig)}")
    if tags:
        head = ("🔥🔥 고하이 신호(상승 변곡)" if tags == {"high"}
                else "❄️❄️ 고다운 신호(하락 변곡)" if tags == {"down"}
                else "⚡ 양방향 혼조 신호")
        print(head + "\n" + "\n".join(lines))
    else:
        print("OK 중립(변곡 신호 없음)\n" + "\n".join(lines))


if __name__ == "__main__":
    main()
