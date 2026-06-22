"""국내 증시 신용거래융자 잔고(개인 '빚투') 트리거 체크 — KOFIA FreeSIS.
크론용. 사상최고(약 38조) 근접/돌파 또는 급증 시에만 🚨 알림, 평소엔 'OK'(침묵).
--show 플래그로 최근 ~10영업일 추이 표를 항상 출력(즉시 조회용).
src/ 밖(tools/)이라 라이브 매매 무결성 게이트와 무관.

데이터 소스(공개·키 불필요, 역설계로 확정):
  POST https://freesis.kofia.or.kr/meta/getMetaDataList.do
  Content-Type: application/json
  body: {"dmSearch":{"tmpV1":"D","tmpV40":"1000000","tmpV41":"1",
                     "tmpV45":<시작 YYYYMMDD>,"tmpV46":<종료 YYYYMMDD>,
                     "OBJ_NM":"STATSCU0100000070BO"}}
  serviceId STATSCU0100000070 = '신용공여 잔고 추이'. 응답 ds1 행(일자별, 단위 백만원):
    TMPV1=일자  TMPV2=신용거래융자 전체  TMPV3=유가증권(코스피)  TMPV4=코스닥
    (TMPV5~7=신용거래대주, TMPV8=청약자금대출, TMPV9=예탁증권담보융자)
  최신 영업일은 getSrvData.do의 dsListAppDt.TMPV1 로 조회(하드코딩 없음).
"""
import sys
from datetime import datetime, timedelta

import requests

BASE = "https://freesis.kofia.or.kr"
SERVICE_ID = "STATSCU0100000070"
OBJ_NM = "STATSCU0100000070BO"
HEADERS = {"Content-Type": "application/json; charset=UTF-8",
           "User-Agent": "Mozilla/5.0 (credit_check)"}

# 경고 임계선(조 단위, 신용거래융자 '전체' 기준)
RECORD = 38.0   # 사상최고권. 이 이상이면 신고가 갱신/돌파로 간주 🚨
WATCH = 36.0    # 근접 감시선. 이 이상이면 보고, 미만이면 침묵(OK)
SURGE = 0.5     # 직전 영업일 대비 +0.5조 이상 급증 시 🚨


def _latest_biz_date() -> str:
    """getSrvData.do 의 dsListAppDt.TMPV1 = 최신(승인) 영업일 YYYYMMDD."""
    r = requests.post(f"{BASE}/meta/getSrvData.do", headers=HEADERS, timeout=10, json={
        "dmSearchData": {"strSvrId": SERVICE_ID, "strDivId": "MSIS10000000000000",
                         "app_peron_yn": "Y", "language_gb": "KOR", "strGetCode": "N"}})
    r.raise_for_status()
    return r.json()["dsListAppDt"][0]["TMPV1"]


def fetch_rows(days_back: int = 20):
    """최근 days_back 일치 신용거래융자 잔고를 일자 내림차순으로 반환.
    각 행: {"date","total","kospi","kosdaq"} (조 단위 float)."""
    try:
        end = _latest_biz_date()
    except Exception:
        end = datetime.now().strftime("%Y%m%d")
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=days_back * 2)).strftime("%Y%m%d")
    r = requests.post(f"{BASE}/meta/getMetaDataList.do", headers=HEADERS, timeout=10, json={
        "dmSearch": {"tmpV1": "D", "tmpV40": "1000000", "tmpV41": "1",
                     "tmpV45": start, "tmpV46": end, "OBJ_NM": OBJ_NM}})
    r.raise_for_status()
    rows = []
    for d in r.json().get("ds1", []):
        try:
            rows.append({
                "date": str(d["TMPV1"]),
                "total": float(d["TMPV2"]) / 1_000_000,   # 백만원 → 조원
                "kospi": float(d["TMPV3"]) / 1_000_000,
                "kosdaq": float(d["TMPV4"]) / 1_000_000,
            })
        except (KeyError, ValueError, TypeError):
            continue
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows


def _fmt_date(s: str) -> str:
    return f"{s[:4]}/{s[4:6]}/{s[6:8]}" if len(s) == 8 else s


def show(rows):
    """최근 ~10영업일 추이 표 출력(--show 용)."""
    print("신용거래융자 잔고 추이 (KOFIA, 단위: 조원)")
    print(f"{'일자':>12} {'전체':>8} {'유가증권':>9} {'코스닥':>8} {'전일대비':>9}")
    top = rows[:10]
    for i, x in enumerate(top):
        # 내림차순이므로 직전 영업일 = 한 칸 뒤 행. (i+1 행이 더 과거)
        prev = rows[i + 1]["total"] if i + 1 < len(rows) else None
        diff = f"{x['total'] - prev:+.2f}" if prev is not None else "  -  "
        print(f"{_fmt_date(x['date']):>12} {x['total']:>8.2f} {x['kospi']:>9.2f} "
              f"{x['kosdaq']:>8.2f} {diff:>9}")


def main():
    want_show = "--show" in sys.argv
    try:
        rows = fetch_rows()
    except Exception as e:
        print(f"OK (신용잔고 조회 실패: {e})")
        return
    if not rows:
        print("OK (신용잔고 데이터 없음)")
        return

    if want_show:
        show(rows)

    cur = rows[0]
    prev = rows[1]["total"] if len(rows) > 1 else None
    delta = (cur["total"] - prev) if prev is not None else 0.0
    delta_str = f"전일대비 {delta:+.2f}조" if prev is not None else "전일대비 n/a"
    head = (f"신용거래융자 전체 {cur['total']:.2f}조 "
            f"(유가증권 {cur['kospi']:.2f}조 / 코스닥 {cur['kosdaq']:.2f}조) "
            f"{delta_str} [{_fmt_date(cur['date'])}]")

    triggered = cur["total"] >= WATCH or delta >= SURGE
    if triggered:
        flags = []
        if cur["total"] >= RECORD:
            flags.append(f"사상최고권({RECORD:.0f}조) 돌파")
        elif cur["total"] >= WATCH:
            flags.append(f"{WATCH:.0f}조 감시선 상회")
        if delta >= SURGE:
            flags.append(f"하루 +{delta:.2f}조 급증")
        print(f"🚨 {head} — {', '.join(flags)}. 빚투 과열 점검 권장.")
    elif want_show:
        print(f"OK {head} — 트리거({WATCH:.0f}조) 미도달.")
    else:
        print(f"OK {head} — 트리거({WATCH:.0f}조) 미도달, 침묵")


if __name__ == "__main__":
    main()
