# OpenClaw 대화형 어시스턴트 연동

kis-desk는 텔레그램(OpenClaw/madu_bot)에서 자연어로 쓰는 것이 최종 인터페이스다.
OpenClaw가 읽는 **실제 플레이북**은 `~/.openclaw/workspace/`에 있고, 이 문서는 그 구조를 버전관리용으로 기록한다.
(플레이북 원본을 고치면 이 문서도 같이 갱신)

## 플레이북 3종 (역할 분리)
| 파일 | 역할 | 성격 |
|---|---|---|
| `KIS_RESEARCH.md` | 리서치 질의응답(저평가/NAV/수급/공시/종목분석) | 읽기전용, 자유롭게 |
| `KIS_STRATEGY_CONFIG.md` | 전략 파라미터 조정 + **승인 게이트**(제안→승인→자동주문) | 설정·승인, 사람 게이트 |
| `KIS_POC.md` | 단발 PoC 실행(미국/국내 배관 점검) | 고정 명령, 사람 승인 |

## 설계 원칙 — "감독형 오토파일럿"
- **리서치 = 내비게이션**: 언제든 물어보면 read-only 명령으로 답(돈 안 나감).
- **매매 = 오토파일럿(테슬라식)**: 사람이 손 올리고(명시 승인) `DRY_RUN=false`여야 작동. OpenClaw는 절대 자율 주문하지 않는다.
- OpenClaw 불변: 종목·숫자 창작 금지(반드시 명령 실행 후 출력만 보고), `config/.env`·`DRY_RUN`·코드 변경 금지, 출력 원문 복붙.

## 리서치 명령 요약 (전부 `--env prod`, read-only)
- `scorecard --wide --top N` — 종합 스코어카드(NAV+PBR+수급+공시−사업질, 🪤 가치함정)
- `nav --wide --top N` / `navdetail <code>` — NAV 할인율 / 자회사 분해
- `holdco` · `quality` · `overlay` — 펀더 표 / 사업질 / 수급·공시
- `fundamentals <code>` · `diagnose <code>` · `dart <code>` · `news <q>` — 단일 종목

상세 매핑(윤기 님 말→명령)은 `~/.openclaw/workspace/KIS_RESEARCH.md` 참조.
