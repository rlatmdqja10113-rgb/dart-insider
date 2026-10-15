"""CLI.

python -m insider run                 # 수집 + 파싱 + 아직 안 보낸 공시 발송 (야간 스케줄용)
python -m insider run --dry-run       # 발송 없이 리포트 파일만 ('보낸 것' 표시도 안 함)
python -m insider report --unsent     # 다음 실행 때 나갈 내용 미리보기 (DB 를 바꾸지 않음)
python -m insider report --lookback 3 # 접수일 기준 최근 N일 리포트
python -m insider backfill --days 365 # 과거 1년치 적재
python -m insider stats
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import settings, today_kst
from .db import Store
from . import pipeline


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="insider")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="수집·파싱·리포트·발송 (아직 안 보낸 공시만)")
    r.add_argument("--days", type=int, default=7, help="공시 목록 조회 범위(일). 보고 지연을 감안해 여유 있게")
    r.add_argument("--max-age", type=int, default=5, help="이보다 오래된(일) 미발송 공시는 보내지 않음")
    r.add_argument("--lookback", type=int, help=argparse.SUPPRESS)  # 예전 옵션. 받아만 두고 쓰지 않는다.
    r.add_argument("--dry-run", action="store_true", help="발송하지 않음 ('보낸 것' 표시도 안 함)")

    b = sub.add_parser("backfill", help="과거 데이터 적재")
    b.add_argument("--days", type=int, default=365)

    rp = sub.add_parser("report", help="DB 내용으로 리포트만 출력 (발송 안 함)")
    rp.add_argument("--unsent", action="store_true", help="다음 실행 때 나갈 내용(아직 안 보낸 공시) 미리보기")
    rp.add_argument("--lookback", type=int, default=1)
    rp.add_argument("--max-age", type=int, default=5)
    rp.add_argument("--date", type=date.fromisoformat, default=today_kst())

    sub.add_parser("stats", help="DB 현황")
    sub.add_parser("reparse", help="저장된 원문(data/raw)으로 파서만 다시 실행 (네트워크 불필요)")

    st = sub.add_parser("site", help="DB 내용으로 정적 사이트 생성")
    st.add_argument("--days", type=int, default=settings.site_days)
    st.add_argument("--lookback", type=int, default=1)
    st.add_argument("--out", default=str(settings.site_dir or "site"))

    a = p.parse_args(argv)

    if a.cmd == "run":
        if a.lookback is not None:
            print("참고: --lookback 은 더 이상 쓰지 않습니다. 아직 안 보낸 공시를 자동으로 골라 보냅니다.", file=sys.stderr)
        return pipeline.run_daily(settings, days=a.days, max_age_days=a.max_age, dry_run=a.dry_run)
    elif a.cmd == "backfill":
        pipeline.backfill(settings, days=a.days)
    elif a.cmd == "report":
        settings.ensure_dirs()
        store = Store(settings.db_path)
        from .report import build_markdown, build_telegram
        if a.unsent:
            filings, trades = pipeline.select_unreported(store, a.date, a.max_age, bootstrap=False)
            if not filings:
                print("다음 실행 때 보낼 새 공시가 없습니다.")
                return 0
            data = pipeline.build_report_data(settings, store, a.date, trades=trades, filings=filings)
        else:
            data = pipeline.build_report_data(settings, store, a.date, a.lookback)
        print(build_markdown(data, settings.top_n))
        print("\n--- telegram (free) ---\n")
        print(build_telegram(data, tier="free", site_url=settings.site_url))
        print("\n--- telegram (full) ---\n")
        print(build_telegram(data, tier="full", site_url=settings.site_url, top_n=settings.top_n))
    elif a.cmd == "stats":
        print(Store(settings.db_path).stats())
    elif a.cmd == "reparse":
        import logging
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
        print(pipeline.reparse(settings, Store(settings.db_path)))
    elif a.cmd == "site":
        from pathlib import Path
        from .site import build_site
        settings.ensure_dirs()
        out = build_site(settings, Store(settings.db_path), Path(a.out), days=a.days, lookback_days=a.lookback)
        print(out / "index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
