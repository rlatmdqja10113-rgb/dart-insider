"""수집 → 파싱 → 점수화 → 리포트 → 발송 을 잇는 파이프라인.

발송 단위는 '날짜 구간'이 아니라 '아직 안 보낸 공시'다.
- 월요일 아침에는 금요일 낮에 접수된 공시가 자연스럽게 포함된다.
- 같은 날 두 번 돌아도(재실행, 수동 실행) 이미 보낸 공시는 다시 보내지 않는다.
- 발송이 실패하면 '보낸 것' 표시를 하지 않으므로 다음 실행이 다시 보낸다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import KST, Settings, today_kst
from .dart_client import DartClient, DartError
from .db import Store, trade_key
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


def parse_pending(
    settings: Settings, client: DartClient, store: Store, limit: int = 500, retry_since: str | None = None
) -> tuple[int, int]:
    """아직 원문을 안 읽은 공시를 내려받아 거래 행을 추출한다. 원문은 data/raw 에 저장해 둔다.
    retry_since(YYYYMMDD) 를 주면 그 이후 접수분 중 실패했던 공시도 다시 시도한다. (성공, 실패) 건수."""
    ok = fail = 0
    for f in store.unparsed_filings(limit, retry_since=retry_since):
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


def dedupe_restated(trades: list[dict]) -> list[dict]:
    """같은 거래가 여러 공시(원본 + [기재정정])에 반복되면 가장 나중 공시의 행만 남긴다.
    한 공시 안의 행끼리는 건드리지 않는다 (같은 날 같은 수량을 두 번 산 경우 등)."""
    latest: dict[tuple, str] = {}
    for t in trades:
        k = trade_key(t)
        if k not in latest or t["rcept_no"] > latest[k]:
            latest[k] = t["rcept_no"]
    return [t for t in trades if latest[trade_key(t)] == t["rcept_no"]]


def select_unreported(
    store: Store, today: date, max_age_days: int, bootstrap: bool = True
) -> tuple[list[dict], list[dict]]:
    """아직 발송하지 않은 공시와 그 거래를 고른다. (filings, trades)

    - max_age_days 보다 오래된 미발송 공시는 뉴스 가치가 없어 제외한다.
    - 처음 발송하는 DB 라면 어제 이전 공시는 지난 것으로 보고 건너뛴다 (bootstrap=False 면 표시는 하지 않고 미리보기만).
    - 이미 보낸 공시와 똑같은 거래(정정공시 반복분)는 뺀다.
    """
    since = (today - timedelta(days=max_age_days)).strftime("%Y%m%d")
    if not store.has_any_reported():
        cutoff = (today - timedelta(days=1)).strftime("%Y%m%d")
        if bootstrap:
            n = store.mark_reported_before(cutoff, "bootstrap")
            if n:
                log.info("첫 발송: %s 이전 접수 공시 %s건은 지난 것으로 보고 건너뜁니다.", cutoff, n)
        since = max(since, cutoff)

    filings = store.unreported_filings(since)
    trades = store.trades_for_filings([f["rcept_no"] for f in filings])
    already = store.reported_trade_keys(t["corp_code"] for t in trades)
    kept = [t for t in dedupe_restated(trades) if trade_key(t) not in already]
    if len(kept) != len(trades):
        log.info("정정공시 등으로 반복 보고된 거래 %s건 제외", len(trades) - len(kept))
    return filings, kept


def _iso(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def build_report_data(
    settings: Settings,
    store: Store,
    report_date: date,
    lookback_days: int = 1,
    *,
    trades: list[dict] | None = None,
    filings: list[dict] | None = None,
) -> dict:
    """점수화한 리포트 데이터. 마크다운·텔레그램·사이트가 모두 이 결과를 쓴다.

    trades 를 주면 그대로 쓰고(일일 발송: 아직 안 보낸 공시), 안 주면 report_date 로부터
    lookback_days 일 전까지 접수된 거래를 쓴다(사이트, report 명령)."""
    if trades is None:
        start = (report_date - timedelta(days=lookback_days)).strftime("%Y%m%d")
        end = report_date.strftime("%Y%m%d")
        trades = store.trades_between(start, end)
        n_filings = len({t["rcept_no"] for t in trades})
    else:
        dts = sorted({f["rcept_dt"] for f in filings or []} | {t["rcept_dt"] for t in trades if t.get("rcept_dt")})
        start, end = (dts[0], dts[-1]) if dts else (report_date.strftime("%Y%m%d"),) * 2
        n_filings = len(filings) if filings is not None else len({t["rcept_no"] for t in trades})

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
            "filings_in_period": n_filings,
            "trades_in_period": len(trades),
            "noise_excluded": sum(1 for t in trades if t["kind"] == "other"),
        },
        "rcept_from": _iso(start),
        "rcept_to": _iso(end),
        "period": f"{_iso(start)} ~ {_iso(end)}",
        "report_date": report_date,
    }


def build_report(settings: Settings, store: Store, report_date: date, lookback_days: int) -> tuple[str, str]:
    """(markdown, 무료 티어 telegram) 문자열."""
    d = build_report_data(settings, store, report_date, lookback_days)
    md = build_markdown(d, settings.top_n)
    tg = build_telegram(d, tier="free", site_url=settings.site_url)
    return md, tg


EXIT_OK = 0
EXIT_SEND_FAILED = 2


def _report_path(settings: Settings, today: date, suffix: str) -> Path:
    """같은 날 두 번째 발송분이면 시각을 붙여 앞의 것을 덮어쓰지 않는다."""
    p = settings.report_dir / f"{today:%Y-%m-%d}{suffix}"
    if p.exists():
        p = settings.report_dir / f"{today:%Y-%m-%d}_{datetime.now(KST):%H%M}{suffix}"
    return p


def deliver(settings: Settings, store: Store, today: date, filings: list[dict], md: str, tg_free: str, tg_full: str) -> int:
    """설정된 채널로 보낸다. 하나라도 성공하면 이 공시들을 '보낸 것'으로 표시한다(성공한 채널에 중복 발송 방지).
    실패한 채널이 있으면 EXIT_SEND_FAILED 를 돌려준다 → GitHub Actions 가 빨간 X 로 알려준다."""
    results: dict[str, bool] = {}
    if settings.telegram_bot_token and settings.telegram_chat_id:
        results["텔레그램(무료 채널)"] = notify.send_telegram(settings, tg_free, chat_id=settings.telegram_chat_id)
    if settings.telegram_bot_token and settings.telegram_paid_chat_id:
        results["텔레그램(유료 채널)"] = notify.send_telegram(settings, tg_full, chat_id=settings.telegram_paid_chat_id)
    if settings.smtp_host and settings.mail_to:
        results["이메일"] = notify.send_email(settings, f"[내부자 매매] {today:%Y-%m-%d}", md)

    if not results:
        log.warning("발송 채널이 설정돼 있지 않습니다(.env 의 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). 리포트 파일만 만들었습니다.")
        return EXIT_OK

    any_ok = any(results.values())
    if any_ok:
        store.mark_reported([f["rcept_no"] for f in filings], datetime.now(KST).isoformat(timespec="seconds"))
    store.record_report(today.isoformat(), "", any_ok)

    failed = [name for name, ok in results.items() if not ok]
    if failed:
        log.error("발송 실패: %s%s", ", ".join(failed), "" if any_ok else " — 다음 실행 때 다시 보냅니다.")
        return EXIT_SEND_FAILED
    log.info("발송 완료: %s (공시 %s건 '보낸 것'으로 표시)", ", ".join(results), len(filings))
    return EXIT_OK


def run_daily(settings: Settings, days: int = 7, max_age_days: int = 5, dry_run: bool = False) -> int:
    """야간 스케줄러가 호출하는 진입점. 프로세스 종료 코드를 돌려준다 (0 정상, 2 발송 실패)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.ensure_dirs()
    store = Store(settings.db_path)
    client = DartClient(settings.dart_api_key, sleep=settings.request_sleep)
    today = today_kst()

    collect(settings, client, store, days=days)
    parse_pending(settings, client, store, retry_since=(today - timedelta(days=days)).strftime("%Y%m%d"))

    code = EXIT_OK
    filings, trades = select_unreported(store, today, max_age_days)
    if not filings:
        log.info("새로 보낼 공시가 없습니다 (이미 다 보냈거나 아직 접수된 게 없음). 발송을 건너뜁니다.")
    else:
        data = build_report_data(settings, store, today, trades=trades, filings=filings)
        notable = any(data[k] for k in ("buys", "sells", "clusters", "entity_buys", "entity_sells"))
        if not notable:
            # 새 공시가 스톡옵션·증여·정정 반복분 같은 것뿐이면 '볼 것 없음' 메시지는 보내지 않는다.
            log.info("새 공시 %s건이 모두 제외 대상(스톡옵션·증여·소액·정정 반복 등)이라 발송하지 않습니다.", len(filings))
            if not dry_run:
                store.mark_reported([f["rcept_no"] for f in filings], "skipped:nothing-notable")
            filings = []
    if filings:
        md = build_markdown(data, settings.top_n)
        tg_free = build_telegram(data, tier="free", site_url=settings.site_url)
        tg_full = build_telegram(data, tier="full", site_url=settings.site_url, top_n=settings.top_n)
        path = _report_path(settings, today, ".md")
        path.write_text(md, encoding="utf-8")
        path.with_name(path.stem + ".telegram.txt").write_text(tg_full, encoding="utf-8")
        log.info("리포트 저장: %s (새 공시 %s건, 접수 %s)", path, len(filings), data["period"])

        if dry_run:
            log.info("--dry-run: 발송하지 않았고 '보낸 것' 표시도 하지 않았습니다.")
        else:
            code = deliver(settings, store, today, filings, md, tg_free, tg_full)

    if settings.site_dir:
        from .site import build_site  # 순환 import 방지
        build_site(settings, store, settings.site_dir, days=settings.site_days, lookback_days=1)

    log.info("완료. DB 현황=%s", store.stats())
    return code


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
