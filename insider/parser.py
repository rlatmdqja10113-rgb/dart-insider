"""'임원ㆍ주요주주 특정증권등 소유상황보고서' 원문(XML)에서 보고자 정보와 거래 행을 뽑아낸다.

실제 DART 서식 (2026년 기준):

  보고자 표
    보고구분 | 변동 | 보고자 구분 | 개인(국내)
    성명(명칭) | 한 글 | 정남진 | 한자(영문) | 鄭南鎭
    발행회사와의 관계 | 임원(등기여부) | 비등기임원 | 직위명 | 상무이사
    주요주주 | -

  거래 표
    보고사유 | 변동일 | 특정증권등의종류 | 변동전 | 증감 | 변동후 | 취득/처분단가 | 비고 | 거래계획보고일자
    장내매수(+) | 2026년 10월 07일 | 보통주 | 6,000 | 974 | 6,974 | 2,424 | - | -
    합 계 | 6,000 | 1,000 | 7,000 | - | -

열 순서가 회사·연도마다 조금씩 달라서 '몇 번째 열'로 고정하지 않고, 각 행에서 날짜 셀을 기준점으로 삼아
앞뒤 셀을 읽는다. 라벨-값 쌍은 같은 행 안에서 '라벨 다음 셀'로 읽는다.
"""
from __future__ import annotations

import re
import warnings
from dataclasses import asdict, dataclass
from typing import Iterable

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

# DART 원문은 XML 을 표방하지만 닫히지 않은 태그가 흔해 관대한 HTML 파서를 일부러 쓴다.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

CELL_TAGS = ("td", "te", "th", "tu")

DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
NUM_RE = re.compile(r"^[-+(]?\s*\d[\d,]*\s*\)?$")
EMPTY = {"", "-", "―", "—", "–"}

# 보고사유 → 거래 성격. 순서가 중요하다(앞에서 먼저 매칭).
BUY_KEYWORDS = ("장내매수", "시간외매수", "장외매수", "공개매수", "매수")
SELL_KEYWORDS = ("장내매도", "시간외매도", "장외매도", "매도")
# 자기 돈으로 사고판 것이 아닌 사유. 신호로 치지 않는다.
NOISE_KEYWORDS = (
    "주식매수선택권", "스톡옵션", "신주인수권", "전환", "교환", "증여", "수증", "상속",
    "무상", "주식배당", "유상증자", "유상신주", "액면", "병합", "분할", "합병",
    "신규보고", "신규선임", "임원선임", "임원퇴임", "퇴임", "신고의무발생", "담보",
    "대차", "대여", "반환", "소각", "출자", "현물", "우리사주", "성과급", "양도제한", "제3자배정",
    "장외매수(+)주식매수선택권",
)


@dataclass
class Trade:
    corp_code: str
    corp_name: str
    stock_code: str | None
    reporter: str | None
    reporter_type: str | None   # 개인(국내) / 법인(국내) / 개인(국외) / 법인(국외) — DART 가 적어준 값
    position: str | None
    relation: str | None
    event_date: str | None
    reason: str | None
    kind: str
    security_type: str | None
    before_cnt: int | None
    change_cnt: int | None
    after_cnt: int | None
    price: int | None
    amount: int | None
    note: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def classify_reason(reason: str | None) -> str:
    """보고사유 문자열을 buy / sell / other 로 분류한다."""
    if not reason:
        return "other"
    r = reason.replace(" ", "")
    for kw in NOISE_KEYWORDS:
        if kw in r:
            return "other"
    for kw in BUY_KEYWORDS:
        if kw in r:
            return "buy"
    for kw in SELL_KEYWORDS:
        if kw in r:
            return "sell"
    return "other"


# ---- 저수준 유틸 ---------------------------------------------------------
def _text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _rows(table) -> list[list[str]]:
    rows = []
    for tr in table.find_all("tr"):
        cells = [_text(c) for c in tr.find_all(CELL_TAGS)]
        if cells:
            rows.append(cells)
    return rows


def _parse_date(s: str) -> str | None:
    m = DATE_RE.search(s or "")
    if not m:
        return None
    y, mo, d = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _is_num(s: str) -> bool:
    return bool(NUM_RE.match((s or "").strip()))


def _parse_int(s: str | None) -> int | None:
    if s is None:
        return None
    s = s.strip()
    if s in EMPTY:
        return None
    neg = s.startswith("(") and s.endswith(")")  # 회계식 음수 표기
    m = re.search(r"[-+]?\d[\d,]*", s)
    if not m:
        return None
    try:
        v = int(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


def _squash(s: str) -> str:
    return (s or "").replace(" ", "")


# ---- 보고자 정보 ---------------------------------------------------------
def _reporter_info(soup: BeautifulSoup) -> dict:
    """성명 / 보고자 구분 / 직위 / 발행회사와의 관계. 같은 행에서 '라벨 다음 셀'을 읽는다."""
    info = {"reporter": None, "reporter_type": None, "position": None, "relation": None}
    rel_parts: list[str] = []

    for table in soup.find_all("table"):
        rows = _rows(table)
        flat = _squash(" ".join(" ".join(r) for r in rows))
        if "성명" not in flat and "보고자구분" not in flat:
            continue
        for cells in rows:
            for i, c in enumerate(cells[:-1]):
                key = _squash(c)
                nxt = cells[i + 1].strip()
                if not nxt or nxt in EMPTY:
                    continue
                if key == "한글" and info["reporter"] is None:
                    info["reporter"] = nxt
                elif key in ("성명(명칭)", "보고자") and info["reporter"] is None and _squash(nxt) not in ("한글", "한자(영문)"):
                    info["reporter"] = nxt
                elif key == "보고자구분" and info["reporter_type"] is None:
                    info["reporter_type"] = nxt
                elif key == "직위명" and info["position"] is None:
                    info["position"] = nxt
                elif key == "임원(등기여부)":
                    rel_parts.append(f"임원({nxt})")
                elif key == "주요주주":
                    rel_parts.append(nxt)
                elif key == "발행회사와의관계" and _squash(nxt) not in ("임원(등기여부)", "임원"):
                    rel_parts.append(nxt)
        if info["reporter"] and info["reporter_type"] and info["position"]:
            break

    if rel_parts:
        info["relation"] = " / ".join(dict.fromkeys(rel_parts))  # 중복 제거, 순서 유지
    if info["reporter"] and re.fullmatch(r"[가-힣 ]+", info["reporter"]):
        info["reporter"] = info["reporter"].replace(" ", "")  # '홍 승 표' → '홍승표'

    if info["position"] in EMPTY:
        info["position"] = None
    # 직위가 비어 있고 관계에 '최대주주' 같은 게 있으면 그걸 직위 자리로
    if info["position"] is None and info["relation"]:
        m = re.search(r"(최대주주|10%이상주주|사실상지배주주|주요주주|대표이사|회장|부회장|사장|부사장|전무|상무|이사|감사)", info["relation"])
        if m:
            info["position"] = m.group(1)
    return info


# ---- 거래 표 -------------------------------------------------------------
def _is_trade_table(rows: list[list[str]]) -> bool:
    head = _squash(" ".join(" ".join(r) for r in rows[:3]))
    return "보고사유" in head and ("증감" in head or "변동" in head)


def _trade_rows(rows: list[list[str]]) -> Iterable[dict]:
    """각 행에서 날짜 셀을 기준점으로 잡고, 그 앞의 마지막 텍스트 셀을 사유, 뒤의 첫 텍스트 셀을 종류,
    그 다음 숫자 셀들을 변동전/증감/변동후/단가 로 읽는다."""
    for cells in rows:
        di = next((i for i, c in enumerate(cells) if _parse_date(c)), None)
        if di is None:
            continue  # 헤더, 합계 행

        before_cells = [c for c in cells[:di] if c and not _is_num(c)]
        after = list(cells[di + 1 :])

        if before_cells:            # 실제 서식: 사유 | 날짜 | 종류 | 숫자들
            reason = before_cells[-1]
        elif after and not _is_num(after[0]):  # 구형: 날짜 | 사유 | 종류 | 숫자들
            reason = after.pop(0)
        else:
            continue
        if _squash(reason) in ("합계", "소계", "계"):
            continue

        security_type = None
        if after and not _is_num(after[0]) and after[0] not in EMPTY:
            security_type = after.pop(0)

        nums: list[int | None] = []
        note = None
        for c in after:
            if _is_num(c) or c in EMPTY:
                if len(nums) < 4:
                    nums.append(_parse_int(c))
            elif note is None and not _parse_date(c):
                note = c
        nums += [None] * (4 - len(nums))
        before, change, after_cnt, price = nums

        kind = classify_reason(reason)
        if change is not None:
            if kind == "sell" and change > 0:
                change = -change
            if kind == "buy" and change < 0:
                change = -change
        if change is None and before is not None and after_cnt is not None:
            change = after_cnt - before

        yield {
            "event_date": _parse_date(cells[di]),
            "reason": reason,
            "kind": kind,
            "security_type": security_type,
            "before_cnt": before,
            "change_cnt": change,
            "after_cnt": after_cnt,
            "price": price if price and price > 0 else None,
            "note": note,
        }


def parse_ownership_report(xml_text: str, filing: dict) -> list[Trade]:
    """원문 전체를 받아 Trade 목록을 돌려준다. filing 은 list.json 의 한 행."""
    soup = BeautifulSoup(xml_text, "html.parser")
    who = _reporter_info(soup)

    trades: list[Trade] = []
    seen: set[tuple] = set()
    for table in soup.find_all("table"):
        rows = _rows(table)
        if not rows or not _is_trade_table(rows):
            continue
        for t in _trade_rows(rows):
            key = (t["event_date"], t["reason"], t["security_type"], t["change_cnt"], t["price"])
            if key in seen:
                continue
            seen.add(key)
            amount = abs(t["change_cnt"]) * t["price"] if t["change_cnt"] and t["price"] else None
            trades.append(
                Trade(
                    corp_code=filing["corp_code"],
                    corp_name=filing["corp_name"],
                    stock_code=filing.get("stock_code") or None,
                    reporter=who["reporter"] or filing.get("flr_nm"),
                    reporter_type=who["reporter_type"],
                    position=who["position"],
                    relation=who["relation"],
                    amount=amount,
                    **t,
                )
            )
    return trades
