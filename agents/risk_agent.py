"""
RiskAgent —— 风险与机会洞察智能体
基于财务规则引擎 + 研报检索 + LLM 研判，输出风险/机会信号。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import pandas as pd

from .base import BaseAgent, AgentContext, AgentResult

# ---------------------------------------------------------------------------
# 风险规则引擎（基于财务指标的量化规则）
# ---------------------------------------------------------------------------
_RISK_RULES: List[Dict[str, Any]] = [
    {
        "id": "R1", "name": "业绩下滑风险",
        "sql": "SELECT stock_abbr, report_period, net_profit_yoy_growth "
               "FROM core_performance_indicators_sheet "
               "WHERE report_period LIKE '%FY' AND net_profit_yoy_growth < -10 "
               "ORDER BY report_year DESC LIMIT 10",
        "condition": lambda df: not df.empty,
        "level": "高",
        "desc": "净利润年同比下降超过 10%",
    },
    {
        "id": "R2", "name": "营收萎缩风险",
        "sql": "SELECT stock_abbr, report_period, operating_revenue_yoy_growth "
               "FROM core_performance_indicators_sheet "
               "WHERE report_period LIKE '%FY' AND operating_revenue_yoy_growth < -5 "
               "ORDER BY report_year DESC LIMIT 10",
        "condition": lambda df: not df.empty,
        "level": "中",
        "desc": "营收年同比下降超过 5%",
    },
    {
        "id": "R3", "name": "偿债风险",
        "sql": "SELECT stock_abbr, report_period, asset_liability_ratio "
               "FROM balance_sheet "
               "WHERE report_period LIKE '%FY' AND asset_liability_ratio > 60 "
               "ORDER BY report_year DESC LIMIT 10",
        "condition": lambda df: not df.empty,
        "level": "中",
        "desc": "资产负债率超过 60%",
    },
    {
        "id": "R4", "name": "现金流恶化风险",
        "sql": "SELECT stock_abbr, report_period, operating_cf_net_amount, net_cash_flow "
               "FROM cash_flow_sheet "
               "WHERE report_period LIKE '%FY' AND operating_cf_net_amount < 0 "
               "ORDER BY report_year DESC LIMIT 10",
        "condition": lambda df: not df.empty,
        "level": "高",
        "desc": "经营活动现金流为负",
    },
    {
        "id": "R5", "name": "毛利率下行风险",
        "sql": "SELECT stock_abbr, report_period, gross_profit_margin "
               "FROM core_performance_indicators_sheet "
               "WHERE report_period LIKE '%FY' "
               "ORDER BY stock_abbr, report_year",
        "condition": lambda df: _check_declining(df, "gross_profit_margin"),
        "level": "中",
        "desc": "毛利率连续两年下降",
    },
]

_OPPORTUNITY_RULES: List[Dict[str, Any]] = [
    {
        "id": "O1", "name": "盈利修复机会",
        "sql": "SELECT stock_abbr, report_period, net_profit_yoy_growth "
               "FROM core_performance_indicators_sheet "
               "WHERE report_period LIKE '%FY' AND net_profit_yoy_growth > 20 "
               "ORDER BY report_year DESC LIMIT 10",
        "condition": lambda df: not df.empty,
        "level": "高",
        "desc": "净利润同比增长超过 20%",
    },
    {
        "id": "O2", "name": "研发驱动机会",
        "sql": "SELECT stock_abbr, report_period, "
               "operating_expense_rnd_expenses * 100.0 / total_operating_revenue AS rnd_ratio "
               "FROM income_sheet "
               "WHERE report_period LIKE '%FY' AND total_operating_revenue > 0 "
               "AND operating_expense_rnd_expenses * 100.0 / total_operating_revenue > 5 "
               "ORDER BY report_year DESC LIMIT 10",
        "condition": lambda df: not df.empty,
        "level": "中",
        "desc": "研发费用占比超过 5%",
    },
    {
        "id": "O3", "name": "现金流优异",
        "sql": "SELECT c.stock_abbr, c.report_period, "
               "c.operating_cf_net_amount, i.net_profit "
               "FROM cash_flow_sheet c JOIN income_sheet i "
               "ON c.stock_abbr=i.stock_abbr AND c.report_period=i.report_period "
               "WHERE c.report_period LIKE '%FY' AND i.net_profit > 0 "
               "AND c.operating_cf_net_amount > i.net_profit * 1.2 "
               "ORDER BY c.report_year DESC LIMIT 10",
        "condition": lambda df: not df.empty,
        "level": "中",
        "desc": "经营现金流超过净利润 120%，盈利质量高",
    },
]


def _check_declining(df: pd.DataFrame, col: str) -> bool:
    """检查是否有公司的某个指标连续两年下降"""
    for stock, grp in df.groupby("stock_abbr"):
        vals = grp.sort_values("report_period")[col].dropna().tolist()
        if len(vals) >= 3 and vals[-1] < vals[-2] < vals[-3]:
            return True
    return False


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class RiskAgent(BaseAgent):
    name = "RiskAgent"

    def run(self, query: str, *, stock: Optional[str] = None, **kwargs) -> AgentResult:
        try:
            stream = bool(kwargs.get("stream", False))
            on_token = kwargs.get("on_token")
            ts = self._trace_start("risk_scan", "扫描风险规则", input_summary=stock or "全部", source="database")
            risk_signals = self._scan_rules(_RISK_RULES, stock)
            self._trace_end(ts, f"{len(risk_signals)} 个风险信号")

            ts = self._trace_start("opp_scan", "扫描机会规则", source="database")
            opp_signals = self._scan_rules(_OPPORTUNITY_RULES, stock)
            self._trace_end(ts, f"{len(opp_signals)} 个机会信号")

            kb_context = ""
            kb_refs = []
            if self.ctx.kb:
                ts = self._trace_start("kb_search", "研报佐证检索", source="knowledge_base")
                kb_query = f"{stock or '医药生物'} 风险 机会 行业趋势 政策"
                kb_text, refs = self.ctx.kb.search_with_refs(kb_query, top_k=3)
                kb_context = kb_text
                kb_refs = refs
                self._trace_end(ts, f"{len(refs)} 条研报参考")

            ts = self._trace_start("llm_analyze", "LLM综合研判")
            analysis = self._analyze(risk_signals, opp_signals, kb_context, query, stream=stream, on_token=on_token)
            self._trace_end(ts, analysis[:200])

            return AgentResult(
                agent_name=self.name,
                content=analysis,
                references=kb_refs,
                metadata={
                    "risk_signals": risk_signals,
                    "opportunity_signals": opp_signals,
                },
                trace=self._collect_trace(),
            )
        except Exception as e:
            return AgentResult(agent_name=self.name, success=False, error=str(e), trace=self._collect_trace())

    # ------------------------------------------------------------------
    def _scan_rules(self, rules: List[Dict], stock: Optional[str]) -> List[Dict]:
        signals = []
        for rule in rules:
            try:
                sql = rule["sql"]
                if stock:
                    # 追加公司过滤
                    if "WHERE" in sql:
                        sql = sql.replace("WHERE", f"WHERE stock_abbr='{stock}' AND", 1)
                    else:
                        sql += f" WHERE stock_abbr='{stock}'"
                df = self.ctx.query_db(sql)
                if rule["condition"](df):
                    evidence = df.head(5).to_dict(orient="records")
                    signals.append({
                        "id": rule["id"],
                        "name": rule["name"],
                        "level": rule["level"],
                        "description": rule["desc"],
                        "evidence": evidence,
                    })
            except Exception:
                continue
        return signals

    def _analyze(self, risks, opps, kb_context, query,
                 stream: bool = False, on_token=None) -> str:
        role_hint = {
            "investor": "重点关注影响投资回报的风险和价值机会",
            "manager": "重点关注经营改善空间和战略方向",
            "regulator": "重点关注合规风险和异常信号",
        }.get(self.ctx.role.value, "")

        prompt = f"""你是医药生物行业的风险洞察专家。

角色视角: {self.ctx.role.label} — {role_hint}

## 检测到的风险信号
{json.dumps(risks, ensure_ascii=False, indent=2, default=str)}

## 检测到的机会信号
{json.dumps(opps, ensure_ascii=False, indent=2, default=str)}

## 研报参考
{kb_context[:3000] if kb_context else '无相关研报'}

请输出详尽、专业、深入的风险与机会研判报告，包含：
1. **风险全景图**：详细解读每一个风险信号的严重程度，剖析背后的潜在业务原因或市场环境因素（不能仅念财务数字，要解释影响）。
2. **价值机会**：对每个机会信号进行发散分析（比如研发转化为产品的潜力、现金流充沛对扩张的利好等）。
3. **行业与政策研判**：结合给出的研报内容，用 2-3 段话展开讨论宏观趋势如何影响这家公司。
4. **行动纲领**：从 {self.ctx.role.label} 视角，给出可直接作为决策参考的操作建议（每条展开说明 50-100 字）。

用户问题: {query}

直接输出中文分析报告（建议 500-900 字左右），条理清晰，带小标题，不要输出 JSON。"""
        return self.llm.complete(prompt, stream=stream, on_token=on_token)
