"""DART OpenAPI 클라이언트.

사용하는 엔드포인트
- list.json      : 공시 목록. 지분공시(B) 중 '임원ㆍ주요주주 특정증권등 소유상황보고서'(B002)만 조회
- document.xml   : 공시 원문(zip 안의 XML). 거래 상세 표를 여기서 파싱
- elestock.json  : 회사별 임원·주요주주 소유보고 요약. 직위/관계 보강용

API 키는 https://opendart.fss.or.kr 에서 무료 발급 (일 20,000건 한도).
"""
from __future__ import annotations

import io
import logging
import time
import zipfile
from datetime import date, timedelta
from typing import Iterator

import requests

log = logging.getLogger(__name__)

BASE = "https://opendart.fss.or.kr/api"
# 지분공시(D) 중 '임원ㆍ주요주주특정증권등소유상황보고서'. (D001 은 5% 대량보유, B 는 주요사항보고서 — 다른 것)
PUBLIC_TYPE_OWNERSHIP = "D"
DETAIL_TYPE_INSIDER_OWNERSHIP = "D002"
REPORT_NAME_KEYWORD = "소유상황"  # 목록에 섞여 들어오는 다른 서식을 거르는 안전장치


class DartError(RuntimeError):
    pass


class DartClient:
    def __init__(self, api_key: str, sleep: float = 0.3, timeout: int = 30):
        if not api_key:
            raise DartError("DART_API_KEY 가 비어 있습니다. .env 에 키를 넣어주세요.")
        self.api_key = api_key
        self.sleep = sleep
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "dart-insider/0.1"

    # ---- 내부 유틸 -----------------------------------------------------
    def _get(self, path: str, **params) -> requests.Response:
        params["crtfc_key"] = self.api_key
        resp = self.session.get(f"{BASE}/{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        if self.sleep:
            time.sleep(self.sleep)
        return resp

    def _get_json(self, path: str, **params) -> dict:
        data = self._get(path, **params).json()
        status = data.get("status")
        # 000 정상, 013 조회 결과 없음
        if status not in ("000", "013"):
            raise DartError(f"{path} status={status} message={data.get('message')}")
        return data

    # ---- 공시 목록 -----------------------------------------------------
    def iter_insider_filings(self, start: date, end: date) -> Iterator[dict]:
        """기간 내 B002 공시를 페이지 끝까지 순회한다. 기간은 최대 3개월 단위로 쪼개 호출한다."""
        cur = start
        while cur <= end:
            chunk_end = min(end, cur + timedelta(days=89))
            yield from self._iter_pages(cur, chunk_end)
            cur = chunk_end + timedelta(days=1)

    def _iter_pages(self, start: date, end: date) -> Iterator[dict]:
        page = 1
        while True:
            data = self._get_json(
                "list.json",
                bgn_de=start.strftime("%Y%m%d"),
                end_de=end.strftime("%Y%m%d"),
                pblntf_ty=PUBLIC_TYPE_OWNERSHIP,
                pblntf_detail_ty=DETAIL_TYPE_INSIDER_OWNERSHIP,
                page_no=page,
                page_count=100,
            )
            items = data.get("list") or []
            for it in items:
                # 정정공시([기재정정]...)도 들어오는데 원문 파싱 시 함께 처리한다. 서식이 다른 건 건너뛴다.
                if REPORT_NAME_KEYWORD not in (it.get("report_nm") or ""):
                    log.debug("건너뜀(다른 서식): %s %s", it.get("rcept_no"), it.get("report_nm"))
                    continue
                yield it
            total_page = int(data.get("total_page") or 1)
            log.info("list.json %s~%s page %s/%s (%s건)", start, end, page, total_page, len(items))
            if page >= total_page:
                break
            page += 1

    # ---- 공시 원문 -----------------------------------------------------
    def fetch_document_xml(self, rcept_no: str) -> str:
        """document.xml 은 zip 으로 내려온다. 가장 큰 XML 을 본문으로 본다."""
        resp = self._get("document.xml", rcept_no=rcept_no)
        ctype = resp.headers.get("Content-Type", "")
        if "zip" not in ctype and not resp.content[:2] == b"PK":
            # 오류 응답은 XML/JSON 으로 온다
            raise DartError(f"document.xml {rcept_no}: 예상치 못한 응답 ({ctype}) {resp.text[:200]}")
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            if not names:
                raise DartError(f"document.xml {rcept_no}: zip 안에 XML 이 없습니다")
            name = max(names, key=lambda n: zf.getinfo(n).file_size)
            raw = zf.read(name)
        for enc in ("utf-8", "euc-kr", "cp949"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    # ---- 보강 정보 -----------------------------------------------------
    def elestock(self, corp_code: str) -> list[dict]:
        """회사별 임원·주요주주 소유보고 요약. 보고자별 직위/관계를 돌려준다."""
        data = self._get_json("elestock.json", corp_code=corp_code)
        return data.get("list") or []
