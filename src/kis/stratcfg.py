"""전략 설정 API — v0/propose가 읽는 단일 설정파일(config/strategy.json)을 검증하며 변경.

madu_bot(OpenClaw)에게는 코드가 아니라 이 명령만 노출한다. 모든 값은 범위 검증.
  show                  현재 설정 출력
  set <key> <value>     budget/dip/target/wall/surge 변경 (검증된 범위 내에서만)
  exclude <code>        해당 종목 매매 제외
  include <code>        제외 해제
  watch <code> <name>   C1 상시 워치에 종목 추가
  unwatch <code>        추가했던 워치 제거
변경은 즉시 파일에 반영되고, 다음 v0/propose 실행부터 적용된다(실행 중인 주문엔 영향 없음).
"""
from __future__ import annotations
import json

from .config import CONFIG_DIR

PATH = CONFIG_DIR / "strategy.json"

DEFAULTS = {"budget": 100000, "dip": 3.0, "target": 1.5, "wall": 1.5,
            "surge": 5.0, "top": 5, "exclude": [], "c1_extra": {}}

# key: (형변환, 최소, 최대) — 실거래 안전 가드레일
BOUNDS = {
    "budget": (int,   10000, 1000000),   # 종목당 1만~100만원
    "dip":    (float, 0.5,   15.0),      # 진입 눌림 0.5~15%
    "target": (float, 0.5,   10.0),      # 목표 익절 0.5~10%
    "wall":   (float, 0.0,   10.0),      # 호가 매수벽 잔량비
    "surge":  (float, 2.0,   30.0),      # 전날 급등 기준 2~30%
    "top":    (int,   1,     30),        # 제안 C2 후보 상위 N개만
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if PATH.exists():
        try:
            cfg.update(json.loads(PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def _save(cfg: dict) -> None:
    PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def set_param(key: str, value) -> dict:
    if key not in BOUNDS:
        raise ValueError(f"변경 불가 항목: {key} (가능: {list(BOUNDS)})")
    cast, lo, hi = BOUNDS[key]
    v = cast(value)
    if not (lo <= v <= hi):
        raise ValueError(f"{key}={v} 범위초과 (허용 {lo}~{hi})")
    cfg = load(); cfg[key] = v; _save(cfg)
    return cfg


def exclude(code: str) -> dict:
    cfg = load()
    if code not in cfg["exclude"]:
        cfg["exclude"].append(code)
    _save(cfg); return cfg


def include(code: str) -> dict:
    cfg = load(); cfg["exclude"] = [x for x in cfg["exclude"] if x != code]
    _save(cfg); return cfg


def watch(code: str, name: str) -> dict:
    cfg = load(); cfg["c1_extra"][code] = name; _save(cfg); return cfg


def unwatch(code: str) -> dict:
    cfg = load(); cfg["c1_extra"].pop(code, None); _save(cfg); return cfg


def summary(cfg: dict | None = None) -> str:
    cfg = cfg or load()
    return (f"⚙️ 전략설정: 예산 {cfg['budget']:,}원/종목 · 눌림 -{cfg['dip']}% · 목표 +{cfg['target']}% "
            f"· 매수벽 ≥{cfg['wall']} · 급등기준 +{cfg['surge']}% · 제안 상위 {cfg.get('top', 5)}개\n"
            f"   제외: {cfg['exclude'] or '없음'} · 추가워치: {cfg['c1_extra'] or '없음'}")
