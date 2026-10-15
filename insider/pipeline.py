"""수집 → 파싱 → 점수화 → 리포트 → 발송 을 잇는 파이프라인."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from .config import Settings, today_kst
from .dart_client import DartClient, DartError
from .db import Store
from .parser import parse_ownership_report
from .report import build_markdown, build_telegram
from .scoring import find_clusters, group_trades, score_trades, split_by_reporter
from . import notify

log = logging.getLogger(__name__)


def collect(settings: Settings, client: DartClient, store: Store, days: int) -> int:
    """최근 days 일간 접수된 B002 공시 목록을 가져와 DB에 넣는다. 새로 들어온 건수를 돌려준다."""
    end = today_kst()
    start = end - timedelta(days=days)
    rows = list(client.iter_insider_filings(start, end))
    inserted = store.upsert_filings(rows)
    log.info("공시 목록 %s건 조회, 신규 %s건", len(rows), inserted)
    return inserted


def _raw_path(settings: Settings, rcept_no: str):
    return settings.raw_dir / f"{rcept_no}.xml"


def _parse_one(settings: Settings, store: Store, filing: dict, xml: str) -> int:
    trades = parse_ownership_report(xml, filing)
    store.replace_trades(filing["rcept_no"], [t.as_dict() for t in trades])
    store.mark_parsed(filing["rcept_no"], True)
    return len(trades)


def parse_pending(settings: Settings, client: DartClient, store: Store, limit: int = 500) -> tuple[int, int]:
    """아직 원문을 안 읽은 공시를 내려받아 거래 행을 추출한다. 원문은 data/raw 에 저장해 둔다.
    (성공, 실패) 건수."""
    ok = fail = 0
    for f in store.unparsed_filings(limit):
        filing = dict(f)
        raw = _raw_path(settings, filing["rcept_no"])
        try:
            if raw.exists():
                xml = raw.read_text(encoding="utf-8")
            else:
                xml = client.fetch_document_xml(filing["rcept_no"])
                raw.write_text(xml, encoding="utf-8")
            n = _parse_one(settings, store, filing, xml)
            ok += 1
            log.debug("%s %s: 거래 %s건", filing["rcept_no"], filing["corp_name"], n)
        except (DartError, Exception) as e:  # noqa: BLE001
            store.mark_parsed(filing["rcept_no"], False, str(e)[:500])
            fail += 1
            log.warning("%s %s 파싱 실패: %s", filing["rcept_no"], filing["corp_name"], e)
    log.info("원문 파싱 성공 %s건, 실패 %s건", ok, fail)
    return ok, fail


def reparse(settings: Settings, store: Store) -> dict:
    """네트워크 없이, 저장된 원문(data/raw)으로 파서를 다시 돌린다. 파서를 고친 뒤 쓴다.
    원문이 없는 공시는 parsed=0 으로 되돌려 다음 run 때 받게 한다."""
    settings.ensure_dirs()
    rows = store.conn.execute("SELECT * FROM filings").fetchall()
    stats = {"reparsed": 0, "trades": 0, "no_raw": 0, "error": 0}
    for f in rows:
        filing = dict(f)
        raw = _raw_path(settings, filing["rcept_no"])
        if not raw.exists():
            store.mark_parsed(filing["rcept_no"], False, None)
            store.conn.execute("UPDATE filings SET parsed=0 WHERE rcept_no=?", (filing["rcept_no"],))
            store.conn.commit()
            stats["no_raw"] += 1
            continue
        try:
            n = _parse_one(settings, store, filing, raw.read_text(encoding="utf-8"))
            stats["reparsed"] += 1
            stats["trades"] += n
        except Exception as e:  # noqa: BLE001
            store.mark_parsed(filing["rcept_no"], False, str(e)[:500])
            stats["error"] += 1
    log.info("재파싱 결과: %s", stats)
    return stats


def build_report_data(settings: Settings, store: Store, report_date: date, lookback_days: int) -> dict:
    """리포트 기준일로부터 lookback_days 일 전까지 접수된 거래를 점수화해 돌려준다.
    마크다운·텔레그램·사이트가 모두 이 결과를 쓴다."""
    start = (report_date - timedelta(days=lookback_days)).strftime("%Y%m%d")
    end = report_date.strftime("%Y%m%d")
    trades = store.trades_between(start, end)

    # 옵션행사 후 매도 판정에 쓸 최근 90일 이력
    since = (report_date - timedelta(days=90)).isoformat()
    history = {c: store.corp_trades_since(c, since) for c in {t["corp_code"] for t in trades}}

    scored = group_trades(score_trades(trades, history, settings.min_amount_krw))
    persons, entities = split_by_reporter(scored)
    clusters = find_clusters(
        [t for t in trades if (t.get("amount") or 0) >= settings.min_amount_krw or not t.get("amount")],
        settings.cluster_window_days,
    )
    return {
        # buys/sells 는 개인 임원·주요주주만. 법인·기관은 entity_* 로 따로.
        "buys": [s for s in persons if s.trade["kind"] == "buy"],
        "sells": [s for s in persons if s.trade["kind"] == "sell"],
        "entity_buys": [s for s in entities if s.trade["kind"] == "buy"],
        "entity_sells": [s for s in entities if s.trade["kind"] == "sell"],
        "clusters": clusters,
        "stats": {
            "filings_in_period": len({t["rcept_no"] for t in trades}),
            "trades_in_period": len(trades),
            "noise_excluded": sum(1 for t in trades if t["kind"] == "other"),
        },
        "period": f"{start[:4]}-{start[4:6]}-{start[6:]} ~ {end[:4]}-{end[4:6]}-{end[6:]}",
        "report_date": report_date,
    }


def build_report(settings: Settings, store: Store, report_date: date, lookback_days: int) -> tuple[str, str]:
    """(markdown, 무료 티어 telegram) 문자열."""
    d = build_report_data(settings, store, report_date, lookback_days)
    md = build_markdown(d, settings.top_n)
    tg = build_telegram(d, tier="free", site_url=settings.site_url)
    return md, tg


def run_daily(settings: Settings, days: int = 7, lookback_days: int = 1, dry_run: bool = False) -> str:
    """야간 스케줄러가 호출하는 진입점. 리포트 파일 경로를 돌려준다."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    store = Store(settings.db_path)
    client = DartClient(settings.dart_api_key, sleep=settings.request_sleep)

    collect(settings, client, store, days=days)
    parse_pending(settings, client, store)

    today = today_kst()
    data = build_report_data(settings, store, today, lookback_days)
    md = build_markdown(data, settings.top_n)
    tg_free = build_telegram(data, tier="free", site_url=settings.site_url)
    tg_full = build_telegram(data, tier="full", site_url=settings.site_url, top_n=settings.top_n)

    path = settings.report_dir / f"{today:%Y-%m-%d}.md"
    path.write_text(md, encoding="utf-8")
    (settings.report_dir / f"{today:%Y-%m-%d}.telegram.txt").write_text(tg_full, encoding="utf-8")
    log.info("리포트 저장: %s", path)

    sent = False
    if not dry_run:
        # 무료 채널: 상위 몇 건만. 유료 채널(설정돼 있으면): 전체.
        sent = notify.send_telegram(settings, tg_free, chat_id=settings.telegram_chat_id) or sent
        if settings.telegram_paid_chat_id:
            sent = notify.send_telegram(settings, tg_full, chat_id=settings.telegram_paid_chat_id) or sent
        sent = notify.send_email(settings, f"[내부자 매매] {today:%Y-%m-%d}", md) or sent
    store.record_report(today.isoformat(), str(path), sent)

    if settings.site_dir:
        from .site import build_site  # 순환 import 방지
        build_site(settings, store, settings.site_dir, days=settings.site_days, lookback_days=lookback_days)

    log.info("완료. 발송=%s, DB 현황=%s", sent, store.stats())
    return str(path)


def backfill(settings: Settings, days: int) -> None:
    """과거 데이터 적재. 통계 기능을 붙일 때 쓴다."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    store = Store(settings.db_path)
    client = DartClient(settings.dart_api_key, sleep=settings.request_sleep)
    collect(settings, client, store, days=days)
    while True:
        ok, fail = parse_pending(settings, client, store, limit=200)
        if ok + fail == 0:
            break
    log.info("백필 완료: %s", store.stats())
