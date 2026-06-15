# 주문 운영 명세서 — 발주·정정·취소·체결확인 (2026-06-05)

> 진입조건이 **이미 확정된** 건은 stratv0 감시모드(watch-and-trigger) 대신 **지정가를 호가창에 바로 얹는다(resting)**.
> 코드: `~/kis-autotrade`. 실주문은 `--live` + `.env DRY_RUN=false` 둘 다라야 발동. 기본 dry-run.

## 0. 데이터 소스 두 갈래 (중요)
| 목적 | 소스 | 비고 |
|---|---|---|
| **체결내역**(오늘·이번주·기간) | `inquire-daily-ccld` 기간조회 (`orders.executions`) | ✅ 정식·신뢰. 체결되면 반드시 잡힘 |
| **NXT 미체결 대기주문** 추적 | 발주 시 저장한 `data/orders_live_<날짜>.json`(ledger) + `can_buy.ord_psbl_cash`(묶인현금) | ⚠️ NXT 프리/애프터 미체결은 ccld에 **안 뜸** → order_no를 발주시점에 저장해야 정정/취소 가능 |

- 결정적 확인은 **KIS MTS 앱**(주문장부 원본). 코드는 자동·반복·텔레그램 보고용.

## 1. CLI 명령
```
# 발주 — approved.json 종목을 감시없이 지정가로 즉시 발주(+order_no 저장)
python -m src.cli placeapproved [--budget 100000] --env prod [--live]

# 체결현황 — 오늘 발주(ledger) leg별 체결/미체결 (체결내역 기반)
python -m src.cli status --env prod

# 체결내역 — 오늘/이번주/기간
python -m src.cli fills --env prod                      # 오늘
python -m src.cli fills --week --filled --env prod      # 이번주 체결분만
python -m src.cli fills --from 20260601 --to 20260605 --env prod

# 정정 — 미체결 지정가 가격/수량 변경 (정정=새 ODNO 발급, ledger 자동갱신)
python -m src.cli modify <order_no> <org_no> --price 26000 [--qty N] --exch SOR --env prod [--live]

# 취소
python -m src.cli cancel <order_no> <org_no> [--qty 0] --env prod [--live]

# 목표가 매도 — 체결된 leg에 +target 지정가 매도(보유수량 한도)
python -m src.cli placetargets --env prod [--live]
```

## 2. 정정(modify) 사양
- 엔드포인트/TR: `order-rvsecncl` / `TTTC0803U` (취소와 동일). 구분만 `RVSE_CNCL_DVSN_CD`: `01`=정정, `02`=취소.
- `--price` 새 지정가(호가단위 자동 반올림). `--qty 0`=잔량전체 정정(`QTY_ALL_ORD_YN=Y`), `>0`=해당 수량.
- **정정 시 새 order_no(ODNO)가 발급**된다 → ledger의 order_no/price를 자동 갱신(`place.update_on_modify`).
- ⚠️ **매수가를 현재가 위로 올리면 즉시 체결**(정정=신규주문 성격). 더 싸게 대기하려면 현재가 아래로.

## 3. 세션·거래소 라우팅 (`place.session_routing`)
| 세션 | KST | 라우팅(EXCG_ID_DVSN_CD) | 종목 |
|---|---|---|---|
| nxt_pre | 08:00~08:50 | **SOR** | 일반종목(NXT상장). ETF/레버리지 불가 |
| kr_reg | 09:00~15:30 | (default=KRX) | 전종목 |
| nxt_after | 16:00~20:00 | **SOR** | 일반종목 |
| closed | 그 외 | — | 발주 스킵 |
- ETF 코드(`360750` 등)는 NXT 세션이면 자동 스킵.

## 4. 안전장치 (기존 유지)
- 기본 dry-run, 라이브 게이트(`--live` + `DRY_RUN=false`).
- 1회 주문한도 `MAX_ORDER_AMOUNT=100,000`원(strict `>`), 총노출 `MAX_TOTAL_EXPOSURE=300,000`원, 일일횟수 한도, STOP 킬스위치, 호가단위 자동 라운딩, 전 주문 JSONL 로깅.
- 정정도 `check_order`(한도/킬스위치) 통과해야 전송.

## 5. ledger 스키마 `data/orders_live_<날짜>.json`
```json
[{ "ts":"...", "leg":"175330:0", "code":"175330", "name":"JB금융지주",
   "side":"buy", "qty":4, "price":25000, "target":26500,
   "exchange":"SOR", "session":"nxt_pre",
   "order_no":"0001234400", "org_no":"91253",
   "dry_run":false, "ok":true, "modified_ts":"..." }]
```
- dry-run은 ledger에 안 남김(실주문만 추적).

## 6. 2026-06-05 라이브 테스트 결과
- JB금융지주(175330) 2 leg 실발주: 4주@25,000 + 3주@25,250(SOR/NXT프리), 합 175,750원 → can_buy 가용현금 1,600,138→1,423,664로 정확히 묶임 확인.
- `modify` 검증: leg2 25,250→24,900(↓)→25,250 정정 왕복 성공, 새 ODNO 발급·ledger 자동갱신·묶인현금 차액(±1,050) 일치.
- `fills --week` 06-04 체결 4건 정상 조회. `status` leg별 미체결(대기) 정상.
- **NXT 미체결은 daily-ccld 전 변형(EXCG 01/02/03, CCLD 01)에서 모두 n=0** → ledger 저장 방식 확정 근거.

## 7. 미해결/후속
- `placetargets`는 체결 후 검증 필요(오늘 미체결이라 미검증).
- NXT 미체결 실시간 시세/잔량은 별 엔드포인트(`NX` 시세) 또는 WebSocket 검토.
- openclaw cron 연계: `status`/`fills`를 정해진 시각에 돌려 텔레그램 보고.
