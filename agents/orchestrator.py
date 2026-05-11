"""
Orchestrator —— 编排智能体
负责意图路由、多 Agent 协同调度、结果汇总。
是整个多智能体系统的"大脑"。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentContext, AgentResult, Role, TraceStep
from .query_agent import QueryAgent
from .assessment_agent import AssessmentAgent
from .risk_agent import RiskAgent
from .report_agent import ReportAgent


# ---------------------------------------------------------------------------
# 意图类型
# ---------------------------------------------------------------------------
INTENTS = {
    "query":      "数据查询/智能问数",
    "assessment": "企业运营评估",
    "risk":       "风险与机会洞察",
    "report":     "生成分析报告",
    "compare":    "同业对标分析",
    "chat":       "一般对话/闲聊",
}


class Orchestrator:
    """多智能体编排器"""

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self.llm = ctx.llm
        self.agents: Dict[str, BaseAgent] = {
            "query": QueryAgent(ctx),
            "assessment": AssessmentAgent(ctx),
            "risk": RiskAgent(ctx),
            "report": ReportAgent(ctx),
        }
        self.history: List[Dict[str, str]] = []

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def process(self, user_input: str, **kwargs) -> AgentResult:
        """处理用户输入：意图识别 → Agent 调度 → 结果汇总"""
        orch_trace: List[TraceStep] = []
        stream = bool(kwargs.get("stream", False))
        on_token = kwargs.get("on_token")

        # 1. 意图识别
        t0 = time.time()
        if self.ctx.on_phase:
            self.ctx.on_phase("Orchestrator", "正在分析意图…")
        intent_info = self._classify_intent(user_input)
        intent = intent_info.get("intent", "query")
        stock = intent_info.get("stock") or self.ctx.stock_abbr
        year = intent_info.get("year", 2024)
        orch_trace.append(TraceStep(
            agent="Orchestrator", action="intent_classify",
            detail=f"意图={intent}, 公司={stock}, 年份={year}",
            timestamp=t0, duration_ms=(time.time()-t0)*1000,
            input_summary=user_input[:200],
            output_summary=json.dumps(intent_info, ensure_ascii=False)[:200],
        ))

        if stock:
            self.ctx.stock_abbr = stock

        # 2. 根据意图调度 Agent
        t1 = time.time()
        _intent_label = INTENTS.get(intent, intent)
        if self.ctx.on_phase:
            self.ctx.on_phase("Orchestrator", f"调度 {_intent_label} 智能体…")
        if intent == "assessment":
            result = self._run_assessment(user_input, stock, year, stream=stream, on_token=on_token)
        elif intent == "risk":
            result = self._run_risk(user_input, stock, stream=stream, on_token=on_token)
        elif intent == "report":
            result = self._run_full_report(user_input, stock, year, stream=stream, on_token=on_token)
        elif intent == "compare":
            result = self._run_compare(user_input, year, stream=stream, on_token=on_token)
        elif intent == "chat":
            result = self._run_chat(user_input, stream=stream, on_token=on_token)
        else:
            result = self._run_query(user_input, stock, stream=stream, on_token=on_token)
        orch_trace.append(TraceStep(
            agent="Orchestrator", action="agent_dispatch",
            detail=f"调度 {result.agent_name}",
            timestamp=t1, duration_ms=(time.time()-t1)*1000,
            output_summary=f"success={result.success}",
        ))

        # 3. 合并链路: 编排器链路 + 子Agent链路
        result.trace = orch_trace + result.trace

        # 4. 记录历史
        self.history.append({"role": "user", "content": user_input})
        self.history.append({"role": "assistant", "content": result.content[:500]})

        return result

    # ------------------------------------------------------------------
    # 意图分类
    # ------------------------------------------------------------------
    def _classify_intent(self, user_input: str) -> Dict[str, Any]:
        prompt = f"""你是意图分类专家。判断用户意图并提取关键实体。

可选意图:
- query: 查询具体财务数据（如：华润三九2024年净利润是多少）
- assessment: 评估企业运营状况（如：评估华润三九的运营情况、给华润三九打分）
- risk: 分析风险和机会（如：华润三九有什么风险、医药行业机会）
- report: 生成完整报告（如：生成投资分析报告、出一份诊断报告）
- compare: 两家公司对比（如：对比华润三九和金花股份）
- chat: 一般对话

当前角色: {self.ctx.role.label}
历史上下文: {json.dumps(self.history[-4:], ensure_ascii=False) if self.history else '无'}

用户输入: {user_input}

输出JSON:
{{"intent":"query/assessment/risk/report/compare/chat","stock":"公司简称或null","year":2024,"reason":"判断原因"}}
"""
        return self.llm.complete_json(prompt)

    # ------------------------------------------------------------------
    # Agent 调度策略
    # ------------------------------------------------------------------
    def _run_query(self, query: str, stock: Optional[str], stream: bool = False, on_token=None) -> AgentResult:
        return self.agents["query"].run(query, qid="web", stream=stream, on_token=on_token)

    def _run_assessment(self, query: str, stock: Optional[str], year: int,
                        stream: bool = False, on_token=None) -> AgentResult:
        return self.agents["assessment"].run(query, stock=stock, year=year, stream=stream, on_token=on_token)

    def _run_risk(self, query: str, stock: Optional[str], stream: bool = False, on_token=None) -> AgentResult:
        return self.agents["risk"].run(query, stock=stock, stream=stream, on_token=on_token)

    def _run_compare(self, query: str, year: int, stream: bool = False, on_token=None) -> AgentResult:
        """同业对标：同时评估两家公司"""
        return self.agents["assessment"].run(query, stock=None, year=year, compare=True, stream=stream, on_token=on_token)

    def _run_full_report(self, query: str, stock: Optional[str], year: int,
                         stream: bool = False, on_token=None) -> AgentResult:
        """完整报告：编排多个 Agent 协同工作，共享黑板"""
        sub_traces: List[TraceStep] = []

        # 1. 数据查询
        if self.ctx.on_phase:
            self.ctx.on_phase("Orchestrator", "报告第 1 步：数据查询…")
        query_result = self.agents["query"].run(
            f"{stock or ''}近年主要财务数据概览", qid="report"
        )
        sub_traces.extend(query_result.trace)
        self.ctx.blackboard["query_result"] = query_result.content[:2000]

        # 2. 运营评估
        if self.ctx.on_phase:
            self.ctx.on_phase("Orchestrator", "报告第 2 步：运营评估…")
        assess_result = self.agents["assessment"].run(
            f"评估{stock or '全部公司'}{year}年运营状况",
            stock=stock, year=year,
        )
        sub_traces.extend(assess_result.trace)
        self.ctx.blackboard["assessment_scores"] = assess_result.metadata.get("scores", {})

        # 3. 风险洞察
        if self.ctx.on_phase:
            self.ctx.on_phase("Orchestrator", "报告第 3 步：风险洞察…")
        risk_result = self.agents["risk"].run(
            f"分析{stock or '全部公司'}的风险与机会", stock=stock,
        )
        sub_traces.extend(risk_result.trace)

        # 4. 报告生成
        if self.ctx.on_phase:
            self.ctx.on_phase("Orchestrator", "报告第 4 步：撰写报告…")
        report_result = self.agents["report"].run(
            query,
            agent_results=[query_result, assess_result, risk_result],
            stock=stock,
            stream=stream,
            on_token=on_token,
        )
        report_result.trace = sub_traces + report_result.trace
        return report_result

    def _run_chat(self, query: str, stream: bool = False, on_token=None) -> AgentResult:
        prompt = f"""你是医药生物行业的智能助手。请友好地回答用户问题。
如果问题涉及具体数据，建议用户使用专业功能（运营评估、风险洞察等）。

用户: {query}"""
        content = self.llm.complete(prompt, stream=stream, on_token=on_token)
        return AgentResult(agent_name="ChatAgent", content=content)
