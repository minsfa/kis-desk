"""DART 전자공시 조회 — 종목별 최근 공시 + 호재/악재 자동 라벨링.

급등 종목의 '특이 이슈'를 잡는 내부요인 축. 통계(pattern.py) 위에 오버레이한다.
DART OpenAPI(무료): https://opendart.fss.or.kr → 인증키 발급 후 config/.env 의 DART_API_KEY 설정.

흐름:
  1) corpCode.xml(zip) 1회 다운로드 → 종목코드→corp_code 매핑 캐시(data/dart/corp_map.json)
  2) 공시검색 list.json 으로 최근 N일 공시 조회
  3) 보고서명 키워드로 호재/악재/중립 라벨
순수 공시 데이터일 뿐 매매신호 아님 — 판단 보조용.
"""
from __future__ import annotations
import io
import json
import os
import zipfile
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

from .config import PROJECT_ROOT
from .logging_util import log_event

KST = timezone(timedelta(hours=9))
DART_DIR = PROJECT_ROOT / "data" / "dart"
CORP_MAP = DART_DIR / "corp_map.json"      # 종목코드 → corp_code
NAME_MAP = DART_DIR / "name_map.json"      # 정규화 법인명 → 종목코드 (상장사만)
BASE = "https://opendart.fss.or.kr/api"

# 사업보고서 종류코드
REPRT = {"annual": "11011", "h1": "11012", "q1": "11013", "q3": "11014"}

# 보고서명 키워드 → 라벨. 위에서부터 우선순위(먼저 맞는 것 채택).
BEARISH = ["유상증자", "전환사채", "신주인수권부사채", "교환사채", "감자",
           "블록딜", "최대주주", "횡령", "배임", "영업정지", "상장폐지",
           "불성실공시", "관리종목", "소송", "특별손실"]
BULLISH = ["자기주식취득", "자기주식 취득", "자기주식소각", "무상증자", "단일판매",
           "공급계약", "수주", "영업(잠정)", "현금ㆍ현물배당", "주식배당",
           "합병", "분할", "자산양수"]


def _key() -> str:
    k = os.getenv("DART_API_KEY", "").strip()
    if not k:
        raise RuntimeError("DART_API_KEY 미설정. opendart.fss.or.kr 에서 발급 후 config/.env 에 추가.")
    return k


def _label(title: str) -> str:
    for kw in BEARISH:
        if kw in title:
            return "악재?"
    for kw in BULLISH:
        if kw in title:
            return "호재?"
    return "중립"


def _load_corp_map(force: bool = False) -> dict:
    """종목코드(6자리) → DART corp_code(8자리) 매핑. 최초 1회 또는 force 시 갱신."""
    if CORP_MAP.exists() and not force:
        return json.loads(CORP_MAP.read_text(encoding="utf-8"))
    DART_DIR.mkdir(parents=True, exist_ok=True)
    r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": _key()}, timeout=30)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml)
    mp: dict[str, str] = {}       # 종목코드 → corp_code
    name_mp: dict[str, str] = {}  # 정규화 법인명 → 종목코드 (상장사만)
    for item in root.iter("list"):
        sc = (item.findtext("stock_code") or "").strip()
        cc = (item.findtext("corp_code") or "").strip()
        nm = (item.findtext("corp_name") or "").strip()
        if sc and cc:          # 상장사만(stock_code 있는 것)
            mp[sc] = cc
            if nm:
                name_mp[_norm_name(nm)] = sc
    CORP_MAP.write_text(json.dumps(mp, ensure_ascii=False), encoding="utf-8")
    NAME_MAP.write_text(json.dumps(name_mp, ensure_ascii=False), encoding="utf-8")
    log_event("dart_corpmap", n=len(mp), n_names=len(name_mp))
    return mp


def corp_code(stock_code: str) -> str | None:
    return _load_corp_map().get(stock_code)


HOLDCO_NAMES = DART_DIR / "holdco_names.json"   # 자동발굴 지주사 {code: name}


def list_holding_companies(force: bool = False) -> dict:
    """corpCode.xml에서 이름에 '홀딩스'/'지주'가 든 상장사 자동발굴(국내 보통주만).

    외국계(9xxxxx)·비표준 코드 제외. 데이터 기반이라 코드 수기입력 불필요(무환각)."""
    if HOLDCO_NAMES.exists() and not force:
        return json.loads(HOLDCO_NAMES.read_text(encoding="utf-8"))
    DART_DIR.mkdir(parents=True, exist_ok=True)
    r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": _key()}, timeout=30)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(zf.read(zf.namelist()[0]))
    out: dict[str, str] = {}
    for it in root.iter("list"):
        sc = (it.findtext("stock_code") or "").strip()
        nm = (it.findtext("corp_name") or "").strip()
        if not (sc and nm) or len(sc) != 6 or not sc.isdigit() or sc.startswith("9"):
            continue
        if "홀딩스" in nm or nm.endswith("지주") or "지주회사" in nm:
            out[sc] = nm
    HOLDCO_NAMES.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    log_event("dart_holdco_list", n=len(out))
    return out


# ---- 타법인 출자현황 → 상장 자회사 지분율 (NAV 엔진 입력) ----

# 그룹 한글음차 → 로마자 통일(양측 동일 적용). DART는 자회사를 한글음차("씨제이제일제당"),
# corpCode는 로마자("CJ제일제당")로 적는 일이 많아 이를 맞춰 매칭률을 올린다.
_ALIAS_TOKENS = {
    "씨제이": "CJ", "엘지": "LG", "에스케이": "SK", "엘에스": "LS",
    "지에스": "GS", "케이티앤지": "KT&G", "케이티": "KT", "디엘": "DL",
    "에이치디현대": "HD현대", "에이치엘": "HL", "케이씨씨": "KCC",
    "포스코": "POSCO", "에스엠": "SM",
}


def _norm_name(s: str) -> str:
    """법인명 정규화 — 그룹 음차 통일 + (주)/공백/문장부호 제거 후 비교 키.

    DART 출자현황 법인명(필러 자유기재)과 corpCode 정식명을 느슨히 매칭하기 위함.
    완벽치 않으므로 미매칭은 호출부에서 그대로 노출(은닉 절단 금지)."""
    import re
    s = s or ""
    s = s.replace("주식회사", "").replace("(주)", "").replace("㈜", "")
    # 각주·주석 토큰 제거: (주2) (*1) (주1),(주7) 등
    s = re.sub(r"\([^)]*\)", "", s)
    s = re.sub(r"주\d+", "", s)
    # 주식 종류 꼬리표 제거(매칭 방해)
    for w in ("보통주식", "우선주식", "보통주", "우선주", "보통", "우선"):
        s = s.replace(w, "")
    s = re.sub(r"[\s\.,_\-()·*]", "", s)
    s = s.upper()
    for ko, ro in _ALIAS_TOKENS.items():   # 음차→로마자 통일
        s = s.replace(ko.upper(), ro)
    return s


def _load_name_map(force: bool = False) -> dict:
    """정규화 법인명 → 종목코드(상장사). corpCode.xml 기반(없으면 재다운로드)."""
    if NAME_MAP.exists() and not force:
        return json.loads(NAME_MAP.read_text(encoding="utf-8"))
    _load_corp_map(force=True)  # NAME_MAP 동시 생성
    return json.loads(NAME_MAP.read_text(encoding="utf-8")) if NAME_MAP.exists() else {}


def other_corp_investments(stock_code: str, year: int, reprt: str = "annual") -> list[dict]:
    """타법인 출자현황(otrCprInvstmntSttus). 출자대상 법인명·기말 지분율·장부가액.

    reprt: annual|h1|q1|q3. 반환 각 항목: {name, stake_pct, book_eok}. 상장 매칭은 호출부.
    """
    cc = corp_code(stock_code)
    if not cc:
        log_event("dart_nocorp", code=stock_code)
        return []
    r = requests.get(f"{BASE}/otrCprInvstmntSttus.json", timeout=20, params={
        "crtfc_key": _key(), "corp_code": cc,
        "bsns_year": str(year), "reprt_code": REPRT.get(reprt, reprt),
    })
    d = r.json()
    status = d.get("status")
    if status not in ("000", "013"):
        log_event("error", op="dart_invst", code=stock_code, status=status, msg=d.get("message"))
        return []
    out = []
    for it in d.get("list", []) or []:
        name = (it.get("inv_prm") or "").strip()
        if not name:
            continue
        rate = _num(it.get("trmend_blce_qota_rt"))   # 기말 지분율(%)
        if rate is None:                              # 기말 없으면 기초로 보완
            rate = _num(it.get("bsis_blce_qota_rt"))
        book = _num(it.get("trmend_blce_acntbk_amount"))   # 기말 장부가액(원)
        if book is None:
            book = _num(it.get("bsis_blce_acntbk_amount"))
        out.append({"name": name, "stake_pct": rate,
                    "book_eok": round(book / 1e8) if book else None})
    log_event("dart_invst", code=stock_code, year=year, reprt=reprt, n=len(out))
    return out


def _num(v) -> float | None:
    """DART 숫자문자열(콤마/하이픈/공백) → float."""
    if v is None:
        return None
    s = str(v).replace(",", "").replace(" ", "").strip()
    if s in ("", "-", "比"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def match_listed(name: str) -> str | None:
    """출자대상 법인명 → 상장 종목코드(매칭 실패 시 None)."""
    return _load_name_map().get(_norm_name(name))


def recent_disclosures(stock_code: str, days: int = 7) -> list[dict]:
    """최근 N일 공시 목록 + 호재/악재 라벨. 최신순."""
    cc = corp_code(stock_code)
    if not cc:
        log_event("dart_nocorp", code=stock_code)
        return []
    end = datetime.now(KST)
    bgn = end - timedelta(days=days)
    r = requests.get(f"{BASE}/list.json", timeout=15, params={
        "crtfc_key": _key(), "corp_code": cc,
        "bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
        "page_count": 100,
    })
    d = r.json()
    status = d.get("status")
    if status not in ("000", "013"):   # 000=정상, 013=조회결과없음
        log_event("error", op="dart_list", code=stock_code, status=status, msg=d.get("message"))
        return []
    out = []
    for it in d.get("list", []) or []:
        title = it.get("report_nm", "")
        out.append({
            "date": it.get("rcept_dt"),
            "title": title,
            "flag": _label(title),
            "filer": it.get("flr_nm"),
            "rcept_no": it.get("rcept_no"),
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it.get('rcept_no')}",
        })
    log_event("dart", code=stock_code, days=days, n=len(out),
              bearish=sum(1 for x in out if x["flag"] == "악재?"),
              bullish=sum(1 for x in out if x["flag"] == "호재?"))
    return out


def summary(stock_code: str, days: int = 7) -> str:
    """텔레그램/CLI용 한 블록 요약."""
    ds = recent_disclosures(stock_code, days)
    if not ds:
        return f"📄 DART({stock_code}) 최근 {days}일: 공시 없음 / 조회불가"
    lines = [f"📄 DART({stock_code}) 최근 {days}일 공시 {len(ds)}건"]
    for x in ds[:10]:
        mark = {"악재?": "🔴", "호재?": "🟢", "중립": "⚪"}.get(x["flag"], "⚪")
        lines.append(f"  {mark} {x['date']} {x['title']} ({x['filer']})")
    return "\n".join(lines)
