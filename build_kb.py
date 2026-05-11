from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

import pandas as pd
import pdfplumber

from common import build_vector_kb
from config import KB_INDEX, KB_JSON, ROOT

RESEARCH_ROOT = ROOT / "示例数据" / "附件5：研报数据"
PDF_GROUPS: Sequence[Tuple[str, Path]] = (
    ("stock_report", RESEARCH_ROOT / "个股研报"),
    ("industry_report", RESEARCH_ROOT / "行业研报"),
    ("macro_report", RESEARCH_ROOT / "宏观研报"),
)
EXCEL_FILES: Sequence[str] = (
    "个股_研报信息.xlsx",
    "行业_研报信息.xlsx",
    "字段说明.xlsx",
)
DOWNLOAD_INDEXES: Sequence[Tuple[str, str]] = (
    ("stock_report", "downloads.stock.json"),
    ("industry_report", "downloads.industry.json"),
    ("macro_report", "downloads.macro.json"),
)


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_pdf_text(path: Path) -> str:
    parts: List[str] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                text = clean_text(text)
                if text:
                    parts.append(text)
    except Exception as exc:
        print(f"[KB] 跳过无法解析的PDF: {path.name} ({exc})")
        return ""
    return "\n".join(parts)


def load_pdf_docs() -> List[Dict[str, str]]:
    fallback_meta = load_download_metadata()
    docs: List[Dict[str, str]] = []
    for doc_type, folder in PDF_GROUPS:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.pdf")):
            content = extract_pdf_text(path)
            if not content:
                content = fallback_meta.get((doc_type, path.name), "")
            if not content:
                continue
            docs.append(
                {
                    "type": doc_type,
                    "source": path.name,
                    "content": content,
                }
            )
    return docs


def load_excel_docs() -> List[Dict[str, str]]:
    docs: List[Dict[str, str]] = []
    for filename in EXCEL_FILES:
        path = RESEARCH_ROOT / filename
        if not path.exists():
            continue
        df = pd.read_excel(path).fillna("")
        content = clean_text(df.to_string(index=False))
        if not content:
            continue
        docs.append(
            {
                "type": "report_info",
                "source": path.name,
                "content": content,
            }
        )
    return docs


def load_download_metadata() -> Dict[Tuple[str, str], str]:
    meta: Dict[Tuple[str, str], str] = {}
    for doc_type, filename in DOWNLOAD_INDEXES:
        path = RESEARCH_ROOT / filename
        if not path.exists():
            continue
        records = json.loads(path.read_text(encoding="utf-8"))
        for row in records:
            saved_path = Path(str(row.get("saved_path") or ""))
            if not saved_path.name:
                continue
            pieces = [
                str(row.get("title") or ""),
                str(row.get("name") or ""),
                str(row.get("code") or ""),
                str(row.get("published_at") or ""),
                str(row.get("page_url") or ""),
                str(row.get("file_url") or ""),
            ]
            content = clean_text("\n".join(piece for piece in pieces if piece))
            if content:
                meta[(doc_type, saved_path.name)] = content
    return meta


def build_kb(kb_json_path: Path, kb_index_path: Path) -> List[Dict[str, str]]:
    docs = load_pdf_docs() + load_excel_docs()
    kb_json_path.parent.mkdir(parents=True, exist_ok=True)
    kb_json_path.write_text(json.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")
    build_vector_kb(kb_json_path, kb_index_path)
    return docs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kb-json", default=str(KB_JSON))
    parser.add_argument("--kb-index", default=str(KB_INDEX))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    kb_json_path = Path(args.kb_json)
    kb_index_path = Path(args.kb_index)
    docs = build_kb(kb_json_path, kb_index_path)
    counts: Dict[str, int] = {}
    for doc in docs:
        counts[doc["type"]] = counts.get(doc["type"], 0) + 1
    print(json.dumps({"documents": len(docs), "by_type": counts}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
