"""
QueryAgent —— 智能问数智能体
封装 SQL 生成、知识库检索、交叉验证、自动绘图能力。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List

import pandas as pd

from .base import BaseAgent, AgentContext, AgentResult
from common import SubTask, schema_text, plot_auto


class QueryAgent(BaseAgent):
    name = "QueryAgent"

    @staticmethod
    def _sanitize_value(value: Any) -> Any:
        if isinstance(value, float) and pd.isna(value):
            return None
        return value

    @classmethod
    def _sanitize_records(cls, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        cleaned = []
        for row in records:
            if not isinstance(row, dict):
                continue
            cleaned.append({k: cls._sanitize_value(v) for k, v in row.items()})
        return cleaned

    def __init__(self, ctx: AgentContext):
        super().__init__(ctx)
        self.schema = schema_text(ctx.conn1)

    # ------------------------------------------------------------------
    def run(self, query: str, *, qid: str = "Q", **kwargs) -> AgentResult:
        try:
            stream = bool(kwargs.get("stream", False))
            on_token = kwargs.get("on_token")
            # 1. 规划
            ts = self._trace_start("task_plan", "问题拆解为子任务", input_summary=query)
            tasks = self._plan(query)
            self._trace_end(ts, f"{len(tasks)} 个子任务")
            if not tasks:
                return AgentResult(agent_name=self.name, content="无法将问题拆解为有效子任务。", trace=self._collect_trace())
            # 2. 执行
            for t in tasks:
                ts = self._trace_start(
                    f"{t.task_type}_execute", t.description,
                    input_summary=t.query[:200], source=t.task_type,
                )
                t.result = self._execute(t)
                out = str(t.result)[:200] if t.result else ""
                self._trace_end(ts, out)
            # 3. 整合
            ts = self._trace_start("integrate", "整合结果生成回答")
            result = self._integrate(query, tasks, qid, stream=stream, on_token=on_token)
            self._trace_end(ts, result.content[:200])
            result.trace = self._collect_trace()
            return result
        except Exception as e:
            return AgentResult(agent_name=self.name, success=False, error=str(e), trace=self._collect_trace())

    # ------------------------------------------------------------------
    # 子步骤
    # ------------------------------------------------------------------
    def _plan(self, question: str) -> List[SubTask]:
        ctx_info = []
        if self.ctx.stock_abbr:
            ctx_info.append(f"公司：{self.ctx.stock_abbr}")
        if self.ctx.report_period:
            ctx_info.append(f"报告期：{self.ctx.report_period}")
        ctx_str = "，".join(ctx_info) if ctx_info else "无特定限制"

        prompt = f"""你是任务规划专家。将用户问题拆解为子任务。
子任务类型: 'sql' (数据库查询) 或 'kb' (知识库检索)。

【重要上下文】
当前前端下拉框选中状态：{ctx_str}
规则：如果用户问题中没有明确指明其他公司或年份，你必须强制使用上述下拉框上下文信息来补全 SQL 或 KB 的查询条件！

数据库 Schema:
{self.schema}

SQL 编写规范：
- SQLite 语法，禁止 YEAR()/MONTH()
- 使用 report_year (INTEGER) 进行年份过滤
- report_period 格式：年度="2024FY"，一季度="2024Q1"，半年度="2024HY"，三季度="2024Q3"

用户问题: {question}

输出 JSON 数组:
[{{"id":1,"task_type":"sql","description":"描述","query":"SQL 或关键词"}}]
"""
        tasks_json = self.llm.complete_json(prompt)
        sub_tasks = []
        if isinstance(tasks_json, list):
            for i, t in enumerate(tasks_json, start=1):
                if isinstance(t, dict) and "task_type" in t:
                    sub_tasks.append(SubTask(
                        id=i,
                        task_type=t.get("task_type", "kb"),
                        description=t.get("description", ""),
                        query=t.get("query", ""),
                    ))
        return sub_tasks

    def _execute(self, task: SubTask) -> Any:
        if task.task_type == "sql":
            try:
                sql = self._safe_sql(task.query)
                df, verify = self.ctx.query_both(sql)
                return {"data": df.to_dict(orient="records"), "verification": verify,
                        "sql": sql, "rows": len(df)}
            except Exception as e:
                try:
                    repaired_df = self._sql_with_retry(task.query, max_retries=2)
                    repaired_sql = self._safe_sql(self._fix_sql(task.query, str(e)))
                    return {
                        "data": repaired_df.to_dict(orient="records"),
                        "verification": "已自动纠错后执行（未做双库校验）",
                        "sql": repaired_sql,
                        "rows": len(repaired_df),
                    }
                except Exception as e2:
                    return f"SQL Error: {e2}"
        elif task.task_type == "kb":
            content, refs = self.ctx.kb.search_with_refs(task.query)
            return {"content": content, "references": refs}
        return "Unknown task type"

    def _integrate(self, question: str, tasks: List[SubTask], qid: str,
                   stream: bool = False, on_token=None) -> AgentResult:
        context = ""
        sqls, kb_refs = [], []
        for t in tasks:
            if t.task_type == "sql" and isinstance(t.result, dict) and "data" in t.result:
                cleaned_data = self._sanitize_records(t.result["data"])
                preview = json.dumps(cleaned_data, ensure_ascii=False, indent=2)[:3000]
                context += f"子任务{t.id}({t.description}):\n{preview}\n校验: {t.result['verification']}\n\n"
                sqls.append(t.query)
            elif t.task_type == "kb" and isinstance(t.result, dict):
                context += f"子任务{t.id}({t.description}):\n{str(t.result.get('content',''))[:5000]}\n\n"
                refs = t.result.get("references", [])
                if isinstance(refs, list):
                    kb_refs.extend(refs)
            else:
                context += f"子任务{t.id}({t.description}):\n{json.dumps(t.result, ensure_ascii=False)[:5000]}\n\n"

        prompt = f"""基于以下子任务结果回答用户问题。
要求:
1. 直接输出最终文字回答，不要输出 JSON。
2. 如果通过了双数据库交叉验证，可以简要说明。
3. 不要编造来源。
4. 先给结论，再给数据依据，不要机械逐年复述。
5. 如果数据缺失，不要写 NaN，要写“该期数据缺失”或“未披露该指标”。
6. 如果用户在问“是不是短板/问题/风险”，要明确回答“是/不是/现有数据不足以支持”，再解释原因。
7. 回答风格要像财务分析师，简洁、自然、可直接给业务方看。

用户问题: {question}
执行背景:
{context}

请提供一份内容充实、分析到位的回答（建议 300-600 字），重点说明数据趋势、经营健康度和可能原因，并紧密扣紧用户的实际问题。不要机械列举，要有明确分析逻辑和业务视角。
"""
        content = self.llm.complete(prompt, stream=stream, on_token=on_token)

        images, chart_type = [], "无"
        for t in tasks:
            if t.task_type == "sql" and isinstance(t.result, dict) and t.result.get("data"):
                df = pd.DataFrame(t.result["data"])
                imgs, ctype = plot_auto(df, qid, t.id, self.ctx.result_dir)
                images.extend(imgs)
                chart_type = ctype

        refs = []
        if kb_refs:
            seen = set()
            merged = []
            for ref in kb_refs + refs:
                if not isinstance(ref, dict):
                    continue
                key = (ref.get("paper_path", ""), ref.get("text", "")[:100])
                if key not in seen:
                    seen.add(key)
                    merged.append(ref)
            refs = merged

        return AgentResult(
            agent_name=self.name,
            content=content,
            charts=images,
            references=refs,
            metadata={"sql": "\n".join(sqls), "chart_type": chart_type},
        )
