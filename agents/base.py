"""
智能体基座：定义所有 Agent 共享的接口、上下文和结果结构。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pandas as pd

# 复用项目已有的共享组件
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import LLMClient, KBEngine


# ---------------------------------------------------------------------------
# 角色枚举
# ---------------------------------------------------------------------------
class Role(str, Enum):
    INVESTOR = "investor"       # 投资者
    MANAGER = "manager"         # 企业管理者
    REGULATOR = "regulator"     # 监管机构

    @property
    def label(self) -> str:
        return {"investor": "投资者", "manager": "企业管理者", "regulator": "监管机构"}[self.value]


# ---------------------------------------------------------------------------
# Agent 上下文 —— 所有 Agent 共享的运行时资源
# ---------------------------------------------------------------------------
@dataclass
class AgentContext:
    """在多个 Agent 之间传递的共享上下文"""
    llm: LLMClient
    conn1: sqlite3.Connection
    conn2: sqlite3.Connection
    kb: KBEngine
    result_dir: Path
    role: Role = Role.INVESTOR
    stock_abbr: Optional[str] = None        # 当前关注公司
    report_period: Optional[str] = None     # 当前关注报告期
    extra: Dict[str, Any] = field(default_factory=dict)
    blackboard: Dict[str, Any] = field(default_factory=dict)  # 多 Agent 共享黑板
    on_phase: Optional[Callable] = None  # (agent_name, detail) -> None

    # ---- 便捷方法 ----
    def query_db(self, sql: str) -> pd.DataFrame:
        """对主库执行 SQL 查询"""
        return pd.read_sql_query(sql, self.conn1)

    def query_both(self, sql: str):
        """双库查询并交叉校验，返回 (df, verification_msg)"""
        df1 = pd.read_sql_query(sql, self.conn1)
        df2 = pd.read_sql_query(sql, self.conn2)
        if df1.equals(df2):
            return df1, "已通过双数据库交叉验证"
        return df1, f"[警告] 双数据库结果不一致！DB1: {len(df1)}行, DB2: {len(df2)}行"


# ---------------------------------------------------------------------------
# 执行链路追踪
# ---------------------------------------------------------------------------
@dataclass
class TraceStep:
    """单步执行记录，用于归因分析和链路展示"""
    agent: str
    action: str          # 如 "intent_classify", "sql_execute", "kb_search"
    detail: str          # 简述该步做了什么
    timestamp: float = 0.0
    duration_ms: float = 0.0
    input_summary: str = ""   # 输入摘要
    output_summary: str = ""  # 输出摘要
    source: str = ""          # 数据来源（表名 / 研报文件名）


# ---------------------------------------------------------------------------
# Agent 结果
# ---------------------------------------------------------------------------
@dataclass
class AgentResult:
    """每个 Agent 返回的统一结果结构"""
    agent_name: str
    success: bool = True
    content: str = ""                                   # 主要文字内容
    data: Optional[pd.DataFrame] = None                 # 结构化数据
    charts: List[str] = field(default_factory=list)     # 图片路径列表
    references: List[Dict[str, str]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    trace: List[TraceStep] = field(default_factory=list)  # 执行链路
    error: str = ""


# ---------------------------------------------------------------------------
# Agent 基类
# ---------------------------------------------------------------------------
class BaseAgent:
    """所有智能体的抽象基类"""
    name: str = "BaseAgent"

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self.llm = ctx.llm
        self._trace: List[TraceStep] = []

    def run(self, query: str, **kwargs) -> AgentResult:
        """子类必须实现：根据 query 执行任务并返回 AgentResult"""
        raise NotImplementedError

    # ---- 追踪工具 ----
    def _trace_start(self, action: str, detail: str = "",
                     input_summary: str = "", source: str = "") -> TraceStep:
        step = TraceStep(
            agent=self.name, action=action, detail=detail,
            timestamp=time.time(), input_summary=input_summary[:200],
            source=source,
        )
        if self.ctx.on_phase:
            self.ctx.on_phase(self.name, detail or action)
        return step

    def _trace_end(self, step: TraceStep, output_summary: str = ""):
        step.duration_ms = (time.time() - step.timestamp) * 1000
        step.output_summary = output_summary[:300]
        self._trace.append(step)

    def _collect_trace(self) -> List[TraceStep]:
        """收集并重置当前 trace"""
        trace = list(self._trace)
        self._trace.clear()
        return trace

    # ---- SQL 安全 ----
    def _safe_sql(self, sql: str) -> str:
        s = sql.strip().rstrip(";").strip()
        if not s.lower().startswith("select"):
            raise ValueError("仅允许 SELECT 查询")
        banned = ["insert ", "update ", "delete ", "drop ", "alter ", "create "]
        if any(b in s.lower() for b in banned):
            raise ValueError("SQL 包含危险语句")
        return s

    def _sql_with_retry(self, sql: str, max_retries: int = 2) -> pd.DataFrame:
        """执行 SQL，失败时调用 LLM 自动纠错重试"""
        last_err = None
        for attempt in range(max_retries + 1):
            try:
                safe = self._safe_sql(sql)
                return self.ctx.query_db(safe)
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    sql = self._fix_sql(sql, str(e))
                    continue
        raise last_err  # type: ignore[misc]

    def _fix_sql(self, sql: str, error: str) -> str:
        """调用 LLM 修复出错的 SQL"""
        prompt = f"""以下 SQL 执行报错，请修复并只输出修复后的 SQL（无别的文字）。
错误: {error}
SQL: {sql}
修复后的SQL:"""
        fixed = self.llm.complete(prompt, temperature=0.0, max_tokens=500)
        fixed = fixed.strip().strip("`").strip()
        if fixed.startswith("sql"):
            fixed = fixed[3:].strip()
        return fixed
