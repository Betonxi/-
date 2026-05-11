from functools import lru_cache
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional
import math
import queue
import threading

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agents import AgentContext, Orchestrator, Role, TraceStep
from common import KBEngine, LLMClient, connect_db
from config import DB_PATH_V1, DB_PATH_V2, KB_INDEX, KB_JSON, RESULT_DIR

ROOT = Path(__file__).resolve().parent
HOME_HTML = ROOT / "frontend" / "home.html"
WORKSPACE_HTML = ROOT / "frontend" / "index.html"
EXPANDED_COMPANY_CSV = ROOT / "示例数据" / "扩展公司池.csv"

app = FastAPI(title="企业运营分析与决策支持系统", version="1.0.0")
app.mount("/result", StaticFiles(directory=str(RESULT_DIR)), name="result")


class AnalyzeRequest(BaseModel):
    mode: str = "query"
    role: str = Role.INVESTOR.value
    question: str = ""
    stock: Optional[str] = None
    year: int = 2024
    extra: str = ""


@lru_cache(maxsize=1)
def get_resources():
    llm = LLMClient()
    conn1 = connect_db(DB_PATH_V1)
    conn2 = connect_db(DB_PATH_V2)
    kb = KBEngine(KB_JSON, KB_INDEX)
    return llm, conn1, conn2, kb


def build_orchestrator(role_value: str) -> Orchestrator:
    llm, conn1, conn2, kb = get_resources()
    try:
        role = Role(role_value)
    except ValueError:
        role = Role.INVESTOR
    ctx = AgentContext(
        llm=llm,
        conn1=conn1,
        conn2=conn2,
        kb=kb,
        result_dir=RESULT_DIR,
        role=role,
    )
    return Orchestrator(ctx)


def serialize_value(value: Any) -> Any:
    if isinstance(value, TraceStep):
        return {
            "agent": value.agent,
            "action": value.action,
            "detail": value.detail,
            "duration_ms": round(value.duration_ms, 1),
            "input_summary": value.input_summary,
            "output_summary": value.output_summary,
            "source": value.source,
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): serialize_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serialize_value(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    return value


def normalize_chart_path(chart: str) -> str:
    if not chart:
        return ""
    path = Path(chart)
    if path.name:
        return f"/result/{path.name}"
    return chart


def pack_result(result, blackboard: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "agent_name": result.agent_name,
        "success": result.success,
        "content": result.content,
        "charts": [normalize_chart_path(chart) for chart in result.charts],
        "references": serialize_value(result.references),
        "metadata": serialize_value(result.metadata),
        "trace": serialize_value(result.trace),
        "error": result.error,
        "blackboard": serialize_value(blackboard),
    }


def load_expanded_companies() -> List[str]:
    if not EXPANDED_COMPANY_CSV.exists():
        return []
    companies: List[str] = []
    with EXPANDED_COMPANY_CSV.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            name = (row.get("A股简称") or "").strip()
            if name:
                companies.append(name)
    return companies


def get_available_options() -> Dict[str, List[Any]]:
    _, conn1, _, _ = get_resources()
    companies_df = conn1.execute(
        "SELECT DISTINCT stock_abbr FROM core_performance_indicators_sheet "
        "WHERE stock_abbr IS NOT NULL AND TRIM(stock_abbr) <> '' ORDER BY stock_abbr"
    ).fetchall()
    years_df = conn1.execute(
        "SELECT DISTINCT report_year FROM core_performance_indicators_sheet "
        "WHERE report_year IS NOT NULL ORDER BY report_year DESC"
    ).fetchall()
    db_companies = [row[0] for row in companies_df if row and row[0]]
    companies: List[str] = []
    seen = set()
    for name in db_companies + load_expanded_companies():
        if not name or name in seen:
            continue
        seen.add(name)
        companies.append(name)
    years = [int(row[0]) for row in years_df if row and row[0] is not None]
    return {"companies": companies, "years": years}


def execute_analysis(req: AnalyzeRequest, stream: bool = False, on_token=None, on_phase=None):
    question = req.question.strip()
    stock = None if not req.stock or req.stock == "全部" else req.stock
    orch = build_orchestrator(req.role)
    orch.ctx.stock_abbr = stock
    orch.ctx.report_period = req.year
    if on_phase:
        orch.ctx.on_phase = on_phase

    if req.mode == "assessment":
        result = orch.agents["assessment"].run(
            f"评估{stock or '全部公司'}{req.year}年运营状况",
            stock=stock,
            year=req.year,
            stream=stream,
            on_token=on_token,
        )
    elif req.mode == "risk":
        result = orch.agents["risk"].run(
            f"分析{stock or '全部公司'}的风险与机会",
            stock=stock,
            stream=stream,
            on_token=on_token,
        )
    elif req.mode == "report":
        report_query = question or f"为{stock or '全部公司'}生成{req.year}年度{orch.ctx.role.label}分析报告"
        if req.extra.strip():
            report_query = f"{report_query}，{req.extra.strip()}"
        result = orch._run_full_report(report_query, stock=stock, year=req.year, stream=stream, on_token=on_token)
    else:
        if not question:
            raise HTTPException(status_code=400, detail="请输入问题")
        if req.stock == "全部":
            question = f"请仅基于当前数据库覆盖公司回答。{question}"
        result = orch.process(question, stream=stream, on_token=on_token)
    return result, orch.ctx.blackboard


def encode_stream_event(event: str, data: Dict[str, Any]) -> str:
    payload = json.dumps(serialize_value(data), ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    if not HOME_HTML.exists():
        raise HTTPException(status_code=404, detail="前端页面不存在")
    return HTMLResponse(HOME_HTML.read_text(encoding="utf-8"))


@app.get("/workspace", response_class=HTMLResponse)
def workspace() -> HTMLResponse:
    if not WORKSPACE_HTML.exists():
        raise HTTPException(status_code=404, detail="功能页面不存在")
    return HTMLResponse(WORKSPACE_HTML.read_text(encoding="utf-8"))


@app.get("/api/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/api/options")
def options() -> Dict[str, Any]:
    return {"ok": True, "result": get_available_options()}


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> Dict[str, Any]:
    try:
        result, blackboard = execute_analysis(req)
        return {"ok": True, "result": pack_result(result, blackboard)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/analyze/stream")
def analyze_stream(req: AnalyzeRequest) -> StreamingResponse:
    def event_generator():
        message_queue: queue.Queue = queue.Queue()
        done = object()

        def emit(event: str, payload: Dict[str, Any]):
            message_queue.put(encode_stream_event(event, payload))

        def on_token(delta: str, full_text: str):
            emit("token", {"delta": delta, "content": full_text})

        def on_phase(agent: str, detail: str):
            emit("phase", {"agent": agent, "detail": detail})

        def worker():
            try:
                result, blackboard = execute_analysis(req, stream=True, on_token=on_token, on_phase=on_phase)
                emit("result", {"ok": True, "result": pack_result(result, blackboard)})
            except HTTPException as exc:
                emit("error", {"detail": exc.detail, "status_code": exc.status_code})
            except Exception as exc:
                emit("error", {"detail": str(exc), "status_code": 500})
            finally:
                message_queue.put(done)

        threading.Thread(target=worker, daemon=True).start()

        while True:
            item = message_queue.get()
            if item is done:
                break
            yield item

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_server:app", host="127.0.0.1", port=8000, reload=False)
