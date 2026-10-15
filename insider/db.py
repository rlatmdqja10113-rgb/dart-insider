"""SQLite 저장소. 공시 목록(filings)과 개별 거래(trades)를 보관한다."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    rcept_no    TEXT PRIMARY KEY,
    corp_code   TEXT NOT NULL,
    corp_name   TEXT NOT NULL,
    stock_code  TEXT,
    corp_cls    TEXT,
    flr_nm      TEXT,
    rcept_dt    TEXT NOT NULL,
    report_nm   TEXT,
    parsed      INTEGER NOT NULL DEFAULT 0,   -- 0 미처리, 1 성공, -1 실패
    parse_error TEXT,
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    reported_at TEXT                          -- 발송한 시각. NULL 이면 아직 안 보냄 ('migrated'/'bootstrap' 은 보낸 걸로 간주)
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rcept_no      TEXT NOT NULL REFERENCES filings(rcept_no),
    corp_code     TEXT NOT NULL,
    corp_name     TEXT NOT NULL,
    stock_code    TEXT,
    reporter      TEXT,
    reporter_type TEXT,          -- 개인(국내) / 법인(국내) 등. DART 보고자 구분
    position      TEXT,
    relation      TEXT,
    event_date    TEXT,          -- 보고사유 발생일 YYYY-MM-DD
    reason        TEXT,          -- 보고사유 원문 (장내매수 등)
    kind          TEXT NOT NULL, -- buy / sell / other
    security_type TEXT,
    before_cnt    INTEGER,
    change_cnt    INTEGER,
    after_cnt     INTEGER,
    price         INTEGER,       -- 취득/처분 단가(원)
    amount        INTEGER,       -- |change_cnt| * price
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_corp_date ON trades(corp_code, event_date);
CREATE INDEX IF NOT EXISTS idx_trades_rcept ON trades(rcept_no);

CREATE TABLE IF NOT EXISTS reports (
    report_date TEXT PRIMARY KEY,
    path        TEXT,
    sent        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(trades)")}
        if "reporter_type" not in cols:
            self.conn.execute("ALTER TABLE trades ADD COLUMN reporter_type TEXT")
            self.conn.commit()
        fcols = {r[1] for r in self.conn.execute("PRAGMA table_info(filings)")}
        if "reported_at" not in fcols:
            self.conn.execute("ALTER TABLE filings ADD COLUMN reported_at TEXT")
            # 이 컬럼이 생기기 전의 공시는 예전 방식(고정 기간)으로 이미 발송됐거나 지나간 것이다.
            # 그대로 두면 다음 실행에서 한꺼번에 다시 발송되므로 '보낸 것'으로 표시한다.
            self.conn.execute("UPDATE filings SET reported_at = 'migrated'")
            self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- filings -------------------------------------------------------
    def upsert_filings(self, rows: Iterable[dict]) -> int:
        """새 공시만 추가한다. 추가된 건수를 돌려준다."""
        inserted = 0
        with self.tx() as c:
            for r in rows:
                cur = c.execute(
                    """INSERT OR IGNORE INTO filings
                       (rcept_no, corp_code, corp_name, stock_code, corp_cls, flr_nm, rcept_dt, report_nm)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        r["rcept_no"], r["corp_code"], r["corp_name"], r.get("stock_code"),
                        r.get("corp_cls"), r.get("flr_nm"), r["rcept_dt"], r.get("report_nm"),
                    ),
                )
                inserted += cur.rowcount
        return inserted

    def unparsed_filings(self, limit: int = 500, retry_since: str | None = None) -> list[sqlite3.Row]:
        """아직 원문을 못 읽은 공시. retry_since(YYYYMMDD)를 주면 그 이후 접수분 중 실패했던 것도 다시 시도한다.
        (DART 가 원문을 늦게 올려 '014 파일이 존재하지 않습니다'로 실패하는 경우가 있다)"""
        if retry_since:
            return self.conn.execute(
                """SELECT * FROM filings WHERE parsed = 0 OR (parsed = -1 AND rcept_dt >= ?)
                   ORDER BY rcept_dt, rcept_no LIMIT ?""",
                (retry_since, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM filings WHERE parsed = 0 ORDER BY rcept_dt, rcept_no LIMIT ?", (limit,)
        ).fetchall()

    # ---- 발송 여부 -----------------------------------------------------
    def unreported_filings(self, since_rcept_dt: str) -> list[dict]:
        """원문을 읽었고 아직 발송하지 않은 공시 (since 이후 접수분만)."""
        rows = self.conn.execute(
            """SELECT * FROM filings
               WHERE parsed = 1 AND reported_at IS NULL AND rcept_dt >= ?
               ORDER BY rcept_dt, rcept_no""",
            (since_rcept_dt,),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_reported(self, rcept_nos: Iterable[str], when: str) -> None:
        with self.tx() as c:
            c.executemany("UPDATE filings SET reported_at = ? WHERE rcept_no = ?", [(when, r) for r in rcept_nos])

    def has_any_reported(self) -> bool:
        return self.conn.execute("SELECT 1 FROM filings WHERE reported_at IS NOT NULL LIMIT 1").fetchone() is not None

    def mark_reported_before(self, before_rcept_dt: str, tag: str) -> int:
        """before 보다 먼저 접수된 미발송 공시를 '보낸 것'으로 표시한다 (첫 실행 때 지난 공시를 쏟아내지 않도록)."""
        with self.tx() as c:
            cur = c.execute(
                "UPDATE filings SET reported_at = ? WHERE reported_at IS NULL AND rcept_dt < ?", (tag, before_rcept_dt)
            )
        return cur.rowcount

    def reported_trade_keys(self, corp_codes: Iterable[str]) -> set[tuple]:
        """이미 발송한(또는 지난 것으로 처리한) 공시에 들어 있던 거래들의 키. 정정공시 중복 제거용."""
        codes = list(set(corp_codes))
        keys: set[tuple] = set()
        for i in range(0, len(codes), 500):
            chunk = codes[i : i + 500]
            rows = self.conn.execute(
                f"""SELECT t.* FROM trades t JOIN filings f ON f.rcept_no = t.rcept_no
                    WHERE f.reported_at IS NOT NULL AND t.corp_code IN ({",".join("?" * len(chunk))})""",
                chunk,
            ).fetchall()
            keys.update(trade_key(dict(r)) for r in rows)
        return keys

    def mark_parsed(self, rcept_no: str, ok: bool, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE filings SET parsed = ?, parse_error = ? WHERE rcept_no = ?",
                (1 if ok else -1, error, rcept_no),
            )

    # ---- trades --------------------------------------------------------
    def replace_trades(self, rcept_no: str, trades: Iterable[dict]) -> int:
        n = 0
        with self.tx() as c:
            c.execute("DELETE FROM trades WHERE rcept_no = ?", (rcept_no,))
            for t in trades:
                c.execute(
                    """INSERT INTO trades
                       (rcept_no, corp_code, corp_name, stock_code, reporter, reporter_type, position, relation,
                        event_date, reason, kind, security_type, before_cnt, change_cnt, after_cnt,
                        price, amount, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        rcept_no, t["corp_code"], t["corp_name"], t.get("stock_code"),
                        t.get("reporter"), t.get("reporter_type"), t.get("position"), t.get("relation"),
                        t.get("event_date"), t.get("reason"), t["kind"], t.get("security_type"),
                        t.get("before_cnt"), t.get("change_cnt"), t.get("after_cnt"),
                        t.get("price"), t.get("amount"), t.get("note"),
                    ),
                )
                n += 1
        return n

    def trades_between(self, start_rcept_dt: str, end_rcept_dt: str) -> list[dict]:
        """접수일 기준 구간의 거래를 가져온다 (YYYYMMDD)."""
        rows = self.conn.execute(
            """SELECT t.*, f.rcept_dt FROM trades t
               JOIN filings f ON f.rcept_no = t.rcept_no
               WHERE f.rcept_dt BETWEEN ? AND ?
               ORDER BY f.rcept_dt, t.corp_name""",
            (start_rcept_dt, end_rcept_dt),
        ).fetchall()
        return [dict(r) for r in rows]

    def trades_for_filings(self, rcept_nos: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(rcept_nos), 500):
            chunk = rcept_nos[i : i + 500]
            rows = self.conn.execute(
                f"""SELECT t.*, f.rcept_dt FROM trades t JOIN filings f ON f.rcept_no = t.rcept_no
                    WHERE t.rcept_no IN ({",".join("?" * len(chunk))})
                    ORDER BY f.rcept_dt, t.corp_name""",
                chunk,
            ).fetchall()
            out += [dict(r) for r in rows]
        return out

    def corp_trades_since(self, corp_code: str, since_event_date: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE corp_code = ? AND event_date >= ? ORDER BY event_date",
            (corp_code, since_event_date),
        ).fetchall()
        return [dict(r) for r in rows]

    def record_report(self, report_date: str, path: str, sent: bool) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO reports (report_date, path, sent) VALUES (?,?,?)",
                (report_date, path, 1 if sent else 0),
            )

    def stats(self) -> dict:
        f = self.conn.execute(
            """SELECT COUNT(*) n, SUM(parsed=1) ok, SUM(parsed=-1) bad,
                      SUM(parsed=1 AND reported_at IS NULL) pending FROM filings"""
        ).fetchone()
        t = self.conn.execute("SELECT COUNT(*) n FROM trades").fetchone()
        return {
            "filings": f["n"] or 0, "parsed_ok": f["ok"] or 0, "parsed_fail": f["bad"] or 0,
            "unreported": f["pending"] or 0, "trades": t["n"] or 0,
        }


def trade_key(t: dict) -> tuple:
    """같은 거래인지 판별하는 키. 원본 공시와 [기재정정] 공시가 같은 거래를 반복 보고할 때 쓴다.
    단가는 정정으로 고쳐지는 경우가 많아 키에서 뺀다."""
    return (
        t["corp_code"], t.get("reporter") or "", t.get("event_date") or "", t.get("reason") or "",
        t.get("security_type") or "", t.get("change_cnt") or 0,
    )
