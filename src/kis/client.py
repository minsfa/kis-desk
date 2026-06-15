"""REST 공통 클라이언트 — 인증 헤더, rate limit, hashkey.

모든 KIS 호출은 이 클라이언트를 거친다. tr_id/tr_cont 는 호출부에서 지정.
"""
from __future__ import annotations
import threading
import time

import requests

from .config import Settings
from .auth import get_access_token
from .logging_util import log_event


class KisClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._token = get_access_token(settings)
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._min_interval = 1.0 / max(1, settings.rate_per_sec)

    # ---- rate limit (보수적: 환경 초당 한도 기준 최소 간격) ----
    def _throttle(self):
        with self._lock:
            wait = self._min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()

    def _headers(self, tr_id: str, tr_cont: str = "", extra: dict | None = None) -> dict:
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._token}",
            "appkey": self.s.app_key,
            "appsecret": self.s.app_secret,
            "tr_id": tr_id,
            "tr_cont": tr_cont,
            "custtype": "P",  # 개인
        }
        if extra:
            h.update(extra)
        return h

    def hashkey(self, body: dict) -> str:
        """주문 body 서명용 hashkey 발급."""
        self._throttle()
        url = f"{self.s.host}/uapi/hashkey"
        h = {
            "content-type": "application/json; charset=utf-8",
            "appkey": self.s.app_key,
            "appsecret": self.s.app_secret,
        }
        r = requests.post(url, headers=h, json=body, timeout=10)
        r.raise_for_status()
        return r.json()["HASH"]

    def get(self, path: str, tr_id: str, params: dict, tr_cont: str = "") -> dict:
        self._throttle()
        url = f"{self.s.host}{path}"
        r = requests.get(url, headers=self._headers(tr_id, tr_cont), params=params, timeout=10)
        return self._handle(r, op=f"GET {path}", tr_id=tr_id)

    def post(self, path: str, tr_id: str, body: dict, use_hashkey: bool = False) -> dict:
        self._throttle()
        url = f"{self.s.host}{path}"
        extra = {"hashkey": self.hashkey(body)} if use_hashkey else None
        r = requests.post(url, headers=self._headers(tr_id, extra=extra), json=body, timeout=10)
        return self._handle(r, op=f"POST {path}", tr_id=tr_id)

    def _handle(self, r: requests.Response, op: str, tr_id: str) -> dict:
        try:
            data = r.json()
        except Exception:
            log_event("error", op=op, tr_id=tr_id, status=r.status_code, body=r.text[:300])
            r.raise_for_status()
            raise
        # KIS 규약: rt_cd "0" 이 정상
        if str(data.get("rt_cd", "0")) != "0":
            log_event("error", op=op, tr_id=tr_id, rt_cd=data.get("rt_cd"),
                      msg=data.get("msg1"), msg_cd=data.get("msg_cd"))
        return data
