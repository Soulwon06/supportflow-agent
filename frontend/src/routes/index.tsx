import { createFileRoute } from "@tanstack/react-router";
import { FormEvent, useEffect, useMemo, useState } from "react";

export const Route = createFileRoute("/")({ component: Workbench });

type User = { id: number; username: string; display_name: string; department?: string; role?: string; permissions?: string[] };
type Message = { role: "user" | "assistant" | "error"; text: string };
type ProgressItem = { message: string; status: string; elapsed?: string };
type Trace = Record<string, any>;

const permissionLabels: Record<string, string> = {
  query_order: "查询订单",
  query_logistics: "查询物流",
  query_inventory: "查询库存",
  query_production: "查询生产进度",
  submit_order: "提交客户订单",
  submit_production_report: "提交生产报工",
  submit_quality_report: "提交质检报工",
  request_outbound: "提交出库申请",
  create_sales_order: "管理员直接建单",
  admin_users: "管理员工账号",
  admin_data: "查看管理数据",
  refund_order: "旧退款流程兼容",
};

const roleLabels: Record<string, string> = {
  employee: "普通员工",
  sales_employee: "业务员",
  production_employee: "生产员工",
  quality_employee: "质检员工",
  warehouse_employee: "仓库员工",
  admin: "管理员",
};

const apiBase = import.meta.env.VITE_API_BASE_URL || "/api";
const apiUrl = (path: string) => `${apiBase}${path}`;

function newThreadId() {
  return globalThis.crypto?.randomUUID?.() || `thread-${Date.now()}`;
}

function initialThreadId() {
  if (typeof window === "undefined") return newThreadId();
  return localStorage.getItem("supportflow_thread_id") || newThreadId();
}

async function requestJson(path: string, init?: RequestInit) {
  const response = await fetch(apiUrl(path), { credentials: "include", ...init });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || "请求失败");
  return data;
}

function Workbench() {
  const [user, setUser] = useState<User | null>(null);
  const [authMode, setAuthMode] = useState<"login" | "register">("login");
  const [authError, setAuthError] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [progress, setProgress] = useState<ProgressItem[]>([]);
  const [trace, setTrace] = useState<Trace | null>(null);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [threadId, setThreadId] = useState(initialThreadId);
  const [interaction, setInteraction] = useState<any>(null);
  const [adminOpen, setAdminOpen] = useState(false);
  const [adminUsers, setAdminUsers] = useState<User[]>([]);
  const [employeeDashboard, setEmployeeDashboard] = useState<any>(null);
  const [adminDashboard, setAdminDashboard] = useState<any>(null);
  const [adminRequests, setAdminRequests] = useState<any[]>([]);
  const [adminFilters, setAdminFilters] = useState({ employee_id: "", status: "", date_from: "", date_to: "" });

  useEffect(() => {
    localStorage.setItem("supportflow_thread_id", threadId);
    requestJson("/auth/me").then((data) => setUser(data.user)).catch(() => setUser(null));
  }, [threadId]);

  useEffect(() => {
    if (!user) return;
    Promise.all([requestJson("/operations/my-dashboard"), requestJson("/operations/production-reports")]).then(([dashboard, reports]) => setEmployeeDashboard({ ...dashboard, production_reports: reports.reports || [] })).catch(() => setEmployeeDashboard(null));
  }, [user?.id]);

  const reset = () => {
    const next = newThreadId();
    setThreadId(next); setMessages([]); setProgress([]); setTrace(null); setInteraction(null);
  };

  const login = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const data = await requestJson("/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: form.get("username"), password: form.get("password") }) });
      setUser(data.user); setAuthError("");
    } catch (error) { setAuthError(error instanceof Error ? error.message : "登录失败"); }
  };

  const register = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      await requestJson("/auth/register", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username: form.get("username"), display_name: form.get("display_name"), department: form.get("department"), password: form.get("password") }) });
      setAuthMode("login"); setAuthError("注册成功，请直接登录。管理员可在后台调整你的权限。");
    } catch (error) { setAuthError(error instanceof Error ? error.message : "注册失败"); }
  };

  const logout = async () => { await requestJson("/auth/logout", { method: "POST" }); setUser(null); setAdminOpen(false); };

  const pushProgress = (message: string, status = "running") => {
    setProgress((items) => [...items.filter((item) => item.status !== "running"), { message, status, elapsed: status === "completed" ? "完成" : "进行中" }]);
  };

  const handleEvent = (event: string, data: any) => {
    if (event === "start") pushProgress("正在理解你的问题…");
    if (event === "progress") pushProgress(data.message || "正在处理…", data.status || "running");
    if (event === "route") pushProgress(`已理解问题：${data.intent || "业务请求"}`, "completed");
    if (event === "result" && data.result) setMessages((items) => [...items.filter((item) => item.role !== "assistant" || item.text !== "处理中…"), { role: "assistant", text: data.result }]);
    if (event === "rag") setTrace((old) => ({ ...old, ...data, evidence: { id: data.evidence_id, title: data.evidence_title, text: data.evidence_text } }));
    if (event === "metrics") setTrace((old) => ({ ...old, latency: { total_ms: data.total_latency_ms }, spans: data.spans, usage: data.llm_usage }));
    if (event === "trace") setTrace(data.trace);
    if (event === "refund_amount" || event === "hitl") { setInteraction({ type: event, ...data }); pushProgress(event === "hitl" ? "等待退款确认" : "等待填写退款金额"); }
    if (event === "terminal") {
      if (data.status === "PENDING_APPROVAL") pushProgress(data.message || "申请已提交，等待管理员审批", "submitted");
      else if (data.status === "NEED_USER_INPUT") pushProgress(data.message || "请补充必要信息", "needs_input");
      else pushProgress(data.message || "处理结束", data.status === "SUCCESS" ? "completed" : "failed");
    }
    if (event === "done" && !interaction) pushProgress("回答完成", "completed");
    if (event === "error") { setMessages((items) => [...items, { role: "error", text: data.user_message || data.message || "处理失败" }]); pushProgress(data.user_message || "处理失败", "failed"); }
  };

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const message = input.trim();
    if (!message || busy) return;
    setMessages((items) => [...items, { role: "user", text: message }, { role: "assistant", text: "处理中…" }]);
    setInput(""); setBusy(true); setProgress([]); setTrace(null); setInteraction(null);
    try {
      const response = await fetch(apiUrl("/chat/stream"), { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message, thread_id: threadId }) });
      if (!response.ok || !response.body) throw new Error("SupportFlow 后端不可用");
      const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = "";
      while (true) {
        const { value, done } = await reader.read(); if (done) break;
        buffer += decoder.decode(value, { stream: true }); const blocks = buffer.split("\n\n"); buffer = blocks.pop() || "";
        for (const block of blocks) {
          const eventName = block.match(/^event:\s*(.+)$/m)?.[1]; const payload = block.match(/^data:\s*(.+)$/m)?.[1];
          if (eventName && payload) handleEvent(eventName, JSON.parse(payload));
        }
      }
    } catch (error) { setMessages((items) => [...items.filter((item) => item.text !== "处理中…"), { role: "error", text: error instanceof Error ? error.message : "请求失败" }]); pushProgress("处理失败", "failed"); }
    finally { setBusy(false); }
  };

  const resume = async (payload: Record<string, any>) => {
    setBusy(true); setInteraction(null);
    try {
      const data = await requestJson("/chat/resume", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ thread_id: threadId, ...payload }) });
      if (data.result) setMessages((items) => [...items, { role: data.success ? "assistant" : "error", text: data.result }]);
      if (data.trace) setTrace(data.trace);
      if (data.interaction) setInteraction(data.interaction);
      else pushProgress(data.terminal_status === "SUCCESS" ? "处理完成" : data.result || "处理结束", data.success ? "completed" : "failed");
    } catch (error) { pushProgress(error instanceof Error ? error.message : "恢复失败", "failed"); }
    finally { setBusy(false); }
  };

  const loadAdmin = async (filters = adminFilters) => {
    const query = new URLSearchParams(Object.entries(filters).filter(([, value]) => value));
    const [users, dashboard, requests] = await Promise.all([
      requestJson("/admin/users"),
      requestJson(`/admin/operations/dashboard${query.toString() ? `?${query}` : ""}`),
      requestJson("/admin/operations/requests"),
    ]);
    setAdminUsers(users.users || []); setAdminDashboard(dashboard); setAdminRequests(dashboard.recent_requests || requests.requests || []); setAdminOpen(true);
  };
  const savePermissions = async (employee: User, role: string, permissions: string[]) => { await requestJson(`/admin/users/${employee.id}/permissions`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role, permissions }) }); await loadAdmin(); };
  const submitOperation = async (path: string, payload: Record<string, unknown>) => {
    try {
      const data = await requestJson(path, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": globalThis.crypto?.randomUUID?.() || `ui-${Date.now()}` }, body: JSON.stringify(payload) });
      setEmployeeDashboard((old: any) => old ? { ...old, requests: [data.request, ...(old.requests || [])], request_counts: { ...old.request_counts, SUBMITTED: (old.request_counts?.SUBMITTED || 0) + 1 } } : old);
      setMessages((items) => [...items, { role: "assistant", text: data.message || "申请已提交，等待管理员处理。" }]);
    } catch (error) { setMessages((items) => [...items, { role: "error", text: error instanceof Error ? error.message : "提交失败" }]); }
  };
  const productionAction = async (report: any, action: "change" | "withdraw") => {
    if (action === "withdraw") {
      await submitOperation(`/operations/production-reports/${report.id}/withdraw`, {});
      return;
    }
    const reported = window.prompt("新的完成数量", String(report.reported_quantity));
    const qualified = window.prompt("新的合格数量", String(report.qualified_quantity));
    const rejected = window.prompt("新的不良数量", String(report.rejected_quantity));
    if (reported === null || qualified === null || rejected === null) return;
    await submitOperation(`/operations/production-reports/${report.id}/change`, { reported_quantity: Number(reported), qualified_quantity: Number(qualified), rejected_quantity: Number(rejected) });
  };
  const decideOperation = async (item: any, approved: boolean) => {
    const reason = approved ? "管理员审核通过" : window.prompt("请输入驳回原因") || "";
    if (!approved && !reason.trim()) return;
    await requestJson(`/admin/operations/requests/${item.id}/decision`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ approved, reason }) });
    await loadAdmin();
  };
  const createAdminOrder = async (payload: Record<string, unknown>) => {
    try {
      const data = await requestJson("/admin/operations/orders", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      setMessages((items) => [...items, { role: "assistant", text: data.message || "管理员订单已创建。" }]);
      await loadAdmin();
    } catch (error) { setMessages((items) => [...items, { role: "error", text: error instanceof Error ? error.message : "管理员建单失败" }]); }
  };

  if (!user) return <AuthScreen mode={authMode} setMode={setAuthMode} onLogin={login} onRegister={register} error={authError} />;

  return <main className="shell">
    <header className="topbar"><div className="brand"><div className="brand-mark">YH</div><div><div className="eyebrow">INTERNAL OPERATIONS</div><h1>永好塑胶订单运营助手</h1><p>订单 · 生产 · 质检 · 库存</p></div></div><div className="toolbar"><span>{user.display_name || user.username}</span>{(user.permissions || []).includes("admin_users") && <button onClick={() => loadAdmin()}>管理看板</button>}<button onClick={logout}>退出</button><button onClick={reset}>新建会话</button></div></header>
    {adminOpen && <AdminPanel users={adminUsers} dashboard={adminDashboard} requests={adminRequests} filters={adminFilters} onFiltersChange={setAdminFilters} onRefresh={() => loadAdmin()} onClose={() => setAdminOpen(false)} onSave={savePermissions} onDecision={decideOperation} onCreateOrder={createAdminOrder} />}
    <EmployeeOperations dashboard={employeeDashboard} permissions={user.permissions || []} onSubmit={submitOperation} onProductionAction={productionAction} />
    <section className="layout"><section className="chat panel"><div className="conversation">{messages.length === 0 && <div className="empty"><strong>今天要处理什么？</strong><span>可以查询订单、库存、生产进度，或发起退款审批。</span></div>}{messages.map((message, index) => <div className={`message ${message.role}`} key={`${index}-${message.text}`}><div className="bubble">{message.text}</div></div>)}{progress.length > 0 && <div className="progress">{progress.map((item, index) => <div className={`progress-row ${item.status}`} key={`${item.message}-${index}`}><span>{item.status === "completed" ? "✓" : item.status === "failed" ? "✕" : item.status === "submitted" ? "→" : item.status === "needs_input" ? "?" : "●"}</span><span>{item.message}</span><small>{item.elapsed}</small></div>)}</div>}{interaction && <InteractionCard interaction={interaction} onResume={resume} />}</div><form className="composer" onSubmit={send}><textarea value={input} onChange={(event) => setInput(event.target.value)} placeholder="输入问题…（Enter 发送，Shift+Enter 换行）" disabled={busy} /><div><small>当前会话：{threadId.slice(0, 8)}</small><button disabled={busy || !input.trim()}>发送</button></div></form></section><aside className="trace panel"><div className="panel-heading"><div><div className="eyebrow">RUNTIME OBSERVABILITY</div><h2>处理详情</h2></div><span>{trace ? "已更新" : "等待请求"}</span></div>{trace ? <TraceView trace={trace} /> : <div className="empty-state">发送请求后显示处理详情。</div>}</aside></section>
  </main>;
}

function AuthScreen({ mode, setMode, onLogin, onRegister, error }: { mode: "login" | "register"; setMode: (mode: "login" | "register") => void; onLogin: (event: FormEvent<HTMLFormElement>) => void; onRegister: (event: FormEvent<HTMLFormElement>) => void; error: string }) {
  return <main className="auth-page"><header className="topbar"><div className="brand"><div className="brand-mark">YH</div><div><div className="eyebrow">INTERNAL OPERATIONS</div><h1>永好塑胶订单运营助手</h1><p>订单 · 生产 · 质检 · 库存</p></div></div><span>内部工作台</span></header><section className="auth-card"><div className="eyebrow">INTERNAL ACCOUNT</div><h2>{mode === "login" ? "登录内部系统" : "注册员工账号"}</h2><p>使用员工账号进入订单运营工作台，默认可查询并提交业务申请。</p>{mode === "login" ? <form onSubmit={onLogin}><input name="username" placeholder="用户名" autoComplete="username" required /><input name="password" type="password" placeholder="密码" autoComplete="current-password" required /><button>登录</button></form> : <form onSubmit={onRegister}><input name="username" placeholder="用户名" required minLength={3} /><input name="display_name" placeholder="姓名" required /><input name="department" placeholder="部门" /><input name="password" type="password" placeholder="密码（至少 8 位）" required minLength={8} /><button>提交注册</button></form>}{error && <div className="notice">{error}</div>}<button className="link" onClick={() => { setMode(mode === "login" ? "register" : "login"); }}> {mode === "login" ? "注册员工账号" : "返回登录"} </button></section><footer>账号、权限、订单、库存和审批均由 FastAPI 后端控制。</footer></main>;
}

function InteractionCard({ interaction, onResume }: { interaction: any; onResume: (payload: Record<string, any>) => void }) {
  if (interaction.type === "hitl") return <div className="interaction"><strong>等待退款确认</strong><p>{interaction.message}</p><button onClick={() => onResume({ confirmed: true })}>确认退款</button><button className="secondary" onClick={() => onResume({ confirmed: false })}>取消</button></div>;
  const refundable = Number(interaction.refundable_amount || 0);
  return <div className="interaction"><strong>等待填写退款金额</strong><p>退款订单：{interaction.order_id}</p><p>当前可退款金额：¥{refundable.toFixed(2)}</p><form onSubmit={(event) => { event.preventDefault(); const value = new FormData(event.currentTarget).get("amount"); onResume({ action: "amount", amount: value }); }}><input name="amount" inputMode="decimal" placeholder="请输入退款金额" required /><button type="button" className="secondary" onClick={() => onResume({ action: "amount", amount: refundable.toFixed(2) })}>全部退款</button><button>继续退款</button><button type="button" className="secondary" onClick={() => onResume({ action: "cancel_amount" })}>取消</button></form></div>;
}

function TraceView({ trace }: { trace: Trace }) { const evidence = trace.evidence || {}; const method = trace.terminal_status === "PENDING_APPROVAL" ? "等待管理员审批" : trace.terminal_status === "NEED_USER_INPUT" ? "等待补充信息" : trace.rag_source || "处理中"; return <div className="trace-view"><div><b>问题类型</b><span>{trace.intent || "处理中"}</span></div><div><b>参考资料</b><span>{evidence.title || trace.evidence_title || "暂无"}</span></div><div><b>回答判断</b><span>{trace.answerability_status || "处理中"}</span></div><div><b>回答方式</b><span>{method}</span></div>{evidence.text && <blockquote>{evidence.text}</blockquote>}<details><summary>查看工程详情</summary><pre>{JSON.stringify(trace, null, 2)}</pre></details></div>; }

function EmployeeOperations({ dashboard, permissions, onSubmit, onProductionAction }: { dashboard: any; permissions: string[]; onSubmit: (path: string, payload: Record<string, unknown>) => void; onProductionAction: (report: any, action: "change" | "withdraw") => void }) {
  const can = (permission: string) => permissions.includes(permission);
  return <section className="ops-grid">
    <div className="panel ops-card"><div className="panel-heading"><div><div className="eyebrow">MY WORK</div><h2>我的工作看板</h2></div></div><div className="stat-grid"><div><b>{dashboard?.production?.reported ?? 0}</b><span>累计完成</span></div><div><b>{dashboard?.quality?.inspected ?? 0}</b><span>累计检验</span></div><div><b>{dashboard?.request_counts?.SUBMITTED ?? 0}</b><span>待审批申请</span></div></div><div className="request-list">{(dashboard?.requests || []).slice(0, 5).map((item: any) => <div key={item.id}><b>{item.request_no}</b><span>{operationType(item.request_type)} · {item.status}</span></div>)}</div>{(dashboard?.production_reports || []).length > 0 && <div className="request-list"><strong>已审批报工</strong>{dashboard.production_reports.slice(0, 5).map((report: any) => <div key={report.id}><span>{report.order_no} · 完成 {report.reported_quantity}</span><span><button className="secondary" onClick={() => onProductionAction(report, "change")}>申请修改</button><button className="secondary" onClick={() => onProductionAction(report, "withdraw")}>申请撤回</button></span></div>)}</div>}</div>
    <div className="panel ops-card"><div className="panel-heading"><div><div className="eyebrow">QUICK ACTIONS</div><h2>快捷申报</h2></div></div><div className="quick-forms">{can("submit_order") && <OperationForm title="提交客户订单" fields={["customer_code", "sku", "quantity", "required_date"]} labels={{ customer_code: "客户编号", sku: "产品 SKU", quantity: "需求数量", required_date: "交期" }} onSubmit={(payload) => onSubmit("/operations/order-submissions", payload)} />}{can("submit_production_report") && <OperationForm title="提交生产报工" fields={["order_no", "reported_quantity", "qualified_quantity", "rejected_quantity"]} labels={{ order_no: "订单号", reported_quantity: "完成数量", qualified_quantity: "合格数量", rejected_quantity: "不良数量" }} onSubmit={(payload) => onSubmit("/operations/production-reports", payload)} />}{can("submit_quality_report") && <OperationForm title="提交质检报工" fields={["order_no", "inspected_quantity", "qualified_quantity", "rejected_quantity"]} labels={{ order_no: "订单号", inspected_quantity: "检验数量", qualified_quantity: "合格数量", rejected_quantity: "不合格数量" }} onSubmit={(payload) => onSubmit("/operations/quality-reports", payload)} />}{can("request_outbound") && <OperationForm title="提交出库申请" fields={["order_no", "quantity"]} labels={{ order_no: "订单号", quantity: "出库数量" }} onSubmit={(payload) => onSubmit("/operations/outbound-requests", payload)} />}</div></div>
  </section>;
}

function OperationForm({ title, fields, labels, onSubmit }: { title: string; fields: string[]; labels: Record<string, string>; onSubmit: (payload: Record<string, unknown>) => void }) {
  return <form className="operation-form" onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); const payload: Record<string, unknown> = {}; fields.forEach((field) => { const value = String(data.get(field) || "").trim(); payload[field] = ["quantity", "reported_quantity", "qualified_quantity", "rejected_quantity", "inspected_quantity"].includes(field) ? Number(value) : value; }); onSubmit(payload); event.currentTarget.reset(); }}><strong>{title}</strong>{fields.map((field) => <input key={field} name={field} placeholder={labels[field]} required={field !== "qualified_quantity" && field !== "rejected_quantity"} />)}<button>提交申请</button></form>;
}

function operationType(value: string) { return ({ order_submission: "订单", production_report: "报工", production_report_change: "修改报工", production_report_withdraw: "撤回报工", quality_report: "质检", outbound_request: "出库" } as Record<string, string>)[value] || value; }

function AdminPanel({ users, dashboard, requests, filters, onFiltersChange, onRefresh, onClose, onSave, onDecision, onCreateOrder }: { users: User[]; dashboard: any; requests: any[]; filters: { employee_id: string; status: string; date_from: string; date_to: string }; onFiltersChange: (filters: { employee_id: string; status: string; date_from: string; date_to: string }) => void; onRefresh: () => void; onClose: () => void; onSave: (user: User, role: string, permissions: string[]) => void; onDecision: (item: any, approved: boolean) => void; onCreateOrder: (payload: Record<string, unknown>) => void }) {
  return <section className="admin panel"><div className="panel-heading"><div><div className="eyebrow">ADMIN CONSOLE</div><h2>运营管理看板</h2></div><button className="secondary" onClick={onClose}>返回对话</button></div><div className="filter-bar"><select value={filters.employee_id} onChange={(event) => onFiltersChange({ ...filters, employee_id: event.target.value })}><option value="">全部员工</option>{users.filter((user) => user.role !== "admin").map((user) => <option key={user.id} value={user.id}>{user.display_name}</option>)}</select><select value={filters.status} onChange={(event) => onFiltersChange({ ...filters, status: event.target.value })}><option value="">全部申请状态</option><option value="SUBMITTED">待审批</option><option value="APPROVED">已批准</option><option value="REJECTED">已驳回</option><option value="WITHDRAWN">已撤回</option></select><input type="date" value={filters.date_from} onChange={(event) => onFiltersChange({ ...filters, date_from: event.target.value })} /><input type="date" value={filters.date_to} onChange={(event) => onFiltersChange({ ...filters, date_to: event.target.value })} /><button onClick={onRefresh}>应用筛选</button></div><div className="stat-grid admin-stats"><div><b>{dashboard?.inventory?.available ?? 0}</b><span>可用库存</span></div><div><b>{dashboard?.production?.ordered ?? 0}</b><span>订单需求</span></div><div><b>{dashboard?.production?.completed ?? 0}</b><span>已完成</span></div><div><b>{dashboard?.quality?.rejected ?? 0}</b><span>质检不良</span></div></div><div className="admin-actions"><h3>管理员直接建单</h3><OperationForm title="不经过员工审批，直接创建已批准订单" fields={["customer_code", "sku", "quantity", "required_date"]} labels={{ customer_code: "客户编号", sku: "产品 SKU", quantity: "需求数量", required_date: "交期" }} onSubmit={onCreateOrder} /></div><h3>订单进度总表</h3><div className="order-table"><div className="order-row order-head"><span>订单 / 客户</span><span>需求</span><span>完成</span><span>出库</span><span>状态</span></div>{(dashboard?.orders || []).map((order: any) => <div className="order-row" key={order.order_no}><span><b>{order.order_no}</b><small>{order.customer_name} · {order.sku}</small></span><span>{order.ordered_quantity}</span><span>{order.completed_quantity}</span><span>{order.shipped_quantity}</span><span>{order.status}</span></div>)}</div><h3>员工工作量</h3><div className="request-table">{(dashboard?.employee_workload || []).map((item: any) => <div className="request-row" key={item.id}><span><b>{item.display_name}</b><small>{item.username}</small></span><span>报工 {item.production_requests} · 质检 {item.quality_requests} · 待审 {item.pending_requests}</span></div>)}</div><h3>待处理申请</h3><div className="request-table">{requests.filter((item) => item.status === "SUBMITTED").map((item) => <div className="request-row" key={item.id}><span><b>{item.request_no}</b><small>{operationType(item.request_type)} · {item.requester_name || item.requester_username}</small></span><span><button onClick={() => onDecision(item, true)}>批准</button><button className="secondary" onClick={() => onDecision(item, false)}>驳回</button></span></div>)}{requests.every((item) => item.status !== "SUBMITTED") && <small>暂无待审批申请</small>}</div><h3>员工权限</h3>{users.map((user) => <div className="admin-user" key={user.id}><strong>{user.display_name}</strong><small>{user.username}</small><select defaultValue={user.role || "employee"} data-role={user.id}>{Object.entries(roleLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select><div className="permissions">{Object.entries(permissionLabels).map(([key, label]) => <label key={key}><input type="checkbox" defaultChecked={user.permissions?.includes(key)} data-permission={key} />{label}</label>)}</div><button onClick={(event) => { const card = (event.currentTarget as HTMLButtonElement).parentElement; const selected = [...(card?.querySelectorAll("input:checked") || [])].map((item) => (item as HTMLInputElement).dataset.permission!).filter(Boolean); const role = (card?.querySelector("select[data-role]") as HTMLSelectElement)?.value || "employee"; onSave(user, role, selected); }}>保存权限</button></div>)}</section>;
}
