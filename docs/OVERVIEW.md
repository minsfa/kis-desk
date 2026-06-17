# kis-desk 전체 개요 (OVERVIEW)

**KIS 투자 코파일럿** — 텔레그램(OpenClaw/madu_bot)에서 묻고·답받고·필요시 매매.
🔬 리서치(무엇을 살까) + ⚙️ 실행(안전하게 수행) + 💬 창구(OpenClaw). 리서치는 **가치·성장·레짐 3렌즈**.

> 약 40개 모듈 / ~4,900줄 / 50+ CLI 명령 / 8 openclaw cron. 코드 `~/kis-autotrade`, 리모트 github.com/minsfa/kis-desk.

---

## 1. 계층 구조 (모듈)

| 계층 | 모듈 | 역할 |
|---|---|---|
| 🧱 인프라 | `config·auth·client·safety·logging_util·tick` | 환경(vts/prod)·토큰캐시·REST+레이트리밋+hashkey·안전게이트·JSONL로깅·호가단위 |
| 📈 시세/주문 코어 | `market·orders·overseas·place` | 현재가·호가·잔고 / 국내·미국 주문·취소·정정·체결추적·목표매도 |
| 📥 데이터 수집 | `daily·investor·minbars·pricelog(_par)·auctionmon` | 일봉5년·투자자순매수누적·분봉·실시간로깅·동시호가 |
| 🔬🟦 가치 렌즈 | `fundamentals·nav·overlay·quality·scorecard·dart` | PBR/시총·NAV할인·수급/자사주공시·ROE/재무·종합스코어·DART공시 |
| 🔬🟧 성장 렌즈 | `growth` | 매출CAGR·영업이익률추세·PSR·PEG·15%CAGR허들 |
| 🔬🌍 레짐 렌즈 | `regime` | KR+US ETF 상대강도 로테이션 |
| 🔬 단일진단 | `diagnose·news·pattern` | 시세+공시+뉴스+급등눌림통계 조립 |
| 👁️ 모니터링 | `portfolio·report` | 보유 점검(읽기전용)·당일 활동요약 |
| ⚙️ KR 자동매매 | `propose·approve·strat_v0·stratcfg·papertest·screener` | 후보제안→승인게이트→전략실행 |
| ⚙️ 실행/PoC | `poc·krpoc·poc_all` | 미국/국내/통합 배관 PoC |
| ⚙️ US H1 하버스 | `us_stab` | 미국 기계적 왕복 무인 안정성 테스트 |

## 2. 명령어 — 작동 시 무슨 일이 일어나나

### 🔬 리서치 (읽기전용, 돈 안 나감)
- `holdco` / `nav [--wide] [--top N]` / `navdetail <c>` — 지주사 PBR·시총 / NAV 할인율(시총 vs 상장지분 시가) / 자회사별 분해
- `overlay` / `quality` / `scorecard [--wide]` — 외국인수급+자사주공시 / ROE·부채·성장 / 종합점수(🪤 가치함정 판정)
- `growth <c>` — 성장 대비 밸류 + 5년 15%CAGR 허들 그리드(멀티플 컴프레션 민감도)
- `regime` — KR+US ETF로 에너지vs테크·중국본토vs홍콩·미국vs비미국 자금 로테이션
- `diagnose/dart/news/fundamentals <c>` — 단일 종목 진단
- `portfolio` — 보유 종목 자동 점검 + 들고가/비중축소 제안

### ⚙️ 실행 (돈 나감 — DRY_RUN=false + --live 필요)
- KR: `propose`(후보 랭킹) → `approve add`(승인=장전) → `stratv0 --live`(승인분 자동 브래킷) · `buy/sell/cancel/modify`(수동) · `placeapproved/placetargets/status/fills`
- US: `usbuy/ussell/uscancel`(수동) · `usstab buy|sell --live`(H1 무인 왕복) · `usstab report`(안정성 집계)
- 설정/조회: `stratcfg`(전략 파라미터) · `balance/usbalance/orders/usorders/canbuy`

## 3. 관점 (철학)

- **리서치가 결정, 실행이 수행, OpenClaw가 창구.** 매매는 테슬라 오토파일럿식 — *사람이 승인해야 작동*, 봇 자율주문 금지.
- **3렌즈를 종목 성격에 맞게:**
  - 🟦 **가치** — "싼가?": 장부 PBR이 아니라 **NAV(상장지분 시가)** 가 척도. 단 *싼 데 본업부실 = 가치함정* → ROE/재무로 거른다.
  - 🟧 **성장** — "더 클까?": 절대 밸류 아닌 *성장 대비*(PEG·15%CAGR 허들·멀티플 컴프레션).
  - 🌍 **레짐** — "자금이 어느 갈림길로?": ETF 상대강도 로테이션(오건영 '패스파인더' 프레임 참고).
- **정성은 흉내내지 않는다.** TAM·해자·라이트의법칙·업황·금리수치는 수치화하지 않고 *사람 딥리서치 / WebSearch 스냅샷*으로. 거짓 정밀 금지.
- **브로커 = ground truth, 멱등성.** 로컬상태가 아니라 잔고 조회로 행동 결정(US H1 reconciliation).
- **무환각 발굴.** 종목코드 수기입력 대신 DART/KIS로 검증(지주사 출자현황, 중국ETF 이름검증).

## 4. 안전 · 자동화

**안전 다층 (defense in depth):**
`DRY_RUN`(기본 dry) · 한도(`MAX_ORDER_AMOUNT`/`MAX_ORDER_USD`/`MAX_DAILY_TRADES`/`MAX_TOTAL_EXPOSURE`) · `STOP` 파일(킬스위치) · 멱등성(잔고체크) · circuit breaker(연속실패→중단) · 일일손실한도(US H1).
**즉시 중단**: `touch ~/kis-autotrade/STOP` / `openclaw cron disable <id>` / `.env DRY_RUN=true`.

**가동 cron (openclaw, 텔레그램 보고):**
| cron | 시각(KST) | 작동 |
|---|---|---|
| kis-propose-krx | 15:35 월~금 | KR 내일 후보 생성 |
| kis-stratv0 / -sum | 08:01 / 15:25 | KR 승인분 자동매매 / 결과 |
| kis-investor-accum | 16:30 | 수급 누적 |
| kis-propose-final | 20:05 | 최종 정리 |
| kis-usstab-buy / sell / report | 22:35 / 04:50 / 05:10 | US H1 무인 왕복 + 안정성 리포트 |

> ⚠️ cron 시각은 미국 EDT 기준 — 11월 EST 전환 시 usstab buy 23:35 / sell 05:50 조정 필요.

## 5. 데이터 흐름

```
KIS API(시세·잔고·주문) ┐
DART(공시·지분)         ├→ 리서치 3렌즈(가치/성장/레짐) → scorecard·portfolio·regime
네이버뉴스·WebSearch    ┘                                      ↓ (사람 판단)
                          승인(approve) → 실행(strat_v0/usstab) → 로그·지표CSV → 텔레그램(openclaw)
```

## 6. 관련 문서
- 리서치: `HOLDCO_RESEARCH.md`(지주사 A~E) · `METRICS_FRAMEWORK.md`(가치🟦+성장🟧 지표사전)
- 연동: `OPENCLAW_ASSISTANT.md`(대화형 플레이북) · 계획 `PLAN.md` · API `kis-api-notes.md` · 가설 `HYPOTHESES.md`
- 상태: `STATUS_2026-06-18.md`

## 7. 현재 상태 (2026-06)
- main 단일 통합(Public). 🔬 리서치 3렌즈 가동. ⚙️ US H1 무인 cron 가동. KR 자동매매는 배관 검증 완료·운영 휴면(승인 대기).
- 계좌전략: 자동매매=US(격리·소액) / 모니터링=메인계좌(읽기전용).
