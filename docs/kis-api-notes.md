# KIS OpenAPI 조사 노트 (초안)

> ⚠️ **초안 — Phase 0에서 공식 문서로 검증 필요.** 특히 `tr_id`와 호출 한도는 KIS가 갱신한 적이 있으므로, 코드 작성 전 공식 포털·샘플로 반드시 재확인한다. 돈이 걸린 영역이라 추정으로 넘어가지 않는다.

- 개발자포털: https://apiportal.koreainvestment.com
- 공식 Python 샘플: https://github.com/koreainvestment/open-trading-api

## 도메인 (REST)
| 환경 | Base URL |
|---|---|
| 실전 | `https://openapi.koreainvestment.com:9443` |
| 모의 | `https://openapivts.koreainvestment.com:29443` |

## 실시간 (WebSocket)
| 환경 | URL |
|---|---|
| 실전 | `ws://ops.koreainvestment.com:21000` |
| 모의 | `ws://ops.koreainvestment.com:31000` |
- 접속용 `approval_key`: `POST /oauth2/Approval` (appkey, secretkey)

## 인증 흐름
1. **접근토큰**: `POST /oauth2/tokenP`
   - body: `{ "grant_type":"client_credentials", "appkey":..., "appsecret":... }`
   - 응답 `access_token` 유효 ~24h. **재발급 호출 빈도 제한 있음** → 토큰을 파일 캐시하고 만료 전 재사용.
2. **hashkey** (주문 등 body 서명): `POST /uapi/hashkey`
3. 매 요청 헤더: `authorization: Bearer <token>`, `appkey`, `appsecret`, `tr_id`, `custtype`(개인 P) 등.

## 주요 엔드포인트 (⚠️ tr_id 검증 필요)
| 기능 | Method/Path | tr_id (실전 / 모의) |
|---|---|---|
| 주식 현재가 | GET `/uapi/domestic-stock/v1/quotations/inquire-price` | `FHKST01010100` (공통) |
| 현금주문(매수/매도) | POST `/uapi/domestic-stock/v1/trading/order-cash` | **확인필요** 매수/매도별 상이, 실전 `TTTC****U` / 모의 `VTTC****U` |
| 잔고조회 | GET `/uapi/domestic-stock/v1/trading/inquire-balance` | 실전 `TTTC8434R` / 모의 `VTTC8434R` (확인필요) |
| 주문체결조회 | GET `/uapi/domestic-stock/v1/trading/inquire-daily-ccld` | 확인필요 |

> 매수/매도 `tr_id`는 과거 코드(TTTC0802U/0801U 등)에서 변경 이력이 있으니 **현재 공식 문서 값을 그대로 사용**한다.

## 호출 한도 (대략 — 검증 필요)
- 실전: 초당 다건 허용(예: ~20건/초). 모의: 더 낮음.
- 초과 시 에러/차단 → 호출 간 rate limit + 재시도 백오프 구현.

## 운영시간
- 국내주식 정규장 09:00~15:30 (동시호가/시간외 별도). 장중에만 체결 가능.

## 구현 시 주의
- 모의/실전은 **도메인·키·tr_id 접두(T vs V)** 가 다르므로 환경 스위치로 일괄 처리.
- 주문 수량/금액/시장가·지정가 파라미터 형식, 정정/취소 API는 Phase 2에서 별도 정리.

---
### TODO (Phase 0)
- [ ] 공식 포털에서 order-cash 매수/매도 tr_id 확정
- [ ] 잔고/체결조회 tr_id 확정
- [ ] 호출 한도 정확 수치 확인
- [ ] 모의계좌 발급 후 토큰 발급 실제 테스트
