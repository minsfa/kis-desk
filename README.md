# kis-desk

한국투자증권(KIS) OpenAPI 기반 **개인 투자 코파일럿** — 텔레그램(OpenClaw/madu_bot)에서 묻고·답받고·필요시 매매.
*(로컬 폴더명은 `kis-autotrade`로 유지 — OpenClaw cron/플레이북 경로 호환)*

## 두 개의 엔진 + 창구

| 구분 | 역할 | 모듈 |
|---|---|---|
| 🔬 **리서치 엔진** | "무엇을 살까"를 데이터로 답 | `fundamentals · nav · overlay · quality · scorecard · dart · news · diagnose · pattern` |
| ⚙️ **실행 엔진** | "사라"를 안전하게 수행 | `orders · place · safety · strat_v0 · propose · approve · overseas · stratcfg` |
| 🧱 공통 기반 | 인증·환경·시세·수집 | `client · config · auth · market · daily · investor · minbars` |
| 💬 창구 | 텔레그램 인터페이스 | OpenClaw(madu_bot) |

## 리서치: 지주사 저평가 딥리서치 (Phase A~E)
LLM 종목 리포트 주장을 KIS·DART **실측으로 재검증**. 자세히: [docs/HOLDCO_RESEARCH.md](./docs/HOLDCO_RESEARCH.md)
- `holdco` PBR/시총 · `nav[--wide]` NAV 할인율 · `overlay` 수급/공시 · `quality` 사업질 · `scorecard[--wide]` 종합(🪤 가치함정 필터)
- 핵심: 장부 PBR은 지주사에 안 맞음 → **NAV(상장지분 시가)** 가 척도. 단 NAV만 싸면 가치함정 → ROE·재무로 옥석.

## 실행: 안전 원칙
- 기본 환경 **모의(vts)**, 실전(prod)은 명시적으로만. `DRY_RUN=true` 기본.
- 모든 주문: 드라이런 → 한도(1회금액·일일횟수·총노출) → `STOP` 킬스위치 → 로깅.
- 자동매매는 **승인 게이트**: `propose`(후보 제안) → 사람이 `approve` → 다음 거래일 자동 주문. 승인 없으면 무거래.

## 빠른 시작
```bash
cp config/.env.example config/.env   # 키 채우기 (git 제외됨)
./.venv/bin/python -m src.cli scorecard --wide --env prod   # 지주사 종합 평가
./.venv/bin/python -m src.cli price 005930 --env prod        # 시세 조회
```

- 📚 API 노트: [docs/kis-api-notes.md](./docs/kis-api-notes.md) · 가설: [docs/HYPOTHESES.md](./docs/HYPOTHESES.md)
- ⚙️ 설정: `config/.env`(키, **git 제외**), `config/strategy.json`(전략 파라미터)

## 상태 (2026-06)
- 실행 엔진: 실거래 배관 검증 완료(국내·해외·NXT). 현재 운영은 휴면(승인 대기).
- 리서치 엔진: 지주사 딥리서치 Phase A~E 가동, 120개 지주사 와이드 평가 가능.
