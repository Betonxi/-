"""
共享模块：LLM客户端、知识库引擎、数据库工具、可视化工具。
供 task2/run_task2.py、task3/run_task3.py、integrated_chat.py 共同引用。
"""
import json
import pickle
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

from config import LLM_API_BASE, LLM_API_KEY, LLM_MODEL, LLM_MAX_RETRIES, LLM_TIMEOUT

# ---------------------------------------------------------------------------
# 全局配置（从 config.py 导入）
# ---------------------------------------------------------------------------
DEFAULT_API_BASE = LLM_API_BASE
DEFAULT_API_KEY = LLM_API_KEY
DEFAULT_MODEL = LLM_MODEL

# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class SubTask:
    id: int
    task_type: str  # 'sql' or 'kb'
    description: str
    query: str
    result: Any = None
    status: str = "pending"


@dataclass
class TaskResult:
    """Task 3 / integrated_chat 使用的结果结构"""
    content: str
    references: List[Dict[str, str]] = field(default_factory=list)
    sql: str = ""
    chart_type: str = "无"
    images: List[str] = field(default_factory=list)


@dataclass
class TurnResult:
    """Task 2 使用的单轮结果"""
    content: str
    sql: str = ""
    chart_type: str = "无"
    images: List[str] = field(default_factory=list)


@dataclass
class SessionState:
    stock_abbr: Optional[str] = None
    report_period: Optional[str] = None
    metric: Optional[str] = None
    last_sql: Optional[str] = None
    last_result_count: int = 0
    last_answer: str = ""

# ---------------------------------------------------------------------------
# LLM 客户端
# ---------------------------------------------------------------------------

class LLMClient:
    def __init__(self, base_url: str = DEFAULT_API_BASE,
                 api_key: str = DEFAULT_API_KEY,
                 model: str = DEFAULT_MODEL):
        if OpenAI is None:
            raise RuntimeError("未安装 openai 库，请先执行 pip install openai")
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model

    def complete(self, prompt: str, stream: bool = False,
                 temperature: float = 0.3, max_tokens: int = 8000,
                 on_token: Optional[Callable[[str, str], None]] = None) -> str:
        last_err = None
        for attempt in range(LLM_MAX_RETRIES):
            try:
                return self._do_complete(prompt, stream, temperature, max_tokens, on_token)
            except Exception as e:
                last_err = e
                wait = min(2 ** attempt, 8)
                print(f"[LLM] 第{attempt+1}次请求失败: {e}，{wait}s后重试", flush=True)
                time.sleep(wait)
        return f"[LLM错误] {last_err}"

    def _do_complete(self, prompt: str, stream: bool,
                     temperature: float, max_tokens: int,
                     on_token: Optional[Callable[[str, str], None]] = None) -> str:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            stream=stream,
            timeout=LLM_TIMEOUT,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=0.8,
        )
        if stream:
            full_text = ""
            has_reasoning = False
            for chunk in resp:
                if not chunk.choices:
                    continue
                if hasattr(chunk.choices[0].delta, "reasoning_content") and chunk.choices[0].delta.reasoning_content:
                    reasoning = chunk.choices[0].delta.reasoning_content
                    if on_token is None and not has_reasoning:
                        print("\n> [思考过程] ", end="", flush=True)
                        has_reasoning = True
                    if on_token is None:
                        print(reasoning, end="", flush=True)
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    if on_token is None and has_reasoning:
                        print("\n\n", end="", flush=True)
                        has_reasoning = False
                    if on_token is None:
                        print(delta, end="", flush=True)
                    full_text += delta
                    if on_token is not None:
                        on_token(delta, full_text)
            if on_token is None:
                print()
            return full_text.strip()
        if not resp.choices:
            return ""
        return (resp.choices[0].message.content or "").strip()

    def complete_json(self, prompt: str, stream: bool = False,
                      temperature: float = 0.1) -> Any:
        txt = self.complete(prompt, stream=stream, temperature=temperature)
        try:
            clean = re.sub(r"```json\s*", "", txt)
            clean = re.sub(r"```\s*", "", clean).strip()
            indices = [i for i in [clean.find("{"), clean.find("[")] if i != -1]
            start = min(indices) if indices else 0
            end_indices = [i for i in [clean.rfind("}"), clean.rfind("]")] if i != -1]
            end = max(end_indices) + 1 if end_indices else len(clean)
            return json.loads(clean[start:end])
        except Exception as e:
            print(f"[JSON解析失败] {e}\n原文: {txt[:200]}", flush=True)
            clean = re.sub(r"```json\s*", "", txt)
            clean = re.sub(r"```\s*", "", clean).strip()
            obj_pos = clean.find("{")
            arr_pos = clean.find("[")
            if obj_pos != -1 and (arr_pos == -1 or obj_pos < arr_pos):
                return {}
            if arr_pos != -1 and (obj_pos == -1 or arr_pos < obj_pos):
                return []
            return {}

# ---------------------------------------------------------------------------
# 知识库引擎 —— PDF分块 + TF-IDF向量检索
# ---------------------------------------------------------------------------

def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 100) -> List[str]:
    """将长文本切分为重叠片段"""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start += chunk_size - overlap
    return chunks


def build_vector_kb(kb_json_path: Path, index_path: Path) -> None:
    """从 kb.json 构建 TF-IDF 向量索引"""
    from sklearn.feature_extraction.text import TfidfVectorizer

    with open(kb_json_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    chunks: List[Dict[str, str]] = []
    for doc in raw_data:
        source = doc["source"]
        doc_type = doc["type"]
        text = doc["content"]
        if not text.strip():
            continue
        for c in _chunk_text(text):
            chunks.append({"source": source, "type": doc_type, "text": c})

    if not chunks:
        print("[KB] 无有效分块，跳过索引构建。")
        return

    texts = [c["text"] for c in chunks]
    vectorizer = TfidfVectorizer(
        analyzer="char_wb", ngram_range=(2, 4), max_features=8000
    )
    tfidf_matrix = vectorizer.fit_transform(texts)

    index_data = {
        "chunks": chunks,
        "vectorizer": vectorizer,
        "tfidf_matrix": tfidf_matrix,
    }
    with open(index_path, "wb") as f:
        pickle.dump(index_data, f)
    print(f"[KB] 向量索引已构建: {len(chunks)} 个分块 -> {index_path}")


class KBEngine:
    """知识库检索引擎，支持 TF-IDF 向量检索，自动降级为关键词匹配"""

    def __init__(self, kb_path: Path, index_path: Optional[Path] = None):
        self.chunks: List[Dict[str, str]] = []
        self.vectorizer = None
        self.tfidf_matrix = None

        # 尝试加载向量索引
        if index_path is None:
            index_path = kb_path.with_suffix(".index.pkl")
        if index_path.exists():
            try:
                with open(index_path, "rb") as f:
                    idx = pickle.load(f)
                self.chunks = idx["chunks"]
                self.vectorizer = idx["vectorizer"]
                self.tfidf_matrix = idx["tfidf_matrix"]
                return
            except Exception as e:
                print(f"[KB] 索引加载失败 ({e})，降级为关键词匹配")

        # 降级：加载原始 JSON
        if kb_path.exists():
            with open(kb_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for doc in raw:
                for c in _chunk_text(doc["content"]):
                    self.chunks.append({"source": doc["source"], "type": doc["type"], "text": c})

    def _retrieve(self, query: str, top_k: int = 5) -> List[Tuple[float, Dict[str, str]]]:
        """核心检索：返回 [(score, chunk), ...]"""
        if not self.chunks:
            return []
        if self.vectorizer is not None and self.tfidf_matrix is not None:
            from sklearn.metrics.pairwise import cosine_similarity
            q_vec = self.vectorizer.transform([query])
            sims = cosine_similarity(q_vec, self.tfidf_matrix).flatten()
            top_idx = sims.argsort()[::-1][:top_k]
            return [(sims[i], self.chunks[i]) for i in top_idx if sims[i] > 0.01]
        # 关键词降级
        keywords = [k for k in re.split(r'[\s,，;；、]+', query) if k]
        scored = []
        for c in self.chunks:
            score = sum(1 for kw in keywords if kw.lower() in c["text"].lower())
            if score > 0:
                scored.append((float(score), c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    @staticmethod
    def _ref_path(chunk: Dict[str, str]) -> str:
        """根据 chunk type 生成正确的参考文献路径"""
        t = chunk["type"]
        src = chunk["source"]
        if t == "stock_report":
            return f"./附件5：研报数据/个股研报/{src}"
        if t == "industry_report":
            return f"./附件5：研报数据/行业研报/{src}"
        if t == "macro_report":
            return f"./附件5：研报数据/宏观研报/{src}"
        # report_info (xlsx) 位于研报数据根目录
        return f"./附件5：研报数据/{src}"

    def _expand_query(self, query: str) -> str:
        """混合检索：原始查询 + 关键词拆分，提升召回率"""
        keywords = re.split(r'[\s,，;；、]+', query)
        # 拼接原始查询和拆分关键词，让 TF-IDF 同时匹配
        return " ".join([query] + [k for k in keywords if len(k) >= 2])

    def search(self, query: str, top_k: int = 5) -> str:
        expanded = self._expand_query(query)
        results = self._retrieve(expanded, top_k)
        if not results:
            return "知识库中未找到相关内容。"
        context = ""
        for _, chunk in results:
            context += f"来源: {chunk['source']}\n内容: {chunk['text']}\n\n"
        return context

    def search_with_refs(self, query: str, top_k: int = 5) -> Tuple[str, List[Dict[str, str]]]:
        """检索并返回 (拼接文本, references列表)"""
        expanded = self._expand_query(query)
        results = self._retrieve(expanded, top_k)
        if not results:
            return "知识库中未找到相关内容。", []
        context = ""
        refs: List[Dict[str, str]] = []
        seen_sources: set = set()
        for _, chunk in results:
            context += f"来源: {chunk['source']}\n内容: {chunk['text']}\n\n"
            src = chunk["source"]
            if src not in seen_sources:
                seen_sources.add(src)
                refs.append({
                    "paper_path": self._ref_path(chunk),
                    "text": chunk["text"][:200],
                    "paper_image": ""
                })
        return context, refs

# ---------------------------------------------------------------------------
# 数据库工具
# ---------------------------------------------------------------------------

def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def schema_text(conn: sqlite3.Connection) -> str:
    tables = {
        "core_performance_indicators_sheet": "核心指标表（EPS、ROE、每股净资产、毛利率等）",
        "balance_sheet": "资产负债表",
        "cash_flow_sheet": "现金流量表",
        "income_sheet": "利润表（营业收入、营业成本、利润总额 total_profit、净利润等）"
    }
    out = []
    for t, desc in tables.items():
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")]
        out.append(f"表名: {t} ({desc})\n列名: {', '.join(cols)}")
    out.append("""
重要列名映射：
- "销售额/营业收入/主营业务收入" → total_operating_revenue
- "利润总额" → total_profit (income_sheet)
- "净利润" → net_profit (income_sheet) 或 net_profit_10k_yuan (KPI表,万元)
- report_period 格式: 2024FY(年度), 2024Q1(一季度), 2024HY(半年度), 2024Q3(三季度)
- 查年度趋势用 report_period LIKE '%FY'
""")
    return "\n\n".join(out)

# ---------------------------------------------------------------------------
# 可视化工具 —— 美化版
# ---------------------------------------------------------------------------

# 现代配色方案
_COLORS = ["#4E79A7", "#F28E2B", "#E15759", "#76B7B2", "#59A14F",
           "#EDC948", "#B07AA1", "#FF9DA7", "#9C755F", "#BAB0AC"]


def _get_cn_fp():
    """返回可用的中文 FontProperties，找不到则返回 None。"""
    import os
    from matplotlib.font_manager import FontProperties
    for fname in ["msyh.ttc", "simhei.ttf", "msyhbd.ttc"]:
        fpath = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts", fname)
        if os.path.isfile(fpath):
            return FontProperties(fname=fpath)
    return None

def _set_font() -> None:
    matplotlib.rcParams["axes.unicode_minus"] = False
    fp = _get_cn_fp()
    if fp:
        from matplotlib import font_manager
        font_manager.fontManager.addfont(fp.get_file())
        matplotlib.rcParams["font.sans-serif"] = [fp.get_name(), "DejaVu Sans"]
    else:
        matplotlib.rcParams["font.sans-serif"] = [
            "Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"
        ]


def _fmt_value(v: float) -> str:
    """格式化数值标签"""
    if abs(v) >= 1e4:
        return f"{v/1e4:.1f}亿"
    if abs(v) >= 1:
        return f"{v:,.0f}"
    return f"{v:.2f}"


def plot_auto(df: pd.DataFrame, qid: str, seq: int, result_dir: Path,
              chart_pref: str = "") -> Tuple[List[str], str]:
    """自动选择图表类型并生成美化图表"""
    if df.empty or len(df.columns) < 2:
        return [], "无" if df.empty else "表格"

    x_col, y_col = df.columns[0], df.columns[1]
    y = pd.to_numeric(df[y_col], errors="coerce")
    if y.notna().sum() == 0:
        return [], "表格"

    fig, ax = plt.subplots(figsize=(9, 5), dpi=150)

    x_labels = df[x_col].astype(str).tolist()
    y_vals = y.fillna(0).tolist()
    n = len(x_labels)

    # 自动选择图表类型
    is_time = any(k in x_col.lower() for k in ["period", "year", "日期", "时间", "报告期"])
    chart = chart_pref or ("折线图" if is_time else "柱状图")

    if "饼" in chart and n <= 8 and all(v >= 0 for v in y_vals):
        colors = _COLORS[:n]
        wedges, texts, autotexts = ax.pie(
            y_vals, labels=x_labels, autopct="%1.1f%%",
            colors=colors, startangle=90,
            textprops={"fontsize": 10}
        )
        for at in autotexts:
            at.set_fontsize(9)
            at.set_fontweight("bold")
        chart = "饼图"

    elif "折线" in chart:
        ax.plot(range(n), y_vals, marker="o", linewidth=2.2,
                markersize=7, color=_COLORS[0], markerfacecolor="white",
                markeredgewidth=2, markeredgecolor=_COLORS[0])
        for i, v in enumerate(y_vals):
            if v != 0:
                ax.annotate(_fmt_value(v), (i, v), textcoords="offset points",
                            xytext=(0, 10), ha="center", fontsize=8, color="#333")
        ax.set_xticks(range(n))
        ax.set_xticklabels(x_labels, rotation=30, ha="right", fontsize=9)
        ax.fill_between(range(n), y_vals, alpha=0.08, color=_COLORS[0])
        chart = "折线图"

    else:
        bars = ax.bar(range(n), y_vals, width=0.6,
                      color=[_COLORS[i % len(_COLORS)] for i in range(n)],
                      edgecolor="white", linewidth=0.8)
        for bar, v in zip(bars, y_vals):
            if v != 0:
                va = "bottom" if v >= 0 else "top"
                ax.text(bar.get_x() + bar.get_width() / 2, v,
                        _fmt_value(v), ha="center", va=va,
                        fontsize=8, fontweight="bold", color="#333")
        ax.set_xticks(range(n))
        ax.set_xticklabels(x_labels, rotation=30, ha="right", fontsize=9)
        chart = "柱状图"

    # 通用美化
    y_label = y_col.replace("_", " ")
    ax.set_title(f"{y_label}", fontsize=13, fontweight="bold", pad=12)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", alpha=0.3, linestyle="--")
    ax.tick_params(axis="y", labelsize=9)

    fig.tight_layout()
    img_name = f"{qid}_{seq}.jpg"
    img_path = result_dir / img_name
    fig.savefig(img_path, format="jpg", bbox_inches="tight")
    plt.close(fig)
    return [f"./result/{img_name}"], chart
