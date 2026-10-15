"""네트워크 없이 파서·점수화·리포트가 맞물리는지 확인한다.  실행: python -m pytest -q"""
import sqlite3
from contextlib import contextmanager
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
    assert h.startswith("10/14~10/15 접수 공시")  # 접수일을 밝힌다

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


# ---------------------------------------------------------------------------
# 발송 단위: '아직 안 보낸 공시'
# ---------------------------------------------------------------------------
@contextmanager
def patched(obj, name, value):
    old = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, old)


def _filing(no, dt, report_nm="임원ㆍ주요주주특정증권등소유상황보고서"):
    return {"rcept_no": no, "corp_code": "00000001", "corp_name": "테스트전자", "stock_code": "000001",
            "flr_nm": "홍길동", "rcept_dt": dt, "report_nm": report_nm}


def _buy(reporter="홍길동", day="2026-10-15", cnt=1000, price=30000):
    return {"corp_code": "00000001", "corp_name": "테스트전자", "stock_code": "000001", "reporter": reporter,
            "reporter_type": "개인(국내)", "position": "대표이사", "relation": "임원(등기임원)", "event_date": day,
            "reason": "장내매수(+)", "kind": "buy", "security_type": "보통주", "before_cnt": 10000,
            "change_cnt": cnt, "after_cnt": 10000 + cnt, "price": price, "amount": cnt * price, "note": None}


def _add(store, filing, trades):
    store.upsert_filings([filing])
    store.replace_trades(filing["rcept_no"], trades)
    store.mark_parsed(filing["rcept_no"], True)


def test_unreported_covers_weekend_and_never_repeats(tmp_path):
    from insider.pipeline import select_unreported

    store = Store(tmp_path / "t.sqlite")
    _add(store, _filing("20261014000001", "20261014"), [_buy("가", "2026-10-14")])
    _add(store, _filing("20261015000001", "20261015"), [_buy("나", "2026-10-15")])

    fri = date(2026, 10, 16)
    filings, trades = select_unreported(store, fri, max_age_days=5)
    assert [f["rcept_no"] for f in filings] == ["20261015000001"]  # 첫 발송: 그제 접수분은 지난 것으로 건너뜀
    store.mark_reported([f["rcept_no"] for f in filings], "2026-10-16T07:10:00+09:00")
    assert select_unreported(store, fri, 5) == ([], [])             # 같은 날 다시 돌아도(재실행) 보낼 게 없음

    # 금요일 낮 접수분 → 월요일 아침에 나간다 (예전 '어제~오늘' 방식은 이걸 놓쳤다)
    _add(store, _filing("20261016000001", "20261016"), [_buy("다", "2026-10-16")])
    filings, trades = select_unreported(store, date(2026, 10, 19), 5)
    assert [f["rcept_no"] for f in filings] == ["20261016000001"]
    assert {t["reporter"] for t in trades} == {"다"}


def test_migration_marks_existing_filings_as_sent(tmp_path):
    db = tmp_path / "old.sqlite"
    c = sqlite3.connect(db)
    c.executescript(
        """CREATE TABLE filings (rcept_no TEXT PRIMARY KEY, corp_code TEXT NOT NULL, corp_name TEXT NOT NULL,
               stock_code TEXT, corp_cls TEXT, flr_nm TEXT, rcept_dt TEXT NOT NULL, report_nm TEXT,
               parsed INTEGER NOT NULL DEFAULT 0, parse_error TEXT, fetched_at TEXT NOT NULL DEFAULT (datetime('now')));
           INSERT INTO filings (rcept_no, corp_code, corp_name, rcept_dt, parsed)
               VALUES ('20261015000001', '00000001', '테스트전자', '20261015', 1);"""
    )
    c.commit()
    c.close()
    store = Store(db)  # 예전 형식 DB → 기존 공시는 이미 보낸 것으로 표시
    assert store.stats()["unreported"] == 0
    _add(store, _filing("20261016000001", "20261016"), [_buy()])
    assert store.stats()["unreported"] == 1


def test_restated_filings_not_double_counted(tmp_path):
    from insider.pipeline import select_unreported

    store = Store(tmp_path / "t.sqlite")
    # 원본과 [기재정정]이 같이 들어오면 거래는 한 번만, 정정본 기준으로
    _add(store, _filing("20261015000001", "20261015"), [_buy(price=30000)])
    _add(store, _filing("20261015000009", "20261015", "[기재정정]임원ㆍ주요주주특정증권등소유상황보고서"), [_buy(price=31000)])
    filings, trades = select_unreported(store, date(2026, 10, 16), 5)
    assert len(filings) == 2 and len(trades) == 1 and trades[0]["price"] == 31000
    store.mark_reported([f["rcept_no"] for f in filings], "t")

    # 이미 보낸 거래를 나중에 또 정정하면: 공시는 새것이지만 거래는 반복이라 뺀다
    _add(store, _filing("20261016000005", "20261016", "[기재정정]임원ㆍ주요주주특정증권등소유상황보고서"), [_buy(price=31500)])
    filings, trades = select_unreported(store, date(2026, 10, 16), 5)
    assert len(filings) == 1 and trades == []


def test_failed_parse_is_retried_within_window(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.upsert_filings([_filing("20261015000001", "20261015"), _filing("20261001000001", "20261001")])
    store.mark_parsed("20261015000001", False, "014 파일이 존재하지 않습니다")
    store.mark_parsed("20261001000001", False, "014 파일이 존재하지 않습니다")
    assert [r["rcept_no"] for r in store.unparsed_filings(retry_since="20261009")] == ["20261015000001"]
    assert store.unparsed_filings() == []  # 백필 경로는 재시도하지 않음 (무한 반복 방지)


class _FakeDart:
    def __init__(self, *a, **k):
        pass

    def iter_insider_filings(self, start, end):
        return iter([_filing("20261015000001", "20261015")])

    def fetch_document_xml(self, rcept_no):
        return FIX.read_text(encoding="utf-8")


def _settings(tmp_path):
    s = Settings()
    s.dart_api_key = "test"
    s.db_path, s.report_dir, s.raw_dir = tmp_path / "db.sqlite", tmp_path / "reports", tmp_path / "raw"
    s.site_dir = None
    s.telegram_bot_token, s.telegram_chat_id, s.telegram_paid_chat_id = "token", "@channel", ""
    s.smtp_host, s.mail_to = "", []
    s.request_sleep = 0
    return s


def test_run_daily_resends_only_after_failure(tmp_path):
    import insider.pipeline as P

    sent, ok = [], {"v": False}

    def fake_send(settings, text, chat_id=None, parse_mode="HTML"):
        sent.append(text)
        return ok["v"]

    s = _settings(tmp_path)
    with patched(P, "DartClient", _FakeDart), patched(P, "today_kst", lambda: date(2026, 10, 16)), \
            patched(P.notify, "send_telegram", fake_send):
        assert P.run_daily(s) == P.EXIT_SEND_FAILED       # 발송 실패 → 빨간 X, '보낸 것' 표시 안 함
        assert Store(s.db_path).stats()["unreported"] == 1
        ok["v"] = True
        assert P.run_daily(s) == P.EXIT_OK                 # 다음 실행이 다시 보냄
        assert P.run_daily(s) == P.EXIT_OK                 # 그다음엔 보낼 게 없음 (중복 없음)
    assert len(sent) == 2 and "10/15 접수" in sent[-1]


def test_run_daily_dry_run_marks_nothing(tmp_path):
    import insider.pipeline as P

    s = _settings(tmp_path)
    with patched(P, "DartClient", _FakeDart), patched(P, "today_kst", lambda: date(2026, 10, 16)), \
            patched(P.notify, "send_telegram", lambda *a, **k: (_ for _ in ()).throw(AssertionError("보내면 안 됨"))):
        assert P.run_daily(s, dry_run=True) == P.EXIT_OK
    assert Store(s.db_path).stats()["unreported"] == 1
    assert (s.report_dir / "2026-10-16.telegram.txt").exists()


def test_run_daily_skips_when_nothing_notable(tmp_path):
    import insider.pipeline as P

    class OnlyNoise(_FakeDart):
        def fetch_document_xml(self, rcept_no):
            return FIX.read_text(encoding="utf-8").replace("장내매수(+)", "주식매수선택권(+)").replace("장내매도(-)", "증여(-)")

    sent = []
    s = _settings(tmp_path)
    with patched(P, "DartClient", OnlyNoise), patched(P, "today_kst", lambda: date(2026, 10, 16)), \
            patched(P.notify, "send_telegram", lambda *a, **k: sent.append(1) or True):
        assert P.run_daily(s) == P.EXIT_OK
    assert sent == [] and Store(s.db_path).stats()["unreported"] == 0
