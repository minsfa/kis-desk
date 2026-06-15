"""환경(vts/prod) 스위치 + 설정 로드.

KIS_ENV 또는 CLI --env 로 모의(vts)/실전(prod)을 고른다. 기본은 vts.
키·계좌·토큰캐시·TR ID 접두를 환경별로 완전 분리한다.
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv 없어도 동작 (환경변수 직접 사용)
    def load_dotenv(*a, **k):  # type: ignore
        return False

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
STATE_DIR = PROJECT_ROOT / "data" / "state"
LOG_DIR = PROJECT_ROOT / "logs"
KILL_SWITCH = PROJECT_ROOT / "STOP"  # 이 파일이 있으면 모든 주문 차단

load_dotenv(CONFIG_DIR / ".env")

# 도메인 (REST). 빌드 직전 공식 포털로 재확인.
HOSTS = {
    "vts": "https://openapivts.koreainvestment.com:29443",   # 모의
    "prod": "https://openapi.koreainvestment.com:9443",      # 실전
}
# 호출 한도(초당) — 모의 2 / 실전 20 (조사값, 공식 공지로 재확인). 보수적으로 사용.
RATE_PER_SEC = {"vts": 2, "prod": 20}

# TR ID. ⚠️ 공식 문서 재확인 필요(변경 이력 있음). psbl(매수가능)은 실전서 검증됨(rt_cd=0).
TR = {
    "vts": {"buy": "VTTC0802U", "sell": "VTTC0801U", "balance": "VTTC8434R",
            "cancel": "VTTC0803U", "ccld": "VTTC8001R", "psbl": "VTTC8908R"},
    "prod": {"buy": "TTTC0802U", "sell": "TTTC0801U", "balance": "TTTC8434R",
             "cancel": "TTTC0803U", "ccld": "TTTC8001R", "psbl": "TTTC8908R"},
}
TR_PRICE = "FHKST01010100"  # 주식현재가 (실전/모의 공통)

# 해외(미국) TR ID. price/balance/psamount 는 라이브 검증됨. 주문 tr_id는 주문 시 검증.
TR_US = {
    "vts": {"buy": "VTTT1002U", "sell": "VTTT1001U", "balance": "VTTS3012R",
            "psamount": "VTTS3007R", "ccnl": "VTTS3035R", "cancel": "VTTT1004U"},
    "prod": {"buy": "TTTT1002U", "sell": "TTTT1006U", "balance": "TTTS3012R",
             "psamount": "TTTS3007R", "ccnl": "TTTS3035R", "cancel": "TTTT1004U"},
}
TR_US_PRICE = "HHDFS00000300"
# 거래소: 주문·잔고용 OVRS_EXCG_CD → 현재가용 EXCD 매핑
US_EXCH = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}


def _bool(v: str | None, default: bool) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    env: str                 # "vts" | "prod"
    app_key: str
    app_secret: str
    account: str             # 계좌 앞 8자리 (CANO)
    account_prod: str        # 상품코드 뒤 2자리 (ACNT_PRDT_CD)
    dry_run: bool
    max_order_amount: int
    max_daily_trades: int
    max_order_usd: float
    max_total_exposure: int = 0   # 국내 포트폴리오 총 노출(매입+미체결매수) 한도, 0=무제한

    @property
    def host(self) -> str:
        return HOSTS[self.env]

    def tr_us(self, action: str) -> str:
        return TR_US[self.env][action]

    @property
    def rate_per_sec(self) -> int:
        return RATE_PER_SEC[self.env]

    def tr(self, action: str) -> str:
        return TR[self.env][action]

    def token_cache_path(self) -> Path:
        return STATE_DIR / f"token_{self.env}.json"


def load_settings(env: str | None = None) -> Settings:
    """env 미지정 시 KIS_ENV(기본 vts) 사용."""
    env = (env or os.getenv("KIS_ENV", "vts")).lower()
    if env not in HOSTS:
        raise ValueError(f"KIS_ENV must be 'vts' or 'prod', got: {env!r}")
    pfx = "KIS_VTS_" if env == "vts" else "KIS_PROD_"

    app_key = os.getenv(pfx + "APP_KEY", "")
    app_secret = os.getenv(pfx + "APP_SECRET", "")
    account = os.getenv(pfx + "ACCOUNT", "")
    account_prod = os.getenv(pfx + "ACCOUNT_PROD", "01")

    for name, val in [(pfx + "APP_KEY", app_key), (pfx + "APP_SECRET", app_secret),
                       (pfx + "ACCOUNT", account)]:
        if not val:
            raise RuntimeError(
                f"{name} 미설정. config/.env 를 채우세요 (config/.env.example 참고)."
            )

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    return Settings(
        env=env,
        app_key=app_key,
        app_secret=app_secret,
        account=account,
        account_prod=account_prod,
        dry_run=_bool(os.getenv("DRY_RUN"), True),          # 기본 dry-run
        max_order_amount=int(os.getenv("MAX_ORDER_AMOUNT", "100000")),
        max_daily_trades=int(os.getenv("MAX_DAILY_TRADES", "5")),
        max_order_usd=float(os.getenv("MAX_ORDER_USD", "200")),
        max_total_exposure=int(os.getenv("MAX_TOTAL_EXPOSURE", "0")),
    )
