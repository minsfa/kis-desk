"""접근토큰 발급 + 파일 캐시 + 재발급 스로틀.

토큰 유효 ~24h, 재발급은 빈도 제한이 있으므로 캐시를 우선 사용한다.
캐시는 환경별(token_vts.json / token_prod.json)로 분리.
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timedelta, timezone

import requests

from .config import Settings
from .logging_util import log_event

KST = timezone(timedelta(hours=9))
_REISSUE_MARGIN_SEC = 600  # 만료 10분 전이면 재발급


def _read_cache(s: Settings) -> dict | None:
    p = s.token_cache_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("env") != s.env:
        return None
    if time.time() >= data.get("expires_epoch", 0) - _REISSUE_MARGIN_SEC:
        return None
    return data


def _write_cache(s: Settings, token: str, expires_epoch: float) -> None:
    p = s.token_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "env": s.env,
        "access_token": token,
        "expires_epoch": expires_epoch,
        "issued_at": datetime.now(KST).isoformat(timespec="seconds"),
    }, ensure_ascii=False), encoding="utf-8")
    # 토큰 파일은 .gitignore 처리됨. 소유자만 읽기.
    try:
        p.chmod(0o600)
    except Exception:
        pass


def get_access_token(s: Settings, force: bool = False) -> str:
    """캐시가 유효하면 재사용, 아니면 POST /oauth2/tokenP 로 발급."""
    if not force:
        cached = _read_cache(s)
        if cached:
            return cached["access_token"]

    url = f"{s.host}/oauth2/tokenP"
    body = {
        "grant_type": "client_credentials",
        "appkey": s.app_key,
        "appsecret": s.app_secret,
    }
    resp = requests.post(url, json=body, timeout=10)
    if resp.status_code != 200:
        log_event("error", op="token", status=resp.status_code, body=resp.text[:300])
        raise RuntimeError(f"토큰 발급 실패 ({resp.status_code}): {resp.text[:200]}")
    data = resp.json()
    token = data["access_token"]
    # expires_in(초) 우선, 없으면 24h 가정
    expires_in = int(data.get("expires_in", 86400))
    _write_cache(s, token, time.time() + expires_in)
    log_event("token", env=s.env, expires_in=expires_in)
    return token
