"""네이버 검색 API(뉴스) — 종목 급변의 '왜'를 잡는 외부요인 축.

DART(공시)가 못 잡는 사건(매크로·루머·실적코멘트·수급기사)을 보완한다.
발급: https://developers.naver.com → 검색 API → Client ID/Secret → config/.env.
순수 기사 메타데이터(제목·날짜·링크)일 뿐 매매신호 아님 — 호재/악재 판단은 LLM 프롬프트 단계에서.
"""
from __future__ import annotations
import html
import os
import re
from datetime import datetime, timezone, timedelta

import requests

from .logging_util import log_event

KST = timezone(timedelta(hours=9))
ENDPOINT = "https://openapi.naver.com/v1/search/news.json"
_TAG = re.compile(r"<[^>]+>")


def _creds() -> tuple[str, str]:
    cid = os.getenv("NAVER_CLIENT_ID", "").strip()
    sec = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    if not (cid and sec):
        raise RuntimeError("NAVER_CLIENT_ID/SECRET 미설정. developers.naver.com 검색 API 발급 후 config/.env 에 추가.")
    return cid, sec


def _clean(s: str) -> str:
    """네이버가 끼워주는 <b> 태그·HTML 엔티티 제거."""
    return html.unescape(_TAG.sub("", s or "")).strip()


def search(query: str, display: int = 10, sort: str = "date") -> list[dict]:
    """뉴스 검색. sort: 'date'(최신) | 'sim'(정확도). 최대 100건."""
    cid, sec = _creds()
    r = requests.get(ENDPOINT, timeout=10,
                     headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec},
                     params={"query": query, "display": min(max(display, 1), 100), "sort": sort})
    if r.status_code != 200:
        log_event("error", op="naver_news", q=query, status=r.status_code, body=r.text[:200])
        r.raise_for_status()
    items = r.json().get("items", []) or []
    out = [{
        "title": _clean(it.get("title")),
        "desc": _clean(it.get("description")),
        "date": it.get("pubDate"),          # 예: 'Wed, 04 Jun 2026 18:30:00 +0900'
        "link": it.get("originallink") or it.get("link"),
    } for it in items]
    log_event("news", q=query, n=len(out))
    return out


def _name(code: str) -> str | None:
    """보유 유니버스에서 종목코드→이름. 없으면 None."""
    from .daily import VALUE_BASKET
    from .strat_v0 import C1_LEADERS
    from . import stratcfg
    m = dict(VALUE_BASKET); m.update(C1_LEADERS)
    try:
        m.update(stratcfg.load().get("c1_extra", {}))
    except Exception:
        pass
    return m.get(code)


def for_stock(code: str, name: str | None = None, display: int = 10,
              qualifier: str = "주가") -> list[dict]:
    """종목 단위 뉴스. name 미지정 시 유니버스에서 자동 해석, 없으면 코드로 검색.
    qualifier: 금융 한정어(기본 '주가') — 후원 스포츠단 등 동명 노이즈 제거용. '' 면 미적용."""
    base = name or _name(code) or code
    q = f"{base} {qualifier}".strip() if qualifier else base
    return search(q, display=display, sort="date")


def summary(code: str, name: str | None = None, n: int = 8, qualifier: str = "주가") -> str:
    """텔레그램/CLI용 한 블록 요약."""
    base = name or _name(code) or code
    items = for_stock(code, name=base, display=n, qualifier=qualifier)
    if not items:
        return f"📰 뉴스({base}) 최근: 없음 / 조회불가"
    lines = [f"📰 뉴스({base}) 최근 {len(items)}건"]
    for it in items:
        lines.append(f"  · {it['title']}")
    return "\n".join(lines)
