import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response, Header, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from pydantic import BaseModel, Field

from .graph import app as agent_app
from .state import create_turn_input
from .streaming import stream_agent
from .logging_config import configure_logging
from .observability import Trace, set_current_trace, reset_current_trace
from .config import get_settings
from .retriever import dense_status, load_real_reranker, real_reranker_status
from .trace_payload import build_trace_payload
from .auth import (
    SESSION_COOKIE,
    authenticate,
    create_session,
    create_user,
    destroy_session,
    ensure_bootstrap_admin,
    get_user_by_session,
    list_users,
    set_user_permissions,
    set_user_access,
)
from .operations import (
    admin_dashboard,
    create_request,
    decide_request,
    employee_dashboard,
    list_requests,
    withdraw_request,
)

logger = logging.getLogger(__name__)

# ============================================================
# 1. Startup readiness and FastAPI application
# ============================================================
configure_logging()

_readiness: dict[str, Any] = {
    "status": "starting",
    "ready": False,
    "reason": "STARTUP_PRELOAD_IN_PROGRESS",
    "dense": {"ready": False},
    "kb_embeddings": {"ready": False},
    "bge_reranker": {"ready": False},
    "timings_ms": {},
}


def _preload_failure(category: str) -> RuntimeError:
    return RuntimeError(category)


async def _preload_local_models() -> None:
    started = time.perf_counter()
    _readiness.update(
        {
            "status": "starting",
            "ready": False,
            "reason": "STARTUP_PRELOAD_IN_PROGRESS",
            "timings_ms": {},
        }
    )
    settings = get_settings()

    dense_started = time.perf_counter()
    dense = await asyncio.to_thread(dense_status)
    dense_ms = (time.perf_counter() - dense_started) * 1000
    _readiness["dense"] = {
        "ready": bool(dense.get("available") and dense.get("model_ready")),
        "enabled": bool(dense.get("enabled")),
    }
    _readiness["kb_embeddings"] = {
        "ready": bool(dense.get("available") and dense.get("kb_embeddings_ready")),
    }
    _readiness["timings_ms"]["dense_preload"] = round(dense_ms, 2)
    _readiness["timings_ms"]["dense_model_preload"] = dense.get(
        "model_initialization_ms"
    )
    _readiness["timings_ms"]["kb_embedding_preparation"] = dense.get(
        "kb_embedding_preparation_ms"
    )
    if not dense.get("enabled"):
        raise _preload_failure("DENSE_PRELOAD_DISABLED")
    if not dense.get("available"):
        raise _preload_failure("DENSE_PRELOAD_FAILED")

    reranker_started = time.perf_counter()
    await asyncio.to_thread(
        load_real_reranker,
        settings.reranker_model_path,
        settings.reranker_device,
    )
    reranker_ms = (time.perf_counter() - reranker_started) * 1000
    reranker = real_reranker_status()
    _readiness["bge_reranker"] = {"ready": bool(reranker.get("loaded"))}
    _readiness["timings_ms"]["bge_preload"] = round(reranker_ms, 2)
    if not reranker.get("loaded"):
        raise _preload_failure("BGE_PRELOAD_FAILED")

    _readiness["timings_ms"]["total_startup_preparation"] = round(
        (time.perf_counter() - started) * 1000,
        2,
    )
    _readiness.update({"status": "ready", "ready": True, "reason": None})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_bootstrap_admin()
        await _preload_local_models()
    except Exception as exc:
        category = str(exc) if str(exc) else "STARTUP_PRELOAD_FAILED"
        _readiness.update(
            {
                "status": "failed",
                "ready": False,
                "reason": category,
            }
        )
        logger.error("SupportFlow local model preload failed: %s", category)
    yield
    _readiness.update({"status": "stopped", "ready": False})

api = FastAPI(
    title="SupportFlow Agent API",
    version="0.4.0",
    lifespan=lifespan,
)

STATIC_DIR = Path(__file__).parent / "static"
api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@api.get("/", include_in_schema=False)
async def workbench_index():
    return FileResponse(STATIC_DIR / "index.html")


# ============================================================
# 2. Request / Response Models
# ============================================================

class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)

    # Demo only:
    # 生产环境中的 role 应来自可信 Authentication Context，
    # 不能相信客户端自己声明的角色。
    role: str = "customer_service"


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)
    department: str = Field(default="", max_length=80)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


class PermissionUpdateRequest(BaseModel):
    permissions: list[str] = Field(default_factory=list)
    role: str | None = None


class OrderSubmissionPayload(BaseModel):
    customer_code: str = Field(min_length=1, max_length=40)
    sku: str = Field(min_length=1, max_length=80)
    quantity: int = Field(gt=0, le=10_000_000)
    required_date: str = Field(min_length=8, max_length=20)
    remark: str = Field(default="", max_length=500)


class ProductionReportPayload(BaseModel):
    order_no: str = Field(min_length=1, max_length=40)
    reported_quantity: int = Field(gt=0, le=10_000_000)
    qualified_quantity: int = Field(default=0, ge=0, le=10_000_000)
    rejected_quantity: int = Field(default=0, ge=0, le=10_000_000)
    report_date: str = Field(default="", max_length=20)
    remark: str = Field(default="", max_length=500)


class ProductionReportChangePayload(BaseModel):
    reported_quantity: int = Field(gt=0, le=10_000_000)
    qualified_quantity: int = Field(default=0, ge=0, le=10_000_000)
    rejected_quantity: int = Field(default=0, ge=0, le=10_000_000)
    remark: str = Field(default="", max_length=500)


class QualityReportPayload(BaseModel):
    order_no: str = Field(min_length=1, max_length=40)
    inspected_quantity: int = Field(gt=0, le=10_000_000)
    qualified_quantity: int = Field(default=0, ge=0, le=10_000_000)
    rejected_quantity: int = Field(default=0, ge=0, le=10_000_000)
    report_date: str = Field(default="", max_length=20)
    remark: str = Field(default="", max_length=500)


class OutboundRequestPayload(BaseModel):
    order_no: str = Field(min_length=1, max_length=40)
    quantity: int = Field(gt=0, le=10_000_000)
    remark: str = Field(default="", max_length=500)


class OperationDecision(BaseModel):
    approved: bool
    reason: str = Field(default="", max_length=500)


class ResumeRequest(BaseModel):
    thread_id: str = Field(min_length=1)
    confirmed: Optional[bool] = None
    action: Optional[str] = None
    amount: Any = None


class ChatResponse(BaseModel):
    thread_id: str
    intent: str
    result: str
    success: bool
    error_code: Optional[str] = None
    routing_source: str = "unknown"
    rag_source: Optional[str] = None
    retrieved_evidence_id: Optional[str] = None
    retrieved_evidence_title: Optional[str] = None
    retrieved_evidence_category: Optional[str] = None
    retrieved_evidence_version: Optional[str] = None
    rerank_score: Optional[float] = None
    answerability_status: Optional[str] = None
    generation_skipped: bool = False
    safe_fallback_reason: Optional[str] = None
    terminal_status: Optional[str] = None
    pending_action: Optional[str] = None
    missing_fields: list[str] = Field(default_factory=list)
    order_total: Optional[float] = None
    refundable_amount: Optional[float] = None
    interaction: Optional[dict] = None
    trace: Optional[dict] = None


def _interaction_from_state(state: dict[str, Any]) -> dict[str, Any] | None:
    if (
        state.get("pending_action") == "refund"
        and "amount" in (state.get("missing_fields") or [])
    ):
        return {
            "type": "refund_amount_required",
            "message": state.get("result") or "请填写退款金额，可选择全部退款。",
            "order_id": state.get("order_id") or state.get("current_order_id"),
            "order_total": state.get("order_total"),
            "refundable_amount": state.get("refundable_amount"),
        }
    if state.get("need_confirmation") and state.get("pending_action") == "refund":
        order_id = state.get("order_id") or state.get("current_order_id") or ""
        amount = float(state.get("refund_amount") or 0.0)
        return {
            "type": "refund_confirmation",
            "message": f"退款属于敏感操作，需要用户确认。准备为订单 {order_id} 退款 ¥{amount:.2f}。",
            "order_id": order_id,
            "amount": amount,
            "refundable_amount": state.get("refundable_amount"),
        }
    return None


def _chat_response_from_state(
    thread_id: str,
    final_state: dict[str, Any],
    trace: Trace,
) -> ChatResponse:
    error_log = final_state.get("error_log", "")
    return ChatResponse(
        thread_id=thread_id,
        intent=final_state.get("intent", "unknown"),
        result=final_state.get("result", ""),
        success=not bool(error_log),
        error_code="AGENT_ERROR" if error_log else None,
        routing_source=final_state.get("routing_source", "unknown"),
        rag_source=final_state.get("rag_source"),
        retrieved_evidence_id=final_state.get("retrieved_evidence_id") or None,
        retrieved_evidence_title=final_state.get("retrieved_evidence_title") or None,
        retrieved_evidence_category=final_state.get("retrieved_evidence_category") or None,
        retrieved_evidence_version=final_state.get("retrieved_evidence_version") or None,
        rerank_score=final_state.get("rerank_score"),
        answerability_status=final_state.get("answerability_status") or None,
        generation_skipped=bool(final_state.get("generation_skipped", False)),
        safe_fallback_reason=final_state.get("safe_fallback_reason") or None,
        terminal_status=final_state.get("terminal_status") or None,
        pending_action=final_state.get("pending_action") or None,
        missing_fields=list(final_state.get("missing_fields") or []),
        order_total=final_state.get("order_total") or None,
        refundable_amount=final_state.get("refundable_amount") or None,
        interaction=_interaction_from_state(final_state),
        trace=build_trace_payload(trace, final_state),
    )


# ============================================================
# 3. Health Check
# ============================================================

@api.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "supportflow-agent",
        "version": "0.4.0",
    }


@api.get("/readiness")
async def readiness():
    """Report whether local Dense, KB vectors, and BGE are ready."""
    from fastapi.responses import JSONResponse

    payload = {
        "status": _readiness["status"],
        "ready": _readiness["ready"],
        "reason": _readiness["reason"],
        "dense": _readiness["dense"],
        "kb_embeddings": _readiness["kb_embeddings"],
        "bge_reranker": _readiness["bge_reranker"],
        "timings_ms": _readiness["timings_ms"],
    }
    return JSONResponse(
        status_code=200 if _readiness["ready"] else 503,
        content=payload,
    )


# ============================================================
# 4. Local account authentication
# ============================================================

def _auth_required() -> bool:
    if os.getenv("SUPPORTFLOW_TEST_MODE") == "1":
        return False
    return os.getenv("SUPPORTFLOW_AUTH_REQUIRED", "true").lower() not in {
        "0",
        "false",
        "no",
    }


def _identity(request: Request, requested_role: str | None = None) -> dict:
    user = get_user_by_session(request.cookies.get(SESSION_COOKIE))
    if user is not None:
        return user
    if not _auth_required():
        return {
            "id": 0,
            "username": "development",
            "display_name": "开发模式",
            "role": requested_role or "customer_service",
            "permissions": [],
        }
    raise HTTPException(status_code=401, detail="请先登录")


def _admin_identity(request: Request) -> dict:
    user = _identity(request)
    if "admin_users" not in set(user.get("permissions") or []):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _permission_identity(request: Request, permission: str) -> dict:
    user = _identity(request)
    if permission not in set(user.get("permissions") or []):
        raise HTTPException(status_code=403, detail="当前角色没有该操作权限")
    return user


@api.post("/auth/register")
async def register(request: RegisterRequest):
    try:
        return {"user": create_user(**request.model_dump())}
    except ValueError as exc:
        if str(exc) == "USERNAME_EXISTS":
            raise HTTPException(status_code=409, detail="用户名已存在") from exc
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api.post("/auth/login")
async def login(request: LoginRequest, response: Response):
    ensure_bootstrap_admin()
    user = authenticate(request.username, request.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = create_session(user["id"])
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=12 * 60 * 60,
    )
    return {"user": user}


@api.post("/auth/logout")
async def logout(request: Request, response: Response):
    destroy_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@api.get("/auth/me")
async def me(request: Request):
    return {"user": _identity(request)}


@api.get("/admin/users")
async def admin_users(request: Request):
    _admin_identity(request)
    return {"users": list_users()}


@api.put("/admin/users/{user_id}/permissions")
async def update_permissions(
    user_id: int,
    payload: PermissionUpdateRequest,
    request: Request,
):
    actor = _admin_identity(request)
    try:
        user = set_user_access(user_id, payload.role, payload.permissions, actor["id"]) if payload.role else set_user_permissions(user_id, payload.permissions, actor["id"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return {"user": user}


# ============================================================
# 5. Manufacturing operation requests and dashboards
# ============================================================

@api.get("/operations/my-dashboard")
async def my_dashboard(request: Request):
    user = _identity(request)
    return employee_dashboard(user["id"])


@api.get("/operations/production-reports")
async def my_production_reports(request: Request):
    user = _identity(request)
    dashboard = employee_dashboard(user["id"])
    return {"reports": dashboard["production_reports"]}


@api.get("/operations/requests")
async def my_operation_requests(request: Request, status: str | None = None):
    user = _identity(request)
    return {"requests": list_requests(requested_by=user["id"], status=status)}


@api.post("/operations/requests/{request_id}/withdraw")
async def withdraw_operation_request(request_id: int, request: Request):
    user = _identity(request)
    try:
        return {"request": withdraw_request(request_id, user["id"])}
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@api.post("/operations/order-submissions")
async def submit_order(payload: OrderSubmissionPayload, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    user = _permission_identity(request, "submit_order")
    try:
        item = create_request("order_submission", user["id"], payload.model_dump(), idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request": item, "message": "订单申请已提交，等待管理员审批。"}


@api.post("/operations/production-reports")
async def submit_production_report(payload: ProductionReportPayload, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    user = _permission_identity(request, "submit_production_report")
    try:
        item = create_request("production_report", user["id"], payload.model_dump(), idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request": item, "message": "生产报工已提交，等待管理员审核。"}


@api.post("/operations/production-reports/{report_id}/change")
async def change_production_report(report_id: int, payload: ProductionReportChangePayload, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    user = _permission_identity(request, "submit_production_report")
    data = payload.model_dump() | {"report_id": report_id}
    try:
        item = create_request("production_report_change", user["id"], data, idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request": item, "message": "报工修改申请已提交，等待管理员审核。"}


@api.post("/operations/production-reports/{report_id}/withdraw")
async def withdraw_production_report(report_id: int, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    user = _permission_identity(request, "submit_production_report")
    try:
        item = create_request("production_report_withdraw", user["id"], {"report_id": report_id}, idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request": item, "message": "报工撤回申请已提交，等待管理员审核。"}


@api.post("/operations/quality-reports")
async def submit_quality_report(payload: QualityReportPayload, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    user = _permission_identity(request, "submit_quality_report")
    try:
        item = create_request("quality_report", user["id"], payload.model_dump(), idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request": item, "message": "质检报工已提交，等待管理员审核。"}


@api.post("/operations/outbound-requests")
async def submit_outbound_request(payload: OutboundRequestPayload, request: Request, idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
    user = _permission_identity(request, "request_outbound")
    try:
        item = create_request("outbound_request", user["id"], payload.model_dump(), idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request": item, "message": "出库申请已提交，库存将在管理员批准后变更。"}


@api.get("/admin/operations/dashboard")
async def operations_dashboard(
    request: Request,
    employee_id: int | None = Query(default=None),
    status: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
):
    _permission_identity(request, "admin_data")
    return admin_dashboard(employee_id=employee_id, status=status, date_from=date_from, date_to=date_to)


@api.get("/admin/operations/requests")
async def all_operation_requests(request: Request):
    _permission_identity(request, "admin_data")
    return {"requests": list_requests()}


@api.post("/admin/operations/requests/{request_id}/decision")
async def decide_operation_request(
    request_id: int,
    payload: OperationDecision,
    request: Request,
):
    actor = _permission_identity(request, "admin_data")
    try:
        item = decide_request(request_id, actor["id"], payload.approved, payload.reason)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request": item}


@api.post("/admin/operations/orders")
async def create_admin_order(payload: OrderSubmissionPayload, request: Request):
    actor = _permission_identity(request, "create_sales_order")
    try:
        item = create_request("order_submission", actor["id"], payload.model_dump())
        result = decide_request(item["id"], actor["id"], True, "管理员直接创建")
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"request": result, "message": "管理员订单已直接创建并进入已审批状态。"}


# ============================================================
# 4. Normal Chat API
# ============================================================

@api.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, http_request: Request):
    identity = _identity(http_request, request.role)
    role = identity["role"]
    permissions = identity.get("permissions")
    trace = Trace(metadata={"thread_id": request.thread_id, "role": role})
    trace_token = set_current_trace(trace)
    try:
        config = {
            "configurable": {
                "thread_id": request.thread_id,
            }
        }

        existing_state = None
        try:
            existing_state = agent_app.get_state(config).values
        except Exception:
            # A new thread has no checkpoint yet.
            existing_state = None

        # Reset current-turn fields while preserving checkpoint business context.
        initial_state = create_turn_input(
            user_message=request.message,
            role=role,
            user_id=int(identity["id"]),
            existing_state=existing_state,
        )
        initial_state["permissions"] = list(permissions or [])

        final_state = await agent_app.ainvoke(
            initial_state,
            config=config,
        )

        error_log = final_state.get("error_log", "")

        success = not bool(error_log)
        trace.finish(success=success)

        return _chat_response_from_state(request.thread_id, final_state, trace)

    except Exception as exc:
        trace.finish(success=False, error="INTERNAL_ERROR")
        raise HTTPException(
            status_code=500,
            detail="SupportFlow Agent internal error",
        ) from exc
    finally:
        if trace.end_time is None:
            trace.finish(success=True)
        logger.info(
            "Agent request completed",
            extra={
                "event": "agent_request_completed",
                "trace_id": trace.trace_id,
                "thread_id": request.thread_id,
                "success": trace.success,
                "total_latency_ms": round(trace.latency_ms or 0, 2),
                "llm_calls": trace.llm_calls,
                "llm_usage": trace.llm_usage,
                "llm_operations": [
                    {
                        "operation": span.metadata.get("operation"),
                        "provider": span.metadata.get("provider"),
                        "model": span.metadata.get("model"),
                        "success": span.success,
                        "error_classification": span.metadata.get(
                            "error_classification"
                        ),
                        "latency_ms": round(span.latency_ms or 0, 2),
                    }
                    for span in trace.spans
                    if span.name == "llm"
                ],
            },
        )
        reset_current_trace(trace_token)


# ============================================================
# 5. Refund HITL Resume API
# ============================================================

@api.post("/chat/resume", response_model=ChatResponse)
async def resume_chat(request: ResumeRequest, http_request: Request):
    """Resume HITL or continue a persisted refund slot-filling action."""
    identity = _identity(http_request)
    role = identity["role"]
    permissions = identity.get("permissions")
    trace = Trace(metadata={"thread_id": request.thread_id, "resume": True, "role": role})
    trace_token = set_current_trace(trace)
    config = {"configurable": {"thread_id": request.thread_id}}
    try:
        snapshot = agent_app.get_state(config)
        if os.getenv("SUPPORTFLOW_TEST_MODE") == "1" and snapshot.values:
            role = str(snapshot.values.get("role") or role)
            permissions = list(snapshot.values.get("permissions") or permissions or [])
        if snapshot.next:
            if request.action is not None or request.confirmed is None:
                raise HTTPException(status_code=400, detail="当前会话正在等待退款确认")
            final_state = await agent_app.ainvoke(
                Command(resume=request.confirmed),
                config=config,
            )
        else:
            values = snapshot.values or {}
            if values.get("pending_action") != "refund":
                raise HTTPException(status_code=409, detail="当前会话没有等待退款操作")
            if request.confirmed is not None:
                raise HTTPException(status_code=400, detail="当前会话尚未进入退款确认")
            if request.action == "cancel_amount":
                continuation_message = "取消退款金额填写"
            elif request.action == "full_refund":
                continuation_message = "全部退款"
            elif request.action == "amount":
                if request.amount is None:
                    raise HTTPException(status_code=422, detail="请提供退款金额")
                continuation_message = f"退款 {request.amount} 元"
            else:
                raise HTTPException(status_code=400, detail="不支持的退款操作")

            continuation_state = create_turn_input(
                user_message=continuation_message,
                role=role,
                user_id=int(identity["id"]),
                existing_state=values,
            )
            continuation_state["permissions"] = list(permissions or [])
            final_state = await agent_app.ainvoke(
                continuation_state,
                config=config,
            )
        error_log = final_state.get("error_log", "")
        trace.finish(success=not bool(error_log))
        return _chat_response_from_state(request.thread_id, final_state, trace)
    except HTTPException:
        raise
    except Exception as exc:
        trace.finish(success=False, error="INTERNAL_ERROR")
        raise HTTPException(status_code=500, detail="SupportFlow Agent internal error") from exc
    finally:
        if trace.end_time is None:
            trace.finish(success=True)
        reset_current_trace(trace_token)


# ============================================================
# 6. Streaming Chat API
# ============================================================

@api.post("/chat/stream")
async def chat_stream(request: ChatRequest, http_request: Request):
    identity = _identity(http_request, request.role)
    return StreamingResponse(
        stream_agent(
            message=request.message,
            thread_id=request.thread_id,
            role=identity["role"],
            user_id=int(identity["id"]),
            permissions=list(identity.get("permissions") or []),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )
