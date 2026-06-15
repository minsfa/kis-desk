# 지주사 저평가 딥리서치 (Phase A~D)

LLM이 작성한 종목 리포트("지주사 저PBR 저평가") 주장을 **KIS·DART 실측으로 재검증**하는 파이프라인.
핵심 교훈: **장부 PBR은 지주사에 안 맞는다** — 자회사 지분을 취득원가로 들고 있어 자회사 주가가
폭등해도 PBR은 안 따라간다(SK스퀘어 PBR 6.74인데 NAV로는 43% 할인). 그래서 NAV가 본 척도.

## 명령어 (전부 read-only, `--env prod` 권장: 모의(vts)는 펀더 필드가 빈약)

| 단계 | 명령 | 산출 |
|---|---|---|
| A | `python -m src.cli holdco --env prod` | PBR/PER/시총 오름차순 표 + `data/holdco/fundamentals_<날짜>.csv` |
| A | `python -m src.cli fundamentals <code> --env prod` | 단일 종목 펀더멘털 |
| B | `python -m src.cli navseed --env prod` | DART 타법인출자현황 → 지분율 맵 `data/holdco/stakes.json` (분기 1회 갱신) |
| B | `python -m src.cli nav --env prod` | NAV 할인율 + 판정 표 + `nav_<날짜>.csv` |
| B | `python -m src.cli navdetail <code> --env prod` | 자회사별 지분가치 분해 |
| C | `python -m src.cli overlay --env prod` | 외국인 30일 순매수 + 자사주/분할 공시 |
| E | `python -m src.cli quality --env prod` | 사업 질·재무 표(ROE/부채비율/차입의존/매출·영익 증가율) |
| D | `python -m src.cli scorecard --env prod` | 종합 점수 랭킹(+가치함정 🪤) + `scorecard_<날짜>.csv` (텔레그램용) |

## 모듈
- `fundamentals.py` — inquire-price(FHKST01010100) 풀파싱(PBR/PER/EPS/BPS/시총/상장주식수/52주위치). `HOLDCO_BASKET` 유니버스.
- `nav.py` — NAV = Σ(자회사 시총 × 지분율). 할인율 = 1 − 시총/NAV. `stakes.json` + `stakes_manual.json`(수동 보강) 병합.
- `overlay.py` — 외국인 순매수 전환/강도 + DART 트리거 공시(자사주/공개매수/분할/합병).
- `quality.py` — 사업 질·재무(연결): 재무비율(FHKST66430300) ROE·부채비율·매출/영익 증가율 + 안정성비율(FHKST66430600) 차입금의존도. `quality_points`(감점)·`is_value_trap`(싸다+현재부실).
- `scorecard.py` — A+B+C+E 가중 합산. 가중치는 파일 상단 상수로 튜닝.

## 가치 함정(value trap) 필터 — Phase E (핵심)
NAV/PBR은 "자산이 싸다"만 본다. 본업이 사양·적자·고부채면 그 싼 게 함정. `quality.py`가 별도 축으로 보강:
- **감점**: 적자(ROE<0) −10 / 저ROE(<5) −4 / 영익감소 −6 / 고차입(의존도>40%) −6 / 차입(30~40%) −3. 적자이력 −5는 ROE<8(미회복)일 때만.
- **🪤 플래그**: 싸다(NAV할인≥40% 또는 PBR≤0.5) **그리고** 현재 부실(ROE<5 또는 회복안된 감익 또는 차입의존>40%). 과거 1회 적자이력만으론 함정 아님(2024 일시손실 노이즈 방지).
- **⚠️ 연결 기준 주의**: KIS 재무는 그룹 연결이라 지주사 자체가 아니라 자회사 부실/부채 합산. **NAV에서 이 부채를 빼면 자회사 시총과 이중계산** → 빼지 않고 리스크 축으로만 사용(이게 'NAV 순부채 차감'을 안 하는 이유).

2026-06-16 효과: 세아홀딩스 67.3→58.3 🪤(ROE 3.1·자산만 쌈). 반대로 효성(ROE14.5)·SK(ROE9.3)·SK스퀘어(ROE55)·아이디스홀딩스(ROE12.4)는 🪤 해제. 질 통과한 진짜 저평가=농심홀딩스·SK·아이디스홀딩스·효성·HD현대.

## 점수 설계 (scorecard.py 상단 상수)
`점수 = 가치(NAV할인×60 + PBR≤0.5동조 +8) + 수급(전환+12/순매수+6/지속매도-10/대량매도-18) + 공시(자사주소각15·공개매수12·자사주취득8·인적분할8, 상한20)`
- **+할인율은 신뢰**(비상장 자회사 누락 시 NAV 과소 → 할인율은 보수적 하한).
- **-프리미엄은 coverage로 구분**: cov≥60%면 진짜 비쌈(감점), cov<60%면 NAV 과소 아티팩트(0점, 판단보류).

## 알려진 한계 (해석 시 필수)
1. **NAV = 상장 자회사 지분만**. 비상장 자회사·자체사업·순현금·순부채 미반영 → 할인율은 하한.
2. **법인명 매칭**: DART는 한글음차("씨제이제일제당"), corpCode는 로마자("CJ제일제당"). `dart._ALIAS_TOKENS`로
   그룹 토큰 통일 + 각주/보통주 꼬리표 제거로 대부분 복구하나, CJ ENM 등 일부 잔존. `coverage`·`n_unmatched`로 노출.
   못 잡는 핵심 자회사는 `data/holdco/stakes_manual.json`에 `{holdcos:{<code>:{matched:[{code,name,stake_pct}]}}}` 로 수동 보강.
3. **지분율 시점**: `navseed`는 직전 사업연도 기준. `--year`/`--reprt`(annual|h1|q1|q3)로 조정.

## openclaw 연동(선택)
`scorecard`/`overlay`의 반환 텍스트를 cron이 마감 후(예: 15:40 KST) 호출해 텔레그램 보고 가능
(`propose.py` 15:35 패턴과 동일). `navseed`는 분기 1회만.
