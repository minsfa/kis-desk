"""투자자별 상세 매매동향 감시 — KRX 정보데이터시스템(투자자별 거래실적 개별종목).
연기금(장기 스마트머니) 순매수/순매도 추세와 방향 전환을 핵심으로 본다.
기존 kis investor 는 개인/외국인/기관 3분류뿐이라, 기관을 9분류(연기금·금융투자·투신·
보험·사모·은행·기타금융·국가/기타금융·기타법인)로 쪼개 "기관=연기금인지 금융투자(증권사
자기매매=프로그램/헤지성)인지"를 구분하기 위함.

크론용 기본(트리거) 모드: 연기금 방향 전환(순매수↔순매도) 또는 큰 변동 시에만 🚨,
평소엔 'OK'(침묵). --show 로 감시 종목별 최근 ~10영업일 투자자별 순매수 표를 항상 출력.
src/ 밖(tools/)이라 라이브 매매 무결성 게이트와 무관(독립 실행 스크립트).

────────────────────────────────────────────────────────────────────────────
데이터 소스(역설계로 확정 — playwright XHR 캡처 + pykrx 교차검증):
  POST https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd
  헤더: User-Agent, Referer(outerLoader), X-Requested-With: XMLHttpRequest
  (1) 종목 표준코드 조회(로그인 불필요):
      bld=dbms/comm/finder/finder_stkisu, mktsel=ALL, searchText=<코드 또는 이름>
      → block1[].full_code (예: 005930 → KR7005930003)
  (2) 개별종목 투자자별 거래실적 일별추이(상세 9/11분류):
      bld=dbms/MDC/STAT/standard/MDCSTAT02303
      params: isuCd=<full_code>, strtDd, endDd(YYYYMMDD), inqTpCd=2(일별추이),
              trdVolVal=2(거래대금)|1(거래량), askBid=3(순매수)|1(매도)|2(매수),
              detailView=1(상세 분류)
      응답 output[] (일자별): TRD_DD + TRDVAL1..TRDVAL11 + TRDVAL_TOT.
      컬럼 순서(KRX 상세 투자자 분류 표준 좌→우; 단위=거래대금 '원'):
        TRDVAL1=금융투자 2=보험 3=투신 4=사모 5=은행 6=기타금융 7=연기금등
        8=기타법인 9=개인 10=외국인 11=기타외국인  TRDVAL_TOT=전체
      → 기관계 = TRDVAL1~7 합. 연기금 = TRDVAL7.
  (3) 시장 전체(코스피 등): bld=dbms/MDC/STAT/standard/MDCSTAT02203
      params: mktId=STK, strtDd, endDd, inqTpCd=2, trdVolVal=2, askBid=3, detailView=1
      (개별종목과 동일한 TRDVAL1..11 컬럼 구조)

⚠️ 인증 게이트(중요·한계):
  현행 KRX 'Data Marketplace'는 MDCSTAT 투자자 통계를 **로그인 사용자에게만** 제공한다.
  로그아웃 상태에서 getJsonData.cmd 는 문자열 "LOGOUT"(HTTP 400) 또는 빈 응답을 반환한다.
  (이전 신용 bld LOGOUT 은 bld 오류였지만, 여기 LOGOUT 은 '로그인 필요'가 원인 — 확인됨.)
  finder(종목검색)는 비로그인도 동작하나, 투자자 거래실적 본문은 로그인 필수.
  → KRX 무료 회원 계정을 config/.env 또는 환경변수에 KRX_ID / KRX_PW 로 넣으면
     자동 로그인하여 실데이터를 받는다. 자격증명이 없으면 fail-soft 로
     'OK (조회 실패: KRX 로그인 필요 …)' 만 출력하고 죽지 않는다.
  로그인 흐름(pykrx auth 와 동일, 검증됨):
    GET MDCCOMS001.cmd → GET login.jsp → POST MDCCOMS001D1.cmd(mbrId/pw)
    _error_code: CD001=성공, CD011=중복로그인(skipDup=Y 재전송), CD010=비번변경필요.
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── 감시 대상(상수). 코드만 넣으면 이름은 finder 로 자동 채움. ──────────────
STOCKS = [("삼성전자", "005930"), ("SK하이닉스", "000660")]
WATCH_MARKET = True            # 코스피 시장 전체도 함께 감시
MARKET_ID, MARKET_NAME = "STK", "코스피(전체)"

# 트리거 기준(연기금, 단위 억원). 보수적으로 잡아 노이즈 최소화.
PENSION_FLIP = True            # 연기금 순매수↔순매도 방향 전환 시 🚨
PENSION_SURGE = 500.0          # 최근 1일 연기금 순매수/도 ±500억 이상이면 큰 변동 🚨

BASE = "https://data.krx.co.kr"
GETJSON = f"{BASE}/comm/bldAttendant/getJsonData.cmd"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REFERER = f"{BASE}/contents/MDC/MDI/outerLoader/index.cmd"
HJSON = {"User-Agent": UA, "Referer": REFERER, "X-Requested-With": "XMLHttpRequest"}

# TRDVAL1..11 → 투자자명. 기관계 = idx 0..6(금융투자~연기금등) 합.
INVESTOR_COLS = ["금융투자", "보험", "투신", "사모", "은행", "기타금융",
                 "연기금등", "기타법인", "개인", "외국인", "기타외국인"]
ORG_IDX = range(0, 7)          # 기관계 구성(금융투자~연기금등)
PENSION_IDX = 6                # 연기금등
FORN_IDX = 9                   # 외국인
FININV_IDX = 0                 # 금융투자

# 로그인 엔드포인트(검증됨)
LOGIN_PAGE = f"{BASE}/contents/MDC/COMS/client/MDCCOMS001.cmd"
LOGIN_JSP = f"{BASE}/contents/MDC/COMS/client/view/login.jsp?site=mdc"
LOGIN_URL = f"{BASE}/contents/MDC/COMS/client/MDCCOMS001D1.cmd"


# ── 설정값(config/.env 또는 환경변수) ───────────────────────────────────────
def _env(key: str):
    v = os.getenv(key)
    if v:
        return v.strip()
    env = Path(__file__).resolve().parent.parent / "config" / ".env"
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return None


# ── KRX 세션(로그인 포함) ────────────────────────────────────────────────────
def krx_session() -> requests.Session:
    """워밍업 + (자격증명 있으면) 로그인된 requests 세션 반환.
    자격증명이 없거나 로그인 실패해도 세션 자체는 반환(finder 등 비로그인 호출용)."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"})
    # 워밍업: JSESSIONID 발급
    s.get(LOGIN_PAGE, timeout=15)
    s.get(LOGIN_JSP, headers={"Referer": LOGIN_PAGE}, timeout=15)
    uid, pw = _env("KRX_ID"), _env("KRX_PW")
    if uid and pw:
        _login(s, uid, pw)
    return s


def _login(s: requests.Session, uid: str, pw: str) -> bool:
    payload = {"mbrNm": "", "telNo": "", "di": "", "certType": "", "mbrId": uid, "pw": pw}
    r = s.post(LOGIN_URL, data=payload, headers={"Referer": LOGIN_PAGE}, timeout=15)
    try:
        data = r.json()
    except Exception:
        return False
    code = data.get("_error_code", "")
    if code == "CD011":                       # 중복 로그인 → 기존 세션 밀어내기
        payload["skipDup"] = "Y"
        try:
            code = s.post(LOGIN_URL, data=payload,
                          headers={"Referer": LOGIN_PAGE}, timeout=15
                          ).json().get("_error_code", "")
        except Exception:
            return False
    return code == "CD001"


def _post_json(s: requests.Session, bld: str, **params) -> dict:
    """getJsonData 호출. 로그아웃/오류면 명확한 예외."""
    body = {"bld": bld, "locale": "ko_KR"}
    body.update(params)
    r = s.post(GETJSON, headers=HJSON, data=body, timeout=15)
    txt = (r.text or "").strip()
    if txt == "LOGOUT" or (r.status_code == 400 and "LOGOUT" in txt):
        raise RuntimeError("KRX 로그인 필요(config/.env 의 KRX_ID/KRX_PW 설정 권장)")
    if not txt:
        raise RuntimeError("KRX 빈 응답(로그인 필요 추정)")
    return r.json()


# ── 데이터 취득 ──────────────────────────────────────────────────────────────
def find_isu(s: requests.Session, code_or_name: str):
    """종목 단축코드/이름 → (이름, 단축코드, 표준코드). finder 는 비로그인 동작."""
    j = _post_json(s, "dbms/comm/finder/finder_stkisu",
                   mktsel="ALL", typeNo="0", searchText=code_or_name)
    rows = j.get("block1", [])
    if not rows:
        raise RuntimeError(f"종목 검색 실패: {code_or_name}")
    # 단축코드 정확 일치 우선
    for x in rows:
        if x.get("short_code") == code_or_name:
            return x["codeName"], x["short_code"], x["full_code"]
    x = rows[0]
    return x["codeName"], x["short_code"], x["full_code"]


def _num(v) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def _parse_rows(output: list) -> list:
    """getJsonData output[] → 일자 내림차순 행 리스트.
    각 행: {date, vals:[11개 순매수 억원], org, total}."""
    rows = []
    for d in output:
        dt = str(d.get("TRD_DD", "")).replace("/", "")
        if len(dt) != 8:
            continue
        vals = [_num(d.get(f"TRDVAL{i + 1}")) / 1e8 for i in range(11)]  # 원→억원
        org = sum(vals[i] for i in ORG_IDX)
        total = _num(d.get("TRDVAL_TOT")) / 1e8
        rows.append({"date": dt, "vals": vals, "org": org, "total": total})
    rows.sort(key=lambda x: x["date"], reverse=True)
    return rows


def fetch_stock(s: requests.Session, full_code: str, days_back: int = 18) -> list:
    """개별종목 투자자별 순매수(거래대금) 일별추이. 단위 억원."""
    end = datetime.now()
    start = end - timedelta(days=days_back * 2)
    j = _post_json(s, "dbms/MDC/STAT/standard/MDCSTAT02303",
                   isuCd=full_code, strtDd=start.strftime("%Y%m%d"),
                   endDd=end.strftime("%Y%m%d"), inqTpCd="2",
                   trdVolVal="2", askBid="3", detailView="1")
    return _parse_rows(j.get("output", []))


def fetch_market(s: requests.Session, mkt_id: str = "STK", days_back: int = 18) -> list:
    """시장 전체 투자자별 순매수(거래대금) 일별추이. 단위 억원."""
    end = datetime.now()
    start = end - timedelta(days=days_back * 2)
    j = _post_json(s, "dbms/MDC/STAT/standard/MDCSTAT02203",
                   mktId=mkt_id, strtDd=start.strftime("%Y%m%d"),
                   endDd=end.strftime("%Y%m%d"), inqTpCd="2",
                   trdVolVal="2", askBid="3", detailView="1")
    return _parse_rows(j.get("output", []))


# ── 출력 ─────────────────────────────────────────────────────────────────────
def _fmt_date(s: str) -> str:
    return f"{s[:4]}/{s[4:6]}/{s[6:8]}" if len(s) == 8 else s


def show_table(name: str, rows: list, n: int = 10):
    """최근 n영업일 투자자별 순매수 표(억원). 연기금/금융투자/외국인 강조."""
    print(f"\n[{name}] 투자자별 순매수 추이 (KRX, 단위: 억원, +순매수/-순매도)")
    hdr = (f"{'일자':>10} {'연기금':>9} {'금융투자':>9} {'투신':>8} {'기관계':>9} "
           f"{'개인':>9} {'외국인':>9}")
    print(hdr)
    print("-" * len(hdr))
    for x in rows[:n]:
        v = x["vals"]
        print(f"{_fmt_date(x['date']):>10} {v[PENSION_IDX]:>9,.0f} {v[FININV_IDX]:>9,.0f} "
              f"{v[2]:>8,.0f} {x['org']:>9,.0f} {v[8]:>9,.0f} {v[FORN_IDX]:>9,.0f}")


def _line(name: str, rows: list) -> tuple:
    """한 줄 요약(연기금 중심 + 외국인·금융투자 병기)과 트리거 여부 반환."""
    cur = rows[0]
    pen = cur["vals"][PENSION_IDX]
    prev_pen = rows[1]["vals"][PENSION_IDX] if len(rows) > 1 else None
    frgn = cur["vals"][FORN_IDX]
    fin = cur["vals"][FININV_IDX]
    flip = (PENSION_FLIP and prev_pen is not None
            and ((pen > 0) != (prev_pen > 0)) and abs(pen) > 50)
    surge = abs(pen) >= PENSION_SURGE
    trend = _trend(rows)
    flags = []
    if flip:
        flags.append(f"연기금 {'순매도→순매수' if pen > 0 else '순매수→순매도'} 전환")
    if surge:
        flags.append(f"연기금 {'매수' if pen > 0 else '매도'} {abs(pen):,.0f}억 급변")
    head = (f"{name}: 연기금 {pen:+,.0f}억 ({trend}) | "
            f"외국인 {frgn:+,.0f}억 / 금융투자 {fin:+,.0f}억 [{_fmt_date(cur['date'])}]")
    return head, flags


def _trend(rows: list, n: int = 5) -> str:
    """최근 n영업일 연기금 누적 순매수로 추세 라벨."""
    cum = sum(r["vals"][PENSION_IDX] for r in rows[:n])
    if cum > 200:
        return f"5일 누적 +{cum:,.0f}억 매수추세"
    if cum < -200:
        return f"5일 누적 {cum:,.0f}억 매도추세"
    return f"5일 누적 {cum:+,.0f}억 중립"


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want_show = "--show" in sys.argv

    # 단일 종목 인자 지원: python tools/investor_detail.py 000660 --show
    if args:
        targets = [(None, args[0])]
        watch_market = False
    else:
        targets = list(STOCKS)
        watch_market = WATCH_MARKET

    try:
        s = krx_session()
    except Exception as e:
        print(f"OK (조회 실패: KRX 세션 생성 실패 {str(e)[:60]})")
        return

    lines, fired = [], []

    for name, code in targets:
        try:
            nm, _short, full = find_isu(s, code)
            nm = name or nm
            rows = fetch_stock(s, full)
            if not rows:
                lines.append(f"{nm}: 데이터 없음"); continue
            if want_show:
                show_table(nm, rows)
            head, flags = _line(nm, rows)
            lines.append(head)
            if flags:
                fired.append(f"{nm} — {', '.join(flags)}")
        except Exception as e:
            lines.append(f"{name or code}: 조회 실패 ({str(e)[:50]})")

    if watch_market:
        try:
            rows = fetch_market(s, MARKET_ID)
            if rows:
                if want_show:
                    show_table(MARKET_NAME, rows)
                head, flags = _line(MARKET_NAME, rows)
                lines.append(head)
                if flags:
                    fired.append(f"{MARKET_NAME} — {', '.join(flags)}")
        except Exception as e:
            lines.append(f"{MARKET_NAME}: 조회 실패 ({str(e)[:50]})")

    # 전부 조회 실패면 fail-soft 단일 라인
    if lines and all("조회 실패" in ln or "세션" in ln for ln in lines):
        print(f"OK (조회 실패: {lines[0].split('(', 1)[-1].rstrip(')')})")
        if want_show:
            for ln in lines:
                print("  " + ln)
        return

    if fired:
        print("🚨 연기금 수급 변곡 신호\n" + "\n".join("  " + f for f in fired)
              + "\n" + "\n".join("  " + ln for ln in lines))
    elif want_show:
        print("\nOK 연기금 변곡 신호 없음\n" + "\n".join("  " + ln for ln in lines))
    else:
        print("OK 연기금 변곡 신호 없음 — " + " / ".join(lines))


if __name__ == "__main__":
    main()
