from __future__ import annotations

import argparse
import json
import random
import re
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import pandas as pd

from config import ROOT


BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}
SSL_CONTEXT = ssl.create_default_context()
DATA_ROOT = ROOT / "示例数据"
REPORT_ROOT = DATA_ROOT / "附件2：财务报告"
RESEARCH_ROOT = DATA_ROOT / "附件5：研报数据"
SSE_DIR = REPORT_ROOT / "reports-上交所"
SZSE_DIR = REPORT_ROOT / "reports-深交所"
BSE_DIR = REPORT_ROOT / "reports-北交所"
STOCK_RESEARCH_DIR = RESEARCH_ROOT / "个股研报"
INDUSTRY_RESEARCH_DIR = RESEARCH_ROOT / "行业研报"
MACRO_RESEARCH_DIR = RESEARCH_ROOT / "宏观研报"
DEFAULT_COMPANY_FILE = DATA_ROOT / "附件1：中药上市公司基本信息（截至到2025年12月22日）.xlsx"
EXPANDED_COMPANY_FILE = DATA_ROOT / "扩展公司池.csv"


@dataclass
class TargetCompany:
    code: str
    name: str = ""
    exchange: str = ""


@dataclass
class DownloadRecord:
    source: str
    code: str
    name: str
    title: str
    published_at: str
    page_url: str
    file_url: str
    saved_path: str


class Downloader:
    def __init__(self, sleep: float = 0.6, retries: int = 3):
        self.sleep = sleep
        self.retries = retries

    def text(self, url: str, *, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, json_data: Optional[Dict[str, Any]] = None) -> str:
        request_url = self._build_url(url, params)
        req_headers = {**BASE_HEADERS, **(headers or {})}
        req_data = None
        if json_data is not None:
            req_data = json.dumps(json_data).encode("utf-8")
            req_headers["Content-Type"] = "application/json"
        
        text = ""
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            req = urllib.request.Request(request_url, headers=req_headers, data=req_data)
            try:
                with urllib.request.urlopen(req, timeout=30, context=SSL_CONTEXT) as resp:
                    data = resp.read()
                    charset = resp.headers.get_content_charset() or "utf-8"
                    text = data.decode(charset, errors="ignore")
                last_error = None
                break
            except Exception as e:
                last_error = e
                if self.sleep and attempt < self.retries - 1:
                    time.sleep(self.sleep * (attempt + 1))
        if last_error is not None:
            print(f"[Warn] Fetch text failed for {request_url}: {last_error}")
        if self.sleep:
            time.sleep(self.sleep)
        return text

    def binary(self, url: str, *, headers: Optional[Dict[str, str]] = None) -> bytes:
        data = b""
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, headers={**BASE_HEADERS, **(headers or {})})
            try:
                with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
                    data = resp.read()
                last_error = None
                break
            except Exception as e:
                last_error = e
                if self.sleep and attempt < self.retries - 1:
                    time.sleep(self.sleep * (attempt + 1))
        if last_error is not None:
            print(f"[Warn] Fetch binary failed for {url}: {last_error}")
        if self.sleep:
            time.sleep(self.sleep)
        return data

    @staticmethod
    def _build_url(url: str, params: Optional[Dict[str, Any]]) -> str:
        if not params:
            return url
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return f"{url}{'&' if '?' in url else '?'}{query}"


class MarketCrawler:
    def __init__(self, downloader: Downloader):
        self.http = downloader

    def discover_targets_from_reports(
        self,
        keyword: str,
        start_year: int,
        end_year: int,
        max_pages: int,
        page_size: int,
        limit: int,
    ) -> List[TargetCompany]:
        results: List[TargetCompany] = []
        seen: Set[str] = set()
        api_url = "https://reportapi.eastmoney.com/report/list"
        for page in range(1, max_pages + 1):
            params = {
                "cb": "datatable123",
                "industryCode": "*",
                "pageNo": page,
                "pageSize": page_size,
                "industry": "*",
                "ratingChange": "",
                "fields": "",
                "qType": 0,
                "rcode": "",
                "p": 1,
                "pageNum": page,
                "_": int(time.time() * 1000),
                "code": "*",
                "orgCode": "",
                "rating": "",
                "beginTime": f"{start_year}-01-01",
                "endTime": f"{end_year}-12-31",
            }
            text = self.http.text(api_url, params=params, headers={"Referer": "https://data.eastmoney.com/report/stock.jshtml"})
            data = parse_json_text(text)
            rows = data.get("data", []) if isinstance(data, dict) and isinstance(data.get("data"), list) else collect_records(data)
            page_added = 0
            for row in rows:
                code = normalize_code(pick_value(row, "stockCode", "securityCode", "code") or "")
                if not code or code in seen:
                    continue
                name = str(pick_value(row, "stockName", "securityName", "name") or "")
                industry_name = str(pick_value(row, "indvInduName", "industryName", "emIndustryName") or "")
                title = str(pick_value(row, "title", "reportTitle", "art_title") or "")
                text_blob = " ".join([code, name, industry_name, title])
                if not matches_keywords(text_blob, keyword):
                    continue
                seen.add(code)
                results.append(TargetCompany(code=code, name=name, exchange=guess_exchange(code)))
                page_added += 1
                if limit and len(results) >= limit:
                    return results
            if page_added == 0:
                break
        return results

    def crawl_exchange_reports(
        self,
        targets: Sequence[TargetCompany],
        start_year: int,
        end_year: int,
        max_pages: int,
        page_size: int,
        max_items: int,
    ) -> List[DownloadRecord]:
        records: List[DownloadRecord] = []
        for target in targets:
            exchange = target.exchange or guess_exchange(target.code)
            if exchange == "sse":
                items = self._crawl_sse(target, start_year, end_year, max_pages, page_size)
            elif exchange == "szse":
                items = self._crawl_szse(target, start_year, end_year, max_pages, page_size)
            elif exchange == "bse":
                items = self._crawl_bse(target)
            else:
                items = []
            for item in items[:max_items]:
                saved = save_binary(item["file_url"], self.http, output_dir_for_exchange(exchange), target.code, item["title"], item["published_at"])
                records.append(
                    DownloadRecord(
                        source=f"exchange:{exchange}",
                        code=target.code,
                        name=target.name,
                        title=item["title"],
                        published_at=item["published_at"],
                        page_url=item["page_url"],
                        file_url=item["file_url"],
                        saved_path=str(saved),
                    )
                )
        return dedupe_records(records)

    def crawl_stock_research(
        self,
        targets: Sequence[TargetCompany],
        start_year: int,
        end_year: int,
        max_pages: int,
        page_size: int,
        max_items: int,
    ) -> List[DownloadRecord]:
        records: List[DownloadRecord] = []
        for target in targets:
            items = self._crawl_eastmoney_stock(target, start_year, end_year, max_pages, page_size)
            for item in items[:max_items]:
                saved = save_binary(item["file_url"], self.http, STOCK_RESEARCH_DIR, target.code, item["title"], item["published_at"])
                records.append(
                    DownloadRecord(
                        source="eastmoney:stock",
                        code=target.code,
                        name=target.name,
                        title=item["title"],
                        published_at=item["published_at"],
                        page_url=item["page_url"],
                        file_url=item["file_url"],
                        saved_path=str(saved),
                    )
                )
        return dedupe_records(records)

    def crawl_category_research(
        self,
        category: str,
        output_dir: Path,
        max_pages: int,
        max_items: int,
        keyword: str,
    ) -> List[DownloadRecord]:
        category_urls = {
            "industry": "https://data.eastmoney.com/report/industry.jshtml",
            "macro": "https://data.eastmoney.com/report/macresearch.jshtml",
        }
        detail_key = {
            "industry": "zw_industry.jshtml",
            "macro": "zw_macresearch.jshtml",
        }
        seed_url = category_urls[category]
        expected = detail_key[category]
        detail_urls: List[str] = []
        for page in range(1, max_pages + 1):
            page_url = seed_url if page == 1 else f"{seed_url}?page={page}"
            html = self.http.text(page_url, headers={"Referer": seed_url})
            for url in extract_detail_urls(html, expected):
                if url not in detail_urls:
                    detail_urls.append(url)
            if not detail_urls:
                for row in extract_inline_report_rows(html):
                    detail_url = build_eastmoney_category_detail_url(expected, row)
                    if detail_url and detail_url not in detail_urls:
                        detail_urls.append(detail_url)
            if len(detail_urls) >= max_items:
                break
        records: List[DownloadRecord] = []
        for detail_url in detail_urls[:max_items]:
            detail_html = self.http.text(detail_url, headers={"Referer": seed_url})
            title = extract_title(detail_html) or detail_url.rsplit("/", 1)[-1]
            if not matches_keywords(" ".join([title, strip_tags(detail_html)]), keyword):
                continue
            file_url = extract_pdf_url(detail_html)
            if not file_url:
                continue
            published_at = extract_date(detail_html) or ""
            saved = save_binary(file_url, self.http, output_dir, category, title, published_at)
            records.append(
                DownloadRecord(
                    source=f"eastmoney:{category}",
                    code="",
                    name="",
                    title=title,
                    published_at=published_at,
                    page_url=detail_url,
                    file_url=file_url,
                    saved_path=str(saved),
                )
            )
        return dedupe_records(records)

    def _crawl_sse(
        self,
        target: TargetCompany,
        start_year: int,
        end_year: int,
        max_pages: int,
        page_size: int,
    ) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        seen: Set[str] = set()
        url = "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
        for page in range(1, max_pages + 1):
            params = {
                "jsonCallBack": f"jsonpCallback{int(time.time() * 1000)}{page}",
                "isPagination": "true",
                "productId": target.code,
                "keyWord": "",
                "securityType": "0101,120100,020100,020200,120200",
                "reportType2": "DQBG",
                "beginDate": f"{start_year}-01-01",
                "endDate": f"{end_year}-12-31",
                "pageHelp.pageSize": page_size,
                "pageHelp.pageCount": 50,
                "pageHelp.pageNo": page,
                "pageHelp.beginPage": page,
                "pageHelp.endPage": page,
                "pageHelp.cacheSize": 1,
                "_": int(time.time() * 1000),
            }
            text = self.http.text(url, params=params, headers={"Referer": "https://www.sse.com.cn/disclosure/listedinfo/regular/"})
            data = parse_json_text(text)
            rows = collect_records(data)
            page_added = 0
            for row in rows:
                title = pick_value(row, "TITLE", "title", "bulletinTitle")
                pdf_url = pick_value(row, "URL", "url", "bulletinUrl")
                if not title or not pdf_url:
                    continue
                pdf_url = normalize_url(str(pdf_url), "https://www.sse.com.cn")
                if pdf_url in seen:
                    continue
                seen.add(pdf_url)
                results.append(
                    {
                        "title": str(title),
                        "page_url": pdf_url,
                        "file_url": pdf_url,
                        "published_at": str(pick_value(row, "SSEDATE", "publishTime", "publishDate") or ""),
                    }
                )
                page_added += 1
            if page_added == 0:
                break
        return results

    def _crawl_szse(self, target: TargetCompany, start_year: int, end_year: int, max_pages: int, page_size: int) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        seen: Set[str] = set()
        url = "https://www.szse.cn/api/disc/announcement/annList"
        for page in range(1, max_pages + 1):
            json_data = {
                "seDate": [f"{start_year}-01-01", f"{end_year}-12-31"],
                "channelCode": ["fixed_disc"],
                "stock": [target.code],
                "pageSize": page_size,
                "pageNum": page,
            }
            text = self.http.text(
                url,
                json_data=json_data,
                headers={
                    "Referer": f"https://www.szse.cn/disclosure/listed/fixed/index.html?stock={target.code}",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
            )
            data = parse_json_text(text)
            rows = collect_records(data)
            page_added = 0
            for row in rows:
                title = pick_value(row, "title", "announcementTitle", "annTitle")
                attach = pick_value(row, "attachPath", "attachpath", "attachUrl", "annPath")
                if not title or not attach:
                    continue
                if "报告" not in str(title):
                    continue
                pdf_url = normalize_szse_attach(str(attach))
                if pdf_url in seen:
                    continue
                seen.add(pdf_url)
                results.append(
                    {
                        "title": str(title),
                        "page_url": f"https://www.szse.cn/disclosure/listed/fixed/index.html?stock={target.code}",
                        "file_url": pdf_url,
                        "published_at": str(pick_value(row, "publishTime", "publishDate") or ""),
                    }
                )
                page_added += 1
            if page_added == 0:
                break
        return results

    def _crawl_bse(self, target: TargetCompany) -> List[Dict[str, str]]:
        urls = [
            f"https://www.bse.cn/products/neeq_listed_companies/related_announcement.html?companyCode={target.code}",
            "https://www.bse.cn/disclosure/announcement.html",
        ]
        results: List[Dict[str, str]] = []
        seen: Set[str] = set()
        for page_url in urls:
            html = self.http.text(page_url, headers={"Referer": "https://www.bse.cn/disclosure/announcement.html"})
            for pdf_url in extract_pdf_links(html, "https://www.bse.cn"):
                if pdf_url in seen:
                    continue
                seen.add(pdf_url)
                name = Path(urllib.parse.urlparse(pdf_url).path).name
                results.append(
                    {
                        "title": name,
                        "page_url": page_url,
                        "file_url": pdf_url,
                        "published_at": extract_date(pdf_url) or "",
                    }
                )
        return results

    def _crawl_eastmoney_stock(
        self,
        target: TargetCompany,
        start_year: int,
        end_year: int,
        max_pages: int,
        page_size: int,
    ) -> List[Dict[str, str]]:
        results: List[Dict[str, str]] = []
        seen: Set[str] = set()
        api_url = "https://reportapi.eastmoney.com/report/list"
        for page in range(1, max_pages + 1):
            params = {
                "cb": "datatable123",
                "industryCode": "*",
                "pageNo": page,
                "pageSize": page_size,
                "industry": "*",
                "ratingChange": "",
                "fields": "",
                "qType": 0,
                "rcode": "",
                "p": 1,
                "pageNum": page,
                "_": int(time.time() * 1000),
                "code": target.code,
                "orgCode": "",
                "rating": "",
                "beginTime": f"{start_year}-01-01",
                "endTime": f"{end_year}-12-31",
            }
            try:
                text = self.http.text(api_url, params=params, headers={"Referer": "https://data.eastmoney.com/report/stock.jshtml"})
            except Exception:
                break
            data = parse_json_text(text)
            rows = collect_records(data)
            page_added = 0
            for row in rows:
                detail_url = build_eastmoney_stock_detail_url(row)
                if not detail_url or detail_url in seen:
                    continue
                seen.add(detail_url)
                detail_html = self.http.text(detail_url, headers={"Referer": "https://data.eastmoney.com/report/stock.jshtml"})
                file_url = extract_pdf_url(detail_html)
                if not file_url:
                    continue
                title = pick_value(row, "title", "reportTitle", "art_title") or extract_title(detail_html) or detail_url
                results.append(
                    {
                        "title": str(title),
                        "page_url": detail_url,
                        "file_url": file_url,
                        "published_at": str(pick_value(row, "publishDate", "publishTime", "reportDate") or extract_date(detail_html) or ""),
                    }
                )
                page_added += 1
            if page_added == 0:
                break
        if results:
            return results
        seed_url = f"https://data.eastmoney.com/report/stock.jshtml?code={target.code}"
        html = self.http.text(seed_url, headers={"Referer": "https://data.eastmoney.com/report/stock.jshtml"})
        for detail_url in extract_detail_urls(html, "zw_stock.jshtml"):
            if detail_url in seen:
                continue
            seen.add(detail_url)
            detail_html = self.http.text(detail_url, headers={"Referer": seed_url})
            file_url = extract_pdf_url(detail_html)
            if not file_url:
                continue
            title = extract_title(detail_html) or detail_url
            results.append(
                {
                    "title": title,
                    "page_url": detail_url,
                    "file_url": file_url,
                    "published_at": extract_date(detail_html) or "",
                }
            )
        return results


def pick_value(row: Dict[str, Any], *names: str) -> Any:
    for name in names:
        key = name.lower()
        for raw_key, value in row.items():
            if str(raw_key).lower() == key and value not in (None, ""):
                return value
    return None


def parse_json_text(text: str) -> Any:
    raw = text.strip()
    if not raw:
        return {}
    if raw.startswith("{") or raw.startswith("["):
        return json.loads(raw)
    match = re.search(r"\((\{.*\}|\[.*\])\)\s*;?\s*$", raw, flags=re.S)
    if match:
        return json.loads(match.group(1))
    first_obj = raw.find("{")
    last_obj = raw.rfind("}")
    if first_obj != -1 and last_obj != -1 and first_obj < last_obj:
        return json.loads(raw[first_obj:last_obj + 1])
    first_arr = raw.find("[")
    last_arr = raw.rfind("]")
    if first_arr != -1 and last_arr != -1 and first_arr < last_arr:
        return json.loads(raw[first_arr:last_arr + 1])
    return {}


def collect_records(data: Any) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            keys = {str(k).lower() for k in node.keys()}
            if {"title", "url"} <= keys or "attachpath" in keys or "encodeurl" in keys or "infocode" in keys:
                records.append(node)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(data)
    return records


def normalize_url(url: str, base: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    return urllib.parse.urljoin(base, url)


def normalize_szse_attach(path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    clean = path if path.startswith("/") else f"/{path}"
    return f"https://disc.static.szse.cn/download{clean}"


def extract_pdf_links(text: str, base: str) -> List[str]:
    urls = []
    for match in re.findall(r"(?:https?:)?//[^\s\"'<>]+?\.pdf(?:\?[^\s\"'<>]*)?", text, flags=re.I):
        urls.append(normalize_url(match, base))
    for match in re.findall(r"(?:href=)?[\"'](/[^\"']+?\.pdf(?:\?[^\"']*)?)[\"']", text, flags=re.I):
        urls.append(normalize_url(match, base))
    deduped: List[str] = []
    seen: Set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def extract_detail_urls(text: str, marker: str) -> List[str]:
    found: List[str] = []
    patterns = [
        rf"https?://data\.eastmoney\.com/report/{re.escape(marker)}[^\s\"'<>)]*",
        rf"//data\.eastmoney\.com/report/{re.escape(marker)}[^\s\"'<>)]*",
        rf"/report/{re.escape(marker)}[^\s\"'<>)]*",
    ]
    for pattern in patterns:
        for value in re.findall(pattern, unescape(text), flags=re.I):
            found.append(normalize_url(value, "https://data.eastmoney.com"))
    deduped: List[str] = []
    seen: Set[str] = set()
    for url in found:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def build_eastmoney_stock_detail_url(row: Dict[str, Any]) -> str:
    encode_url = pick_value(row, "encodeUrl", "encodeurl")
    if encode_url:
        value = str(encode_url)
        if "%" not in value:
            value = urllib.parse.quote(value, safe="")
        return f"https://data.eastmoney.com/report/zw_stock.jshtml?encodeUrl={value}"
    info_code = pick_value(row, "infoCode", "infocode")
    if info_code:
        return f"https://data.eastmoney.com/report/zw_stock.jshtml?infocode={info_code}"
    page_url = pick_value(row, "url", "pageUrl")
    return normalize_url(str(page_url), "https://data.eastmoney.com") if page_url else ""


def build_eastmoney_category_detail_url(detail_page: str, row: Dict[str, Any]) -> str:
    encode_url = pick_value(row, "encodeUrl", "encodeurl")
    if encode_url:
        value = str(encode_url)
        if "%" not in value:
            value = urllib.parse.quote(value, safe="")
        return f"https://data.eastmoney.com/report/{detail_page}?encodeUrl={value}"
    info_code = pick_value(row, "infoCode", "infocode")
    if info_code:
        return f"https://data.eastmoney.com/report/{detail_page}?infocode={info_code}"
    return ""


def extract_inline_report_rows(text: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for match in re.finditer(r"\{[^{}]*\"encodeUrl\"\s*:\s*\"[^\"]+\"[^{}]*\}", text):
        raw = match.group(0)
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        key = str(pick_value(row, "encodeUrl", "infoCode") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def extract_title(text: str) -> str:
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, flags=re.I | re.S)
    if h1_match:
        return clean_text(h1_match.group(1))
    title_match = re.search(r"<title>(.*?)</title>", text, flags=re.I | re.S)
    if title_match:
        return clean_text(title_match.group(1).split("_")[0])
    md_match = re.search(r"#\s*(.+)", text)
    if md_match:
        return clean_text(md_match.group(1))
    return ""


def extract_pdf_url(text: str) -> str:
    links = extract_pdf_links(text, "https://data.eastmoney.com")
    if links:
        return links[0]
    return ""


def extract_date(text: str) -> str:
    match = re.search(r"(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})", text)
    if not match:
        return ""
    value = match.group(1).replace("年", "-").replace("月", "-").replace("日", "")
    value = value.replace("/", "-").replace(".", "-")
    return value


def clean_text(text: str) -> str:
    return strip_tags(unescape(text)).replace("\u3000", " ").strip()


def matches_keywords(text: str, keyword: str) -> bool:
    if not keyword:
        return True
    candidates = [item.strip() for item in re.split(r"[,，;；\s]+", keyword) if item.strip()]
    if not candidates:
        return True
    text_lower = text.lower()
    return any(item.lower() in text_lower for item in candidates)


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def sanitize_filename(text: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", text)
    clean = re.sub(r"\s+", " ", clean).strip(" ._")
    return clean[:150] or "file"


def output_dir_for_exchange(exchange: str) -> Path:
    if exchange == "sse":
        return SSE_DIR
    if exchange == "szse":
        return SZSE_DIR
    return BSE_DIR


def save_binary(url: str, downloader: Downloader, output_dir: Path, code: str, title: str, published_at: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    date_prefix = published_at[:10].replace("/", "-").replace(".", "-") if published_at else "undated"
    prefix = sanitize_filename(code) if code else "report"
    file_name = sanitize_filename(f"{prefix}_{date_prefix}_{title}") + ".pdf"
    path = output_dir / file_name
    if not path.exists():
        path.write_bytes(downloader.binary(url, headers={"Referer": url}))
    return path


def dedupe_records(records: Iterable[DownloadRecord]) -> List[DownloadRecord]:
    result: List[DownloadRecord] = []
    seen: Set[str] = set()
    for record in records:
        key = record.file_url or record.saved_path
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def guess_exchange(code: str) -> str:
    if not code:
        return ""
    if code.startswith(("60", "68", "90")):
        return "sse"
    if code.startswith(("00", "20", "30", "0")):
        return "szse"
    if code.startswith(("4", "8", "9")):
        return "bse"
    return ""


def load_targets(args: argparse.Namespace) -> List[TargetCompany]:
    targets: List[TargetCompany] = []
    if args.codes:
        for code_str in [item.strip() for item in args.codes.split(",") if item.strip()]:
            code = normalize_code(code_str)
            if code:
                targets.append(TargetCompany(code=code, exchange=guess_exchange(code)))
    company_file = Path(args.company_file) if args.company_file else DEFAULT_COMPANY_FILE
    if company_file.exists() and not args.codes:
        df = read_company_frame(company_file)
        if args.keyword:
            keyword = args.keyword.strip()
            df = df[df.astype(str).apply(lambda row: row.str.contains(keyword, case=False, na=False).any(), axis=1)]
        if args.limit:
            df = df.head(args.limit)
        for _, row in df.iterrows():
            code = normalize_code(row.get("股票代码") or row.get("证券代码") or row.get("code") or "")
            if not code:
                continue
            name = str(row.get("A股简称") or row.get("证券简称") or row.get("公司名称") or "")
            exchange_text = str(row.get("上市交易所") or row.get("交易所") or "")
            exchange = map_exchange_text(exchange_text) or guess_exchange(code)
            targets.append(TargetCompany(code=code, name=name, exchange=exchange))
    deduped: List[TargetCompany] = []
    seen: Set[str] = set()
    for target in targets:
        if target.code in seen:
            continue
        seen.add(target.code)
        deduped.append(target)
    return deduped


def read_company_frame(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".csv", ".txt"}:
        return pd.read_csv(path)
    return pd.read_excel(path)


def normalize_code(value: Any) -> str:
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return ""
    digits = re.sub(r"\D", "", text)
    if not digits:
        return ""
    return digits.zfill(6)[-6:]


def map_exchange_text(text: str) -> str:
    if "上海" in text or "上交所" in text:
        return "sse"
    if "深圳" in text or "深交所" in text:
        return "szse"
    if "北京" in text or "北交所" in text:
        return "bse"
    return ""


def exchange_text(exchange: str) -> str:
    if exchange == "sse":
        return "上海证券交易所"
    if exchange == "szse":
        return "深圳证券交易所"
    if exchange == "bse":
        return "北京证券交易所"
    return ""


def write_company_pool(path: Path, targets: Sequence[TargetCompany]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(
        [
            {
                "股票代码": item.code,
                "A股简称": item.name,
                "上市交易所": exchange_text(item.exchange),
            }
            for item in targets
        ]
    )
    df.to_csv(path, index=False, encoding="utf-8-sig")


def write_index(path: Path, records: Sequence[DownloadRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--codes", default="")
        sub.add_argument("--company-file", default=str(DEFAULT_COMPANY_FILE))
        sub.add_argument("--keyword", default="")
        sub.add_argument("--limit", type=int, default=0)
        sub.add_argument("--start-year", type=int, default=2022)
        sub.add_argument("--end-year", type=int, default=2026)
        sub.add_argument("--max-pages", type=int, default=3)
        sub.add_argument("--page-size", type=int, default=30)
        sub.add_argument("--max-items", type=int, default=20)

    exchange = subparsers.add_parser("exchange-reports")
    add_common(exchange)

    stock = subparsers.add_parser("stock-research")
    add_common(stock)

    industry = subparsers.add_parser("industry-research")
    industry.add_argument("--keyword", default="医药")
    industry.add_argument("--max-pages", type=int, default=2)
    industry.add_argument("--max-items", type=int, default=20)

    macro = subparsers.add_parser("macro-research")
    macro.add_argument("--keyword", default="")
    macro.add_argument("--max-pages", type=int, default=2)
    macro.add_argument("--max-items", type=int, default=20)

    discover = subparsers.add_parser("discover-companies")
    discover.add_argument("--keyword", default="医药")
    discover.add_argument("--start-year", type=int, default=2023)
    discover.add_argument("--end-year", type=int, default=2026)
    discover.add_argument("--max-pages", type=int, default=30)
    discover.add_argument("--page-size", type=int, default=50)
    discover.add_argument("--limit", type=int, default=200)
    discover.add_argument("--output", default=str(EXPANDED_COMPANY_FILE))

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    crawler = MarketCrawler(Downloader())

    if args.command == "exchange-reports":
        targets = load_targets(args)
        if not targets:
            raise SystemExit("未找到可抓取的公司代码")
        records = crawler.crawl_exchange_reports(targets, args.start_year, args.end_year, args.max_pages, args.page_size, args.max_items)
        write_index(REPORT_ROOT / "downloads.exchange.json", records)
    elif args.command == "stock-research":
        targets = load_targets(args)
        if not targets:
            raise SystemExit("未找到可抓取的公司代码")
        records = crawler.crawl_stock_research(targets, args.start_year, args.end_year, args.max_pages, args.page_size, args.max_items)
        write_index(RESEARCH_ROOT / "downloads.stock.json", records)
    elif args.command == "industry-research":
        records = crawler.crawl_category_research("industry", INDUSTRY_RESEARCH_DIR, args.max_pages, args.max_items, args.keyword)
        write_index(RESEARCH_ROOT / "downloads.industry.json", records)
    elif args.command == "discover-companies":
        targets = crawler.discover_targets_from_reports(args.keyword, args.start_year, args.end_year, args.max_pages, args.page_size, args.limit)
        write_company_pool(Path(args.output), targets)
        print(json.dumps([asdict(item) for item in targets], ensure_ascii=False, indent=2))
        return
    else:
        records = crawler.crawl_category_research("macro", MACRO_RESEARCH_DIR, args.max_pages, args.max_items, args.keyword)
        write_index(RESEARCH_ROOT / "downloads.macro.json", records)

    print(json.dumps([asdict(item) for item in records], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
