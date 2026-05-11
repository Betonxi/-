"""
AssessmentAgent —— 企业运营评估智能体
五维评分体系：盈利能力、成长能力、运营效率、偿债能力、现金流质量
输出：评分卡、雷达图、诊断结论、同业对标
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .base import BaseAgent, AgentContext, AgentResult

# ---------------------------------------------------------------------------
# 评分维度定义
# ---------------------------------------------------------------------------
DIMENSIONS = ["Profitability", "Growth", "Efficiency", "Solvency", "Cash Flow"]
DIMENSIONS_CN = ["盈利能力", "成长能力", "运营效率", "偿债能力", "现金流质量"]

# 各维度阈值 (min_bad, max_excellent) — 用于 0~100 分标准化
# 基于医药生物行业经验值
_SCORING_RULES: Dict[str, Dict[str, tuple]] = {
    "Profitability": {
        "roe":                (0, 20),      # %
        "net_profit_margin":  (0, 25),      # %
        "gross_profit_margin":(20, 70),     # %
    },
    "Growth": {
        "operating_revenue_yoy_growth": (-10, 30),  # %
        "net_profit_yoy_growth":        (-20, 40),  # %
    },
    "Efficiency": {
        "expense_ratio":      (80, 40),     # 反向: 越低越好
        "rnd_ratio":          (0, 15),      # 研发占比 越高越好
    },
    "Solvency": {
        "asset_liability_ratio": (70, 30),  # 反向: 越低越好
    },
    "Cash Flow": {
        "cf_to_profit_ratio": (0, 1.5),     # 经营现金流/净利润
    },
}


def _normalize(value: float, lo: float, hi: float) -> float:
    """将原始值映射到 0~100 分"""
    if lo < hi:
        score = (value - lo) / (hi - lo) * 100
    else:  # 反向指标
        score = (lo - value) / (lo - hi) * 100
    return max(0.0, min(100.0, score))


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
class AssessmentAgent(BaseAgent):
    name = "AssessmentAgent"

    def run(self, query: str, *, stock: Optional[str] = None,
            year: int = 2024, compare: bool = True, **kwargs) -> AgentResult:
        """
        Parameters
        ----------
        stock : 股票简称，None 则评估全部公司
        year  : 评估年份
        compare : 是否生成同业对标
        """
        try:
            stream = bool(kwargs.get("stream", False))
            on_token = kwargs.get("on_token")
            companies = self._resolve_companies(stock)
            all_scores: Dict[str, Dict[str, float]] = {}
            all_details: Dict[str, Dict[str, Any]] = {}

            for comp in companies:
                ts = self._trace_start("fetch_metrics", f"获取{comp} {year}年指标", source="database")
                raw = self._fetch_metrics(comp, year)
                self._trace_end(ts, f"{len(raw)}项指标")
                ts = self._trace_start("score", f"计算{comp}五维评分")
                scores, details = self._score(raw)
                self._trace_end(ts, str(scores))
                all_scores[comp] = scores
                all_details[comp] = details

            ts = self._trace_start("plot_radar", "生成雷达图")
            chart_path = self._plot_radar(all_scores, year)
            self._trace_end(ts, chart_path)

            ts = self._trace_start("llm_diagnose", "LLM诊断分析")
            diagnosis = self._diagnose(all_scores, all_details, year, query, stream=stream, on_token=on_token)
            self._trace_end(ts, diagnosis[:200])

            meta = {"scores": {c: s for c, s in all_scores.items()},
                    "details": {c: d for c, d in all_details.items()},
                    "year": year}
            return AgentResult(
                agent_name=self.name,
                content=diagnosis,
                charts=[chart_path] if chart_path else [],
                metadata=meta,
                trace=self._collect_trace(),
            )
        except Exception as e:
            return AgentResult(agent_name=self.name, success=False, error=str(e), trace=self._collect_trace())

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------
    def _resolve_companies(self, stock: Optional[str]) -> List[str]:
        if stock:
            return [stock]
        df = self.ctx.query_db("SELECT DISTINCT stock_abbr FROM core_performance_indicators_sheet")
        return df["stock_abbr"].tolist()

    def _fetch_metrics(self, stock: str, year: int) -> Dict[str, Any]:
        """从数据库拉取评估所需的原始指标"""
        m: Dict[str, Any] = {"stock": stock, "year": year}

        # --- 核心指标表 ---
        row = self.ctx.query_db(
            f"SELECT * FROM core_performance_indicators_sheet "
            f"WHERE stock_abbr='{stock}' AND report_period='{year}FY'"
        )
        if not row.empty:
            r = row.iloc[0]
            m["roe"] = r.get("roe")
            m["net_profit_margin"] = r.get("net_profit_margin")
            m["gross_profit_margin"] = r.get("gross_profit_margin")
            m["operating_revenue_yoy_growth"] = r.get("operating_revenue_yoy_growth")
            m["net_profit_yoy_growth"] = r.get("net_profit_yoy_growth")
            m["eps"] = r.get("eps")

        # --- 利润表 —— 费用率、研发占比 ---
        inc = self.ctx.query_db(
            f"SELECT * FROM income_sheet "
            f"WHERE stock_abbr='{stock}' AND report_period='{year}FY'"
        )
        if not inc.empty:
            r = inc.iloc[0]
            rev = r.get("total_operating_revenue") or 0
            if rev > 0:
                total_exp = r.get("total_operating_expenses") or 0
                m["expense_ratio"] = total_exp / rev * 100
                rnd = r.get("operating_expense_rnd_expenses") or 0
                m["rnd_ratio"] = rnd / rev * 100
            m["net_profit"] = r.get("net_profit")
            m["total_operating_revenue"] = rev

        # --- 资产负债表 ---
        bal = self.ctx.query_db(
            f"SELECT * FROM balance_sheet "
            f"WHERE stock_abbr='{stock}' AND report_period='{year}FY'"
        )
        if not bal.empty:
            r = bal.iloc[0]
            m["asset_liability_ratio"] = r.get("asset_liability_ratio")
            m["total_assets"] = r.get("asset_total_assets")

        # --- 现金流量表 ---
        cf = self.ctx.query_db(
            f"SELECT * FROM cash_flow_sheet "
            f"WHERE stock_abbr='{stock}' AND report_period='{year}FY'"
        )
        if not cf.empty:
            r = cf.iloc[0]
            net_profit = m.get("net_profit") or 0
            oper_cf = r.get("operating_cf_net_amount") or 0
            m["cf_to_profit_ratio"] = (oper_cf / net_profit) if net_profit != 0 else 0

        return m

    # ------------------------------------------------------------------
    # 评分
    # ------------------------------------------------------------------
    def _score(self, raw: Dict[str, Any]):
        dim_scores: Dict[str, float] = {}
        dim_details: Dict[str, Any] = {}

        for dim, metrics in _SCORING_RULES.items():
            scores = []
            detail = {}
            for metric_key, (lo, hi) in metrics.items():
                val = raw.get(metric_key)
                if val is None or pd.isna(val):
                    continue
                s = _normalize(float(val), lo, hi)
                scores.append(s)
                detail[metric_key] = {"value": round(float(val), 2), "score": round(s, 1)}
            dim_scores[dim] = round(sum(scores) / len(scores), 1) if scores else 50.0
            dim_details[dim] = detail

        return dim_scores, dim_details

    # ------------------------------------------------------------------
    # 雷达图
    # ------------------------------------------------------------------
    def _plot_radar(self, all_scores: Dict[str, Dict[str, float]], year: int) -> str:
        labels = DIMENSIONS
        n = len(labels)
        angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
        angles += angles[:1]

        fig, ax = plt.subplots(figsize=(7, 7), subplot_kw=dict(polar=True), dpi=150)
        colors = ["#4e79a7", "#e15759", "#76b7b2", "#f28e2b"]

        for idx, (comp, scores) in enumerate(all_scores.items()):
            values = [scores.get(d, 50) for d in labels]
            values += values[:1]
            color = colors[idx % len(colors)]
            ax.plot(angles, values, linewidth=2, label=comp, color=color)
            ax.fill(angles, values, alpha=0.15, color=color)

        ax.set_thetagrids([a * 180 / np.pi for a in angles[:-1]], labels, fontsize=11)
        ax.set_ylim(0, 100)
        ax.set_title(f"Corporate Assessment Radar ({year})",
                     fontsize=14, fontweight="bold", pad=20)
        ax.legend(loc="upper right", bbox_to_anchor=(1.25, 1.1), fontsize=10)
        ax.grid(True, alpha=0.3)

        out = self.ctx.result_dir / f"assessment_radar_{year}.png"
        fig.savefig(out, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        return str(out)

    # ------------------------------------------------------------------
    # LLM 诊断
    # ------------------------------------------------------------------
    def _diagnose(self, all_scores, all_details, year, query,
                  stream: bool = False, on_token=None) -> str:
        data_str = json.dumps(
            {"scores": all_scores, "details": all_details},
            ensure_ascii=False, indent=2, default=str
        )
        role_hint = {
            "investor": "从投资价值角度分析，关注盈利能力和成长性",
            "manager": "从企业管理角度诊断短板，给出改进建议",
            "regulator": "从监管风控角度关注偿债和现金流异常",
        }.get(self.ctx.role.value, "")

        prompt = f"""你是医药生物行业的企业运营评估专家。

角色视角: {self.ctx.role.label} — {role_hint}

以下是 {year} 年度企业运营五维评分数据 (0-100分):
{data_str}

五个维度: 盈利能力、成长能力、运营效率、偿债能力、现金流质量

请输出结构化、深入的评估诊断分析：
1. 总体运营评级：给出一个明确结论（优秀/良好/一般/较差），并用一段话概括核心原因。
2. 五维深度剖析：针对盈利能力、成长能力、运营效率、偿债能力、现金流质量分别输出一段深入分析（不要只是念数字，要说明“为什么是这个分数，意味着什么”）。
3. 同业对标结论：结合竞争对手数据找出显著的优势和劣势。
4. 核心建议：针对 {self.ctx.role.label} 给出 3-5 条具有可行性的长效建议，每条展开说明。

用户问题: {query}

直接输出中文分析报告（篇幅建议 400-800 字），使用 Markdown 排版，不要输出JSON。"""
        return self.llm.complete(prompt, stream=stream, on_token=on_token)
