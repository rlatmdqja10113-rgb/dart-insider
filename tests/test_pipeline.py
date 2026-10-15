"""네트워크 없이 파서·점수화·리포트가 맞물리는지 확인한다.  실행: python -m pytest -q"""
from datetime import date
from pathlib import Path

from insider.config import Settings
from insider.db import Store
from insider.notify import split_telegram
from insider.parser import classify_reason, parse_ownership_report
from insider.pipeline import build_report_data
from insider.report import build_markdown, build_telegram, headline
from insider.scoring import find_clusters, reporter_kind, score_trades, split_by_reporter

FIX = Path(__file__).parent / "fixtures" / "sample_report.xml"
FILING = {
    "rcept_no": "20261014000001", "corp_code": "00000001", "corp_name": "테스트전자",
    "stock_code": "000001", "flr_nm": "홍길동", "rcept_dt": "20261014",
    "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서",
}


def test_classify_reason():
    assert classify_reason("장내매수(+)") == "buy"
    assert classify_reason("장내매도(-)") == "sell"
    assert classify_reason("주식매수선택권행사(+)") == "other"   # '매수'가 들어 있어도 노이즈로
    assert classify_reason("증여(-)") == "other"
    assert classify_reason(None) == "other"


def test_reporter_kind():
    # 개인
    assert reporter_kind("홍길동", "대표이사", "임원(등기임원)") == "person"
    assert reporter_kind("후이메이야", "지배주주", None) == "person"
    assert reporter_kind("Hui Mei Nga", "부동사장", None) == "person"
    # 법인·기관 — GoInsider 실시간 목록에서 본 것들
    assert reporter_kind("미래에셋자산운용(주)", "대주주", None) == "entity"
    assert reporter_kind("국민연금공단", "대주주", None) == "entity"
    assert reporter_kind("엘에스전선 주식회사", "대주주", None) == "entity"
    assert reporter_kind("DB손해보험주식회사", "최대주주", None) == "entity"
    assert reporter_kind("케이헤드조합", "지배주주", None) == "entity"
    assert reporter_kind("J.P. Morgan Securities PLC", "Investment Bank", None) == "entity"
    assert reporter_kind("NMSI, INC.", "주택담보대출업", None) == "entity"
    assert reporter_kind("에스브이아이씨55호 신기술사업투자조합", "대주주", None) == "entity"
    # DART 보고자 구분이 있으면 이름 패턴보다 우선
    assert reporter_kind("김철수", None, None, reporter_type="법인(국내)") == "entity"
    assert reporter_kind("이상한이름주식회사", None, None, reporter_type="개인(국내)") == "person"


def test_parse_fixture():
    trades = parse_ownership_report(FIX.read_text(encoding="utf-8"), FILING)
    assert len(trades) == 4  # 합계 행은 제외
    t0 = trades[0]
    assert t0.reporter == "홍길동"
    assert t0.position == "대표이사"
    assert t0.relation == "임원(등기임원)" and t0.reporter_type == "개인(국내)"
    assert t0.event_date == "2026-10-08"
    assert t0.kind == "buy" and t0.change_cnt == 5000 and t0.price == 31250
    assert t0.amount == 5000 * 31250
    sell = trades[3]
    assert sell.kind == "sell" and sell.change_cnt == -10000  # 부호 보정


def _seed(store: Store):
    """픽스처 임원 1명 + 사외이사 1명 + 자산운용사 1곳 + 모회사 1곳."""
    store.upsert_filings([FILING])
    trades = parse_ownership_report(FIX.read_text(encoding="utf-8"), FILING)
    store.replace_trades(FILING["rcept_no"], [t.as_dict() for t in trades])
    store.mark_parsed(FILING["rcept_no"], True)
    base = trades[0].as_dict()

    f2 = dict(FILING, rcept_no="20261014000002", flr_nm="김철수")
    store.upsert_filings([f2])
    store.replace_trades(f2["rcept_no"], [dict(base, reporter="김철수", position="사외이사", event_date="2026-10-11",
                                               change_cnt=1000, price=31000, amount=31_000_000, before_cnt=0, after_cnt=1000)])
    f3 = dict(FILING, rcept_no="20261014000003", flr_nm="미래에셋자산운용(주)", corp_code="00000002",
              corp_name="테스트리츠", stock_code="000002")
    store.upsert_filings([f3])
    store.replace_trades(f3["rcept_no"], [dict(base, corp_code="00000002", corp_name="테스트리츠", stock_code="000002",
                                               reporter="미래에셋자산운용(주)", reporter_type="법인(국내)", position="대주주", relation="주요주주",
                                               event_date="2026-10-12", change_cnt=200000, price=5000, amount=1_000_000_000)])
    f4 = dict(FILING, rcept_no="20261014000004", flr_nm="엘에스전선 주식회사")
    store.upsert_filings([f4])
    store.replace_trades(f4["rcept_no"], [dict(base, reporter="엘에스전선 주식회사", reporter_type="법인(국내)", position="대주주", relation="최대주주",
                                               event_date="2026-10-12", change_cnt=50000, price=31000, amount=1_550_000_000)])
    for r in (f2, f3, f4):
        store.mark_parsed(r["rcept_no"], True)


def test_scoring_and_split(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    _seed(store)
    rows = store.trades_between("20261014", "20261014")
    history = {c: store.corp_trades_since(c, "2026-07-01") for c in {"00000001", "00000002"}}
    scored = score_trades(rows, history, min_amount=10_000_000)
    persons, entities = split_by_reporter(scored)

    assert {s.trade["reporter"] for s in persons} == {"홍길동", "김철수"}
    assert {s.trade["reporter"] for s in entities} == {"미래에셋자산운용(주)", "엘에스전선 주식회사"}
    # 법인이 금액은 10배 크지만 개인 임원 매수가 위로 온다
    assert scored[0].reporter_kind == "person"
    sells = [s for s in persons if s.trade["kind"] == "sell"]
    assert "옵션행사 후 매도" in sells[0].flags   # 10/12 행사 → 10/13 매도

    # 동반매수는 개인만 센다: 모회사(엘에스전선)가 같이 샀어도 2명(홍길동·김철수)
    clusters = find_clusters(rows, window_days=7)
    assert len(clusters) == 1 and clusters[0].reporters == ["김철수", "홍길동"]


def test_report_outputs(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    _seed(store)
    s = Settings()
    data = build_report_data(s, store, date(2026, 10, 15), lookback_days=1)
    assert len(data["buys"]) == 2 and len(data["sells"]) == 1  # 홍길동 2건이 한 줄로 묶임
    assert data["buys"][0].trade["n_trades"] == 2 and data["buys"][0].trade["amount"] == 5000 * 31250 + 3000 * 30900
    assert len(data["entity_buys"]) == 2 and data["entity_sells"] == []
    assert data["stats"]["noise_excluded"] == 1  # 스톡옵션 행사

    h = headline(data)
    assert "동반매수 테스트전자" in h and "대표이사" in h

    free = build_telegram(data, tier="free")
    full = build_telegram(data, tier="full", top_n=15, site_url="https://example.com")
    assert "<b>" in free and "권유" in free
    assert "유료 채널" in free and "미래에셋" not in free          # 무료: 법인 건수만
    assert "미래에셋자산운용" in full and "전체 보기" in full        # 유료: 법인 목록 + 링크
    assert "8,000주 평균 @" in full and "8,000주" not in free      # 상세는 유료만

    md = build_markdown(data, 15)
    assert "동반 매수" in md and "법인·기관" in md and "권유하지" in md


def test_split_telegram():
    short = "a\n\nb"
    assert split_telegram(short) == [short]
    long = "\n\n".join(f"섹션{i}\n" + "• 줄 " * 300 for i in range(6))
    parts = split_telegram(long)
    assert len(parts) > 1 and all(len(p) <= 4096 for p in parts)
    assert "".join(parts).replace("\n", "") == long.replace("\n", "")  # 내용 유실 없음


def test_build_site(tmp_path):
    from insider.site import build_site

    store = Store(tmp_path / "t.sqlite")
    _seed(store)
    out = build_site(Settings(), store, tmp_path / "site", days=3, lookback_days=400)  # 픽스처 날짜가 과거여도 잡히도록
    index = (out / "index.html").read_text(encoding="utf-8")
    assert "테스트전자" in index and "법인·기관" in index and "권유하지" in index
    assert (out / "feed.json").exists() and (out / ".nojekyll").exists()
