"""
ReportAgent —— 定制化报告生成智能体
根据角色自动整合其他 Agent 的输出，生成结构化报告。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .base import BaseAgent, AgentContext, AgentResult, Role


# ---------------------------------------------------------------------------
# 角色报告模板
# ---------------------------------------------------------------------------
_REPORT_TEMPLATES: Dict[str, str] = {
    Role.INVESTOR.value: """你是面向投资者的分析报告撰写专家。

请根据以下分析结果，为投资者生成一份结构化投研报告。

报告结构：
## 一、企业基本面摘要
（概述公司主营业务、行业地位）

## 二、财务趋势分析
（基于数据查询结果，分析关键财务指标趋势）

## 三、运营评估总结
（基于五维评分，总结运营状况）

## 四、风险与机会
（列出主要风险信号和投资机会）

## 五、投资关注点
（给出 3-5 条投资建议，附数据依据）

## 六、数据来源与溯源
（列出所有参考来源）

要求：每个结论都要有数据支撑，标注数据来源。""",

    Role.MANAGER.value: """你是面向企业管理者的运营诊断报告撰写专家。

报告结构：
## 一、运营健康度总览
（五维评分概述，标注优势与短板）

## 二、各维度深度诊断
（每个维度：现状、问题根因、改进方向）

## 三、同业对标分析
（与竞争对手对比，找差距）

## 四、风险预警
（需要管理层关注的风险项）

## 五、战略优化建议
（3-5 条可落地的改进建议）

## 六、数据来源
（数据溯源）""",

    Role.REGULATOR.value: """你是面向监管机构的风险审查报告撰写专家。

报告结构：
## 一、企业基本信息
（公司代码、名称、行业、报告期）

## 二、财务异常筛查
（偿债风险、现金流异常、大额波动）

## 三、风险信号汇总
（每个信号：等级、触发规则、数据证据）

## 四、合规关注事项
（需要重点核查的项目）

## 五、监管建议
（建议采取的监管措施）

## 六、数据来源与审计线索
（完整数据溯源）""",
}


class ReportAgent(BaseAgent):
    name = "ReportAgent"

    def run(self, query: str, *,
            agent_results: Optional[List[AgentResult]] = None,
            stock: Optional[str] = None,
            **kwargs) -> AgentResult:
        """
        将其他 Agent 的结果整合为一份角色化报告。
        """
        try:
            stream = bool(kwargs.get("stream", False))
            on_token = kwargs.get("on_token")
            if not agent_results:
                return AgentResult(agent_name=self.name, content="没有可整合的分析结果。", trace=self._collect_trace())

            ts = self._trace_start("build_context", "汇总多Agent输出为报告上下文")
            context = self._build_context(agent_results)
            self._trace_end(ts, context[:200])

            template = _REPORT_TEMPLATES.get(self.ctx.role.value, _REPORT_TEMPLATES[Role.INVESTOR.value])
            ts = self._trace_start("generate_report", "LLM生成角色化报告")
            report = self._generate(template, context, query, stock, stream=stream, on_token=on_token)
            self._trace_end(ts, report[:200])

            # 汇总所有图表和引用
            all_charts = []
            all_refs = []
            merged_trace = []
            for r in agent_results:
                all_charts.extend(r.charts)
                all_refs.extend(r.references)
                merged_trace.extend(r.trace)

            return AgentResult(
                agent_name=self.name,
                content=report,
                charts=all_charts,
                references=all_refs,
                metadata={"role": self.ctx.role.value},
                trace=merged_trace + self._collect_trace(),
            )
        except Exception as e:
            return AgentResult(agent_name=self.name, success=False, error=str(e), trace=self._collect_trace())

    # ------------------------------------------------------------------
    def _build_context(self, results: List[AgentResult]) -> str:
        parts = []
        for r in results:
            section = f"### {r.agent_name} 分析结果\n"
            if r.content:
                section += r.content[:5000] + "\n"
            if r.metadata:
                safe_meta = {k: v for k, v in r.metadata.items()
                             if not isinstance(v, (bytes,))}
                section += f"附加数据: {json.dumps(safe_meta, ensure_ascii=False, default=str)[:3000]}\n"
            if r.references:
                section += f"参考来源: {json.dumps(r.references, ensure_ascii=False)[:1000]}\n"
            parts.append(section)
        return "\n".join(parts)

    def _generate(self, template: str, context: str, query: str,
                  stock: Optional[str], stream: bool = False, on_token=None) -> str:
        prompt = f"""{template}

## 分析数据输入
{context}

## 用户需求
{query}

## 关注公司
{stock or '全部'}

请根据以上模板和数据，生成一份**内容完整、结构清晰（建议 800-1500 字）**的分析报告。

要求：
- **分析充分**：关键章节要适度展开，结合财务指标和研报背景给出有逻辑的推理（不要简单拼凑数字，要解释“为什么”、“怎么做”）。
- **逻辑连贯**：章节之间要有过渡段落，全篇行文连贯如一篇真实券商/管理层报告。
- 严格按模板结构生成。
- 保证每个结论后面紧贴明确的引用或数据。
- 采用专业的排版格式（Markdown 标题、重点加粗、分点列表等）。"""

        return self.llm.complete(prompt, max_tokens=8000, stream=stream, on_token=on_token)
