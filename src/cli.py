"""KIS 자동매매 CLI — 진입점.

조회(안전):
  python -m src.cli token --env prod
  python -m src.cli price 005930 --env prod
  python -m src.cli balance --env prod
  python -m src.cli canbuy 122630 --env prod         # 매수가능조회
  python -m src.cli pick --budget 90000 --env prod    # 저가 일반종목 자동선정
  python -m src.cli orders --env prod                 # 당일 주문체결조회
  python -m src.cli report --env prod                 # 당일 요약(openclaw 보고용)

주문(기본 dry-run, --live 라야 실주문 + .env DRY_RUN=false 필요):
  python -m src.cli buy 069500 1 --market --env prod [--live]
  python -m src.cli sell 069500 1 --market --env prod [--live]
  python -m src.cli cancel <order_no> <org_no> --env prod [--live]

기본 환경 vts(모의), 기본 dry-run.
"""
from __future__ import annotations
import argparse
import json
import sys

from .kis.config import load_settings
from .kis.client import KisClient
from .kis import market, orders, screener, report, overseas, poc, krpoc, poc_all, pricelog, pricelog_par, daily, investor, papertest, strat_v0, propose, place, minbars, auctionmon, fundamentals, nav, overlay, scorecard, quality, growth
from .kis.safety import SafetyError


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _show_proposal():
    """최신 proposal 랭킹을 번호와 함께 출력(분석/승인 참고용)."""
    from .kis import propose
    rows = propose.load_today()
    if not rows:
        print("(오늘자 제안 없음 — 'propose' 먼저 실행)")
        return
    print("🏆 제안 랭킹 — 분석 'diagnose <번호>' · 승인 'approve add <번호>':")
    for r in rows:
        sig = (r.get("signals") or "").replace("|", "·")
        print(f"  {r['rank']:>2}. {r['name']}({r['code']}) [{sig}] "
              f"오늘{r['chg']}% → 진입 {r['entry']}({r['qty']}주) 목표 {r['target']} · 점수 {r.get('score','-')} "
              f"{'NXT' if r['nxt'] in ('True', '1') else '정규장만'}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kis", description="KIS 자동매매 CLI (모의/실전)")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env", choices=["vts", "prod"], default=None,
                        help="vts=모의(기본), prod=실전")
    ap.add_argument("--env", choices=["vts", "prod"], default=None, help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("token", parents=[common], help="접근토큰 발급/캐시 확인")
    p_price = sub.add_parser("price", parents=[common], help="현재가 조회")
    p_price.add_argument("code")
    p_dart = sub.add_parser("dart", parents=[common], help="DART 최근 공시 + 호재/악재 라벨")
    p_dart.add_argument("code")
    p_dart.add_argument("--days", type=int, default=7, help="조회 기간(일), 기본 7")
    p_news = sub.add_parser("news", parents=[common], help="네이버 뉴스 검색(종목코드 또는 검색어)")
    p_news.add_argument("query", help="종목코드(6자리) 또는 검색어")
    p_news.add_argument("--n", type=int, default=8, help="기사 수, 기본 8")
    p_diag = sub.add_parser("diagnose", parents=[common], help="종목 특이이슈 진단(시세+통계+공시+뉴스 → 프롬프트 조립)")
    p_diag.add_argument("code")
    p_diag.add_argument("--name", default=None, help="종목명(미지정 시 자동 해석)")
    sub.add_parser("balance", parents=[common], help="잔고 조회")
    p_ob = sub.add_parser("orderbook", parents=[common], help="호가창 매수/매도 잔량(매수벽)")
    p_ob.add_argument("code")
    p_cb = sub.add_parser("canbuy", parents=[common], help="매수가능조회")
    p_cb.add_argument("code")
    p_pick = sub.add_parser("pick", parents=[common], help="저가 일반종목 자동선정")
    p_pick.add_argument("--budget", type=int, default=90000)
    p_pl = sub.add_parser("pricelog", parents=[common], help="주가 로깅 테스트(N종목×간격)")
    p_pl.add_argument("--n", type=int, default=5, help="종목 수(프리셋 상위 N, 최대 10)")
    p_pl.add_argument("--symbols", default=None, help="직접 지정(콤마구분). --n 무시")
    p_pl.add_argument("--interval", type=float, default=1.0, help="라운드 간격(초)")
    p_pl.add_argument("--rounds", type=int, default=5, help="반복 라운드 수")
    p_plp = sub.add_parser("pricelogpar", parents=[common], help="병렬 폴링 레이트리밋 램프 테스트")
    p_plp.add_argument("--counts", default="14,16,18,20,22,25,30", help="종목수 단계(콤마)")
    p_plp.add_argument("--rounds", type=int, default=3, help="단계당 라운드(초당 1)")
    p_plc = sub.add_parser("pricelogcollect", parents=[common], help="N종목 초당 연속수집(지정시각까지)")
    p_plc.add_argument("--n", type=int, default=18, help="종목 수(최대 32)")
    p_plc.add_argument("--until", default="10:00", help="종료 시각 HH:MM (KST)")
    p_dl = sub.add_parser("daily", parents=[common], help="일봉 히스토리 다운로드(가치주 바스켓)")
    p_dl.add_argument("--years", type=float, default=3, help="받을 기간(년)")
    p_dl.add_argument("--codes", default=None, help="code:name 콤마구분(미지정=가치주 바스켓)")
    p_hd = sub.add_parser("holdco", parents=[common],
                          help="지주사 펀더멘털 실측 스냅샷(PBR/시총/PER 오름차순 표 + CSV)")
    p_hd.add_argument("--lowpbr", type=float, default=0.5, help="저PBR 강조 기준(기본 0.5)")
    p_hd.add_argument("--codes", default=None, help="code:name 콤마구분(미지정=지주사 바스켓)")
    p_fund = sub.add_parser("fundamentals", parents=[common], help="단일 종목 펀더멘털(PBR/PER/EPS/BPS/시총)")
    p_fund.add_argument("code")
    p_ns = sub.add_parser("navseed", parents=[common],
                          help="DART 타법인출자현황 → 지주사 지분율 맵 생성(stakes.json)")
    p_ns.add_argument("--year", type=int, default=None, help="사업연도(미지정=직전년)")
    p_ns.add_argument("--reprt", default="annual", choices=["annual", "h1", "q1", "q3"])
    p_ns.add_argument("--wide", action="store_true", help="확장 유니버스(자동발굴 홀딩스/지주 ~100+)")
    p_nav = sub.add_parser("nav", parents=[common], help="지주사 NAV 할인율 테이블(시총 vs 상장지분 시가)")
    p_nav.add_argument("--wide", action="store_true", help="확장 유니버스로 평가")
    p_nav.add_argument("--top", type=int, default=None, help="상위 N개만 출력")
    p_nd = sub.add_parser("navdetail", parents=[common], help="단일 지주사 NAV 분해(자회사별 지분가치)")
    p_nd.add_argument("code")
    p_ov = sub.add_parser("overlay", parents=[common],
                          help="지주사 수급(외국인)+공시(자사주/분할) 오버레이")
    p_ov.add_argument("--days", type=int, default=120, help="공시 조회 기간(일), 기본 120")
    p_sc = sub.add_parser("scorecard", parents=[common],
                          help="지주사 종합 스코어카드(NAV+PBR+수급+공시−사업질, openclaw 보고용)")
    p_sc.add_argument("--top", type=int, default=12, help="상위 N개 출력")
    p_sc.add_argument("--wide", action="store_true", help="확장 유니버스(자동발굴 ~120)로 평가")
    sub.add_parser("quality", parents=[common],
                   help="지주사 사업 질·재무(ROE/부채/성장) 표")
    p_gr = sub.add_parser("growth", parents=[common],
                          help="성장 렌즈(매출CAGR/마진추세/PSR/PEG/CAGR허들) — 정량만")
    p_gr.add_argument("code")
    sub.add_parser("investor", parents=[common], help="일별 투자자 순매수(개인/기관/외국인) 다운로드")
    sub.add_parser("investoracc", parents=[common], help="투자자 수급 일일 누적(history 병합)")
    p_pr=sub.add_parser("propose", parents=[common], help="일일 후보 제안(내일 진입/목표가)")
    p_pr.add_argument("--budget",type=int,default=None)   # None=strategy.json 설정 사용
    p_pr.add_argument("--dip",type=float,default=None)
    p_pr.add_argument("--target",type=float,default=None)
    p_pr.add_argument("--surge",type=float,default=None)
    p_pt = sub.add_parser("papertest", parents=[common], help="눌림목 매수 포워드 페이퍼테스트(주문X)")
    p_pt.add_argument("--target", type=float, default=1.0, help="반등 매도 목표(퍼센트)")
    p_pt.add_argument("--until", default="15:20", help="종료 시각 HH:MM")
    p_pt.add_argument("--poll", type=int, default=10, help="폴링 간격(초)")
    p_v0 = sub.add_parser("stratv0", parents=[common], help="전략v0(C1 눌림+매수벽, C2 전날급등→익일눌림)")
    p_v0.add_argument("--budget", type=int, default=None, help="종목당 주문 한도(원), 미지정시 strategy.json")
    p_v0.add_argument("--dip", type=float, default=None)
    p_v0.add_argument("--target", type=float, default=None)
    p_v0.add_argument("--wall", type=float, default=None, help="C1 매수벽 잔량비 기준")
    p_v0.add_argument("--surge", type=float, default=None, help="C2 전날 급등 기준(퍼센트)")
    p_v0.add_argument("--until", default="15:20")
    p_v0.add_argument("--poll", type=int, default=15)
    p_v0.add_argument("--live", action="store_true", help="실거래(단 .env DRY_RUN=false라야 발동)")
    sub.add_parser("orders", parents=[common], help="당일 주문체결조회")
    sub.add_parser("report", parents=[common], help="당일 요약(openclaw 보고용)")
    p_cfg = sub.add_parser("stratcfg", parents=[common],
                           help="전략설정 조회/변경(show|set|exclude|include|watch|unwatch)")
    p_cfg.add_argument("action", choices=["show", "set", "exclude", "include", "watch", "unwatch"])
    p_cfg.add_argument("a1", nargs="?", help="set: key / exclude·include·watch·unwatch: 종목코드")
    p_cfg.add_argument("a2", nargs="?", help="set: value / watch: 종목명")
    p_ap = sub.add_parser("approve", parents=[common],
                          help="승인목록 관리(show|add <code> [name]|rm <code>|clear) — 승인 종목만 다음날 장전")
    p_ap.add_argument("action", choices=["show", "add", "rm", "clear"])
    p_ap.add_argument("a1", nargs="?", help="add/rm: 종목코드")
    p_ap.add_argument("a2", nargs="?", help="add: 종목명(선택)")
    p_ap.add_argument("--price", type=int, default=None, help="지정 매수단가(원). 여러번 주면 분할매수 트랑쉐 추가")
    p_ap.add_argument("--target", type=int, default=None, help="지정 목표매도가(원, 미지정시 매수가+target%%)")
    p_ap.add_argument("--qty", type=int, default=None, help="해당 트랑쉐 수량(미지정시 예산/단가)")
    p_ap.add_argument("--replace", action="store_true", help="기존 트랑쉐 지우고 교체")

    for name in ("buy", "sell"):
        p = sub.add_parser(name, parents=[common], help=f"{name} 주문 (기본 dry-run)")
        p.add_argument("code")
        p.add_argument("qty", type=int)
        p.add_argument("--price", type=int, default=0, help="지정가(미지정/0=시장가)")
        p.add_argument("--market", action="store_true", help="시장가")
        p.add_argument("--live", action="store_true", help="실주문(미지정 시 dry-run)")

    p_can = sub.add_parser("cancel", parents=[common], help="미체결 주문 취소")
    p_can.add_argument("order_no")
    p_can.add_argument("org_no")
    p_can.add_argument("--qty", type=int, default=0, help="0=전량")
    p_can.add_argument("--live", action="store_true")

    p_mod = sub.add_parser("modify", parents=[common], help="미체결 지정가 정정(가격/수량 변경)")
    p_mod.add_argument("order_no")
    p_mod.add_argument("org_no")
    p_mod.add_argument("--price", type=int, default=0, help="새 지정가(원)")
    p_mod.add_argument("--qty", type=int, default=0, help="정정 수량(0=잔량전체)")
    p_mod.add_argument("--exch", default=None, help="거래소 라우팅 SOR/KRX/NXT(NXT프리·애프터=SOR)")
    p_mod.add_argument("--live", action="store_true")

    p_pa = sub.add_parser("placeapproved", parents=[common],
                          help="approved.json 종목을 감시없이 지정가로 즉시 발주(order_no 저장)")
    p_pa.add_argument("--budget", type=int, default=100000, help="수량 미지정 leg 의 종목당 예산(원)")
    p_pa.add_argument("--live", action="store_true", help="실주문(미지정 시 dry-run)")

    p_st = sub.add_parser("status", parents=[common],
                          help="오늘 발주(ledger) leg별 체결/미체결 현황(체결내역 기반)")

    p_fl = sub.add_parser("fills", parents=[common], help="체결내역 조회(오늘/이번주/기간)")
    p_fl.add_argument("--from", dest="d_from", default=None, help="시작일 YYYYMMDD(미지정=오늘)")
    p_fl.add_argument("--to", dest="d_to", default=None, help="종료일 YYYYMMDD")
    p_fl.add_argument("--week", action="store_true", help="이번주(월~오늘)")
    p_fl.add_argument("--filled", action="store_true", help="체결분만")

    p_ptg = sub.add_parser("placetargets", parents=[common],
                           help="체결된 leg에 +목표가 매도 지정가 발주(보유수량 한도)")
    p_ptg.add_argument("--live", action="store_true")

    p_mb = sub.add_parser("minbars", parents=[common],
                          help="하루치 1분봉 OHLCV 수집·누적(일중 패턴 검증용, 기본 005930/000660/243880)")
    p_mb.add_argument("--codes", default=None, help="쉼표구분 종목코드(미지정=감시군)")
    p_mb.add_argument("--date", default=None, help="수집일 YYYYMMDD(미지정=오늘)")
    p_mb.add_argument("--backfill", type=int, default=0, help="과거 N일 백필(평일)")

    p_am = sub.add_parser("auctionmon", parents=[common],
                          help="동시호가/프리마켓 예상체결가+호가잔량 N초 모니터")
    p_am.add_argument("code")
    p_am.add_argument("--mkt", default="J", help="J(KRX)/UN(프리마켓 NXT통합)")
    p_am.add_argument("--sec", type=int, default=10, help="폴링 간격(초)")
    p_am.add_argument("--until", default="09:00", help="종료시각 HH:MM")

    # ---- 해외(미국) ----
    p_usp = sub.add_parser("usprice", parents=[common], help="미국 현재가")
    p_usp.add_argument("symbol")
    p_usp.add_argument("--excg", default="NASD", help="NASD/NYSE/AMEX")
    sub.add_parser("usbalance", parents=[common], help="해외 잔고")
    p_uso = sub.add_parser("usorders", parents=[common], help="미국 당일 주문체결내역")
    p_uso.add_argument("--excg", default="NASD")
    p_usc = sub.add_parser("uscancel", parents=[common], help="미국 미체결 주문 취소")
    p_usc.add_argument("symbol")
    p_usc.add_argument("order_no")
    p_usc.add_argument("qty", type=int)
    p_usc.add_argument("--excg", default="NASD")
    p_usc.add_argument("--live", action="store_true")
    p_poc = sub.add_parser("uspoc", parents=[common],
                           help="미국 PoC 단일 실행(SCHD 1주, OpenClaw용)")
    p_poc.add_argument("--live", action="store_true", help="실거래(단 .env DRY_RUN=false라야 발동)")
    p_kr = sub.add_parser("krpoc", parents=[common],
                          help="국내 PoC 단일 실행(ETF+주식 각 1주, OpenClaw용)")
    p_kr.add_argument("--live", action="store_true", help="실거래(단 .env DRY_RUN=false라야 발동)")
    p_all = sub.add_parser("pocall", parents=[common],
                           help="통합 PoC — 시간대 자동감지(NXT/국내메인/미국)")
    p_all.add_argument("--session", choices=["nxt_pre", "kr_main", "nxt_after", "us_reg"],
                       default=None, help="세션 강제 지정(미지정=현재시각 자동감지)")
    p_all.add_argument("--live", action="store_true", help="실거래(단 .env DRY_RUN=false라야 발동)")
    p_uspick = sub.add_parser("uspick", parents=[common], help="저가 일반 미국ETF 자동선정")
    p_uspick.add_argument("--budget", type=float, default=320.0)
    p_uscb = sub.add_parser("uscanbuy", parents=[common], help="미국 매수가능금액")
    p_uscb.add_argument("symbol")
    p_uscb.add_argument("price", type=float)
    p_uscb.add_argument("--excg", default="NASD")
    for name in ("usbuy", "ussell"):
        p = sub.add_parser(name, parents=[common], help=f"미국 {name[2:]} 주문(지정가, 기본 dry-run)")
        p.add_argument("symbol")
        p.add_argument("qty", type=int)
        p.add_argument("price", type=float, help="지정가(USD)")
        p.add_argument("--excg", default="NASD", help="NASD/NYSE/AMEX")
        p.add_argument("--live", action="store_true")

    args = ap.parse_args(argv)

    # report 는 로그만 읽으므로 키 없이도 동작
    if args.cmd == "report":
        print(report.summarize_today())
        return 0

    try:
        s = load_settings(args.env)
    except (RuntimeError, ValueError) as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    if args.cmd == "token":
        from .kis.auth import get_access_token
        tok = get_access_token(s)
        _print({"env": s.env, "token_prefix": tok[:12] + "...", "host": s.host})
        return 0

    if args.cmd == "stratcfg":          # 네트워크 불필요 — 설정 변경/조회 전용
        from .kis import stratcfg
        try:
            if args.action == "show":
                pass
            elif args.action == "set":
                stratcfg.set_param(args.a1, args.a2)
            elif args.action == "exclude":
                stratcfg.exclude(args.a1)
            elif args.action == "include":
                stratcfg.include(args.a1)
            elif args.action == "watch":
                stratcfg.watch(args.a1, args.a2 or args.a1)
            elif args.action == "unwatch":
                stratcfg.unwatch(args.a1)
        except (ValueError, TypeError) as e:
            print(f"[설정 거부] {e}", file=sys.stderr)
            return 2
        print(stratcfg.summary())
        return 0

    if args.cmd == "approve":           # 네트워크 불필요 — 승인목록 관리
        from .kis import approve as _ap
        try:
            if args.action == "add":
                if not args.a1:
                    raise ValueError("종목코드/번호 필요: approve add <코드|번호> [name] [--price P] [--target T] [--qty Q] [--replace]")
                code, name = args.a1, args.a2
                price, target, qty = args.price, args.target, args.qty
                if args.a1.isdigit() and len(args.a1) <= 2:   # 랭킹 번호 → 코드+진입/목표/수량 자동
                    row = propose.pick(int(args.a1))
                    if not row:
                        raise ValueError(f"오늘 제안에 {args.a1}번 없음 — 'propose' 먼저")
                    code = row["code"]; name = name or row["name"]
                    if price is None: price = int(float(row["entry"]))
                    if target is None: target = int(float(row["target"]))
                    if qty is None: qty = int(row["qty"])
                _ap.add(code, name, price=price, target=target, qty=qty, replace=args.replace)
            elif args.action == "rm":
                _ap.remove(args.a1)
            elif args.action == "clear":
                _ap.clear()
            elif args.action == "show":
                _show_proposal()       # 최신 제안 후보(번호) 출력
        except (ValueError, TypeError) as e:
            print(f"[승인 거부] {e}", file=sys.stderr)
            return 2
        print(_ap.summary())
        return 0

    if args.cmd == "dart":              # 네트워크 필요하나 KIS 아닌 DART API — 클라이언트 불필요
        from .kis import dart
        try:
            print(dart.summary(args.code, days=args.days))
        except RuntimeError as e:
            print(f"[DART] {e}", file=sys.stderr)
            return 2
        return 0

    if args.cmd == "navseed":           # DART만 사용 — KIS 클라이언트 불필요
        try:
            _basket = fundamentals.wide_basket() if args.wide else fundamentals.HOLDCO_BASKET
            res = nav.seed_stakes(_basket, year=args.year, reprt=args.reprt)
        except RuntimeError as e:
            print(f"[DART] {e}", file=sys.stderr)
            return 2
        tot_m = sum(len(h["matched"]) for h in res["holdcos"].values())
        tot_u = sum(len(h["unmatched"]) for h in res["holdcos"].values())
        print(f"✅ 지분율 맵 생성: {len(res['holdcos'])}개 지주사 "
              f"(상장매칭 {tot_m} / 미매칭 {tot_u}) — {nav.STAKES}")
        for code, h in res["holdcos"].items():
            subs = ", ".join(f"{m['name']}({m['stake_pct']:.0f}%)" for m in h["matched"][:6])
            print(f"  {h['name']}({code}): {len(h['matched'])}개 상장 → {subs or '(매칭 0)'}")
        return 0

    if args.cmd == "news":              # 네이버 검색 API — KIS 클라이언트 불필요
        from .kis import news
        try:
            code = args.query if args.query.isdigit() and len(args.query) == 6 else None
            print(news.summary(code, name=None if code else args.query, n=args.n))
        except RuntimeError as e:
            print(f"[뉴스] {e}", file=sys.stderr)
            return 2
        return 0

    c = KisClient(s)

    try:
        if args.cmd == "price":
            _print(market.get_price(c, args.code))
        elif args.cmd == "diagnose":
            from .kis import diagnose
            code, name = args.code, args.name
            if args.code.isdigit() and len(args.code) <= 2:   # 랭킹 번호 → 코드 해석
                row = propose.pick(int(args.code))
                if not row:
                    print(f"오늘 제안에 {args.code}번 없음 — 'propose' 먼저", file=sys.stderr)
                    return 2
                code, name = row["code"], name or row["name"]
            print(diagnose.run(c, code, name=name))
        elif args.cmd == "balance":
            _print(market.get_balance(c))
        elif args.cmd == "orderbook":
            _print(market.get_orderbook(c, args.code))
        elif args.cmd == "canbuy":
            _print(orders.can_buy(c, args.code))
        elif args.cmd == "pick":
            _print(screener.pick(c, args.budget))
        elif args.cmd == "pricelog":
            syms = ([x.strip() for x in args.symbols.split(",")]
                    if args.symbols else pricelog.symbols_for(args.n))
            print(pricelog.run(c, syms, args.interval, args.rounds))
        elif args.cmd == "pricelogpar":
            counts = [int(x) for x in args.counts.split(",")]
            print(pricelog_par.run(c, counts, args.rounds))
        elif args.cmd == "pricelogcollect":
            print(pricelog_par.collect(c, args.n, args.until))
        elif args.cmd == "daily":
            codes = daily.VALUE_BASKET
            if args.codes:
                codes = {}
                for part in args.codes.split(","):
                    cd, _, nm = part.strip().partition(":")
                    codes[cd] = nm or cd
            print(daily.download_basket(c, codes, args.years))
        elif args.cmd == "holdco":
            basket = fundamentals.HOLDCO_BASKET
            if args.codes:
                basket = {}
                for part in args.codes.split(","):
                    cd, _, nm = part.strip().partition(":")
                    basket[cd] = nm or cd
            print(fundamentals.summary(c, basket, low_pbr=args.lowpbr))
        elif args.cmd == "fundamentals":
            _print(fundamentals.get_fundamentals(c, args.code))
        elif args.cmd == "nav":
            _basket = fundamentals.wide_basket() if args.wide else fundamentals.HOLDCO_BASKET
            print(nav.summary(c, _basket, top=args.top))
        elif args.cmd == "navdetail":
            print(nav.detail(c, args.code))
        elif args.cmd == "overlay":
            print(overlay.summary(c, fundamentals.HOLDCO_BASKET, days=args.days))
        elif args.cmd == "scorecard":
            _basket = fundamentals.wide_basket() if args.wide else fundamentals.HOLDCO_BASKET
            print(scorecard.report(c, _basket, top=args.top))
        elif args.cmd == "quality":
            print(quality.summary(c, fundamentals.HOLDCO_BASKET))
        elif args.cmd == "growth":
            print(growth.summary(c, args.code))
        elif args.cmd == "investor":
            print(investor.download_basket(c, daily.VALUE_BASKET))
        elif args.cmd == "investoracc":
            print(investor.accumulate(c, daily.VALUE_BASKET))
        elif args.cmd == "propose":
            print(propose.run(c, budget=args.budget, dip=args.dip, target=args.target, surge=args.surge))
        elif args.cmd == "papertest":
            print(papertest.run(c, papertest.WATCH, target=args.target,
                                until=args.until, poll=args.poll))
        elif args.cmd == "stratv0":
            print(strat_v0.run(c, budget=args.budget, dip=args.dip, target=args.target,
                               wall=args.wall, surge=args.surge, until=args.until,
                               poll=args.poll, live=args.live))
        elif args.cmd == "orders":
            _print(orders.today_orders(c))
        elif args.cmd in ("buy", "sell"):
            market_order = args.market or args.price == 0
            fn = orders.buy if args.cmd == "buy" else orders.sell
            res = fn(c, args.code, args.qty, price=args.price,
                     market=market_order, live=args.live)
            _print(res)
            if res.get("dry_run"):
                print("\n※ dry-run. 실주문하려면 --live + .env DRY_RUN=false", file=sys.stderr)
        elif args.cmd == "cancel":
            _print(orders.cancel(c, args.order_no, args.org_no, args.qty, live=args.live))
        elif args.cmd == "modify":
            res = orders.modify(c, args.order_no, args.org_no, price=args.price,
                                qty=args.qty, live=args.live, exchange=args.exch)
            _print(res)
            if res.get("ok") and not res.get("dry_run"):
                place.update_on_modify(args.order_no, res.get("order_no_new"),
                                       new_price=args.price or None,
                                       new_qty=args.qty or None)
            if res.get("dry_run"):
                print("\n※ dry-run. 실주문하려면 --live + .env DRY_RUN=false", file=sys.stderr)
        elif args.cmd == "placeapproved":
            res = place.place_approved(c, live=args.live, budget=args.budget)
            _print(res)
            if any(p.get("dry_run") for p in res.get("placed", [])):
                print("\n※ dry-run. 실주문하려면 --live + .env DRY_RUN=false", file=sys.stderr)
        elif args.cmd == "status":
            _print(place.fill_status(c))
        elif args.cmd == "fills":
            d_from, d_to = args.d_from, args.d_to
            if args.week:
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                now = _dt.now(_tz(_td(hours=9)))
                d_from = (now - _td(days=now.weekday())).strftime("%Y%m%d")
                d_to = now.strftime("%Y%m%d")
            _print(orders.executions(c, d_from, d_to, only_filled=args.filled))
        elif args.cmd == "placetargets":
            res = place.place_targets(c, live=args.live)
            _print(res)
            if any(p.get("dry_run") for p in res.get("placed", [])):
                print("\n※ dry-run. 실주문하려면 --live + .env DRY_RUN=false", file=sys.stderr)
        elif args.cmd == "minbars":
            codes = [x.strip() for x in args.codes.split(",")] if args.codes else None
            if args.backfill:
                _print(minbars.backfill(c, codes, args.backfill))
            else:
                _print(minbars.collect(c, codes, args.date))
        elif args.cmd == "auctionmon":
            print(auctionmon.run(c, args.code, mkt=args.mkt, sec=args.sec, until=args.until))
        elif args.cmd == "usprice":
            _print(overseas.get_price(c, args.symbol, args.excg))
        elif args.cmd == "usbalance":
            _print(overseas.get_balance(c))
        elif args.cmd == "usorders":
            _print(overseas.today_orders(c, args.excg))
        elif args.cmd == "uscancel":
            _print(overseas.cancel(c, args.symbol, args.order_no, args.qty,
                                   excg=args.excg, live=args.live))
        elif args.cmd == "uspoc":
            print(poc.run(c, live=args.live))
        elif args.cmd == "krpoc":
            print(krpoc.run(c, live=args.live))
        elif args.cmd == "pocall":
            print(poc_all.run(c, session=args.session, live=args.live))
        elif args.cmd == "uspick":
            _print(overseas.pick(c, args.budget))
        elif args.cmd == "uscanbuy":
            _print(overseas.can_buy(c, args.symbol, args.price, args.excg))
        elif args.cmd in ("usbuy", "ussell"):
            fn = overseas.buy if args.cmd == "usbuy" else overseas.sell
            res = fn(c, args.symbol, args.qty, args.price, excg=args.excg, live=args.live)
            _print(res)
            if res.get("dry_run"):
                print("\n※ dry-run. 실주문하려면 --live + .env DRY_RUN=false", file=sys.stderr)
    except SafetyError as e:
        print(f"[안전 차단] {e}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
