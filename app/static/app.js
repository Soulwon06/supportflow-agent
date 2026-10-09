const conversation = document.getElementById("conversation");
const tracePanel = document.getElementById("tracePanel");
const traceStatus = document.getElementById("traceStatus");
const progress = document.getElementById("progress");
const hitlCard = document.getElementById("hitlCard");
const composer = document.getElementById("composer");
const input = document.getElementById("messageInput");
const sendButton = document.getElementById("sendButton");
const resetButton = document.getElementById("resetButton");
const threadIdLabel = document.getElementById("threadIdLabel");
const authPanel = document.getElementById("authPanel");
const appShell = document.getElementById("appShell");
const currentUser = document.getElementById("currentUser");
const loginForm = document.getElementById("loginForm");
const registerForm = document.getElementById("registerForm");
const authMessage = document.getElementById("authMessage");
const showRegisterButton = document.getElementById("showRegister");
const cancelRegisterButton = document.getElementById("cancelRegister");
const logoutButton = document.getElementById("logoutButton");
const adminButton = document.getElementById("adminButton");
const adminPanel = document.getElementById("adminPanel");
const adminUsers = document.getElementById("adminUsers");
const closeAdmin = document.getElementById("closeAdmin");

const INTENT_LABELS = {
  knowledge: "知识咨询",
  manufacturing_knowledge: "制造规范咨询",
  quality_mixed: "订单质检与处理建议",
  order: "订单查询",
  logistics: "物流查询",
  refund: "退款申请",
  inventory: "库存查询",
  production: "生产进度",
  quality_submit: "质检申报",
  order_entry: "上单申请",
  unknown: "其他问题",
};

const ANSWERABILITY_LABELS = {
  SUPPORTED: "资料足够，可以回答",
  PARTIAL: "资料只能回答部分内容",
  UNSUPPORTED: "当前资料不足，已避免推测回答",
};

const PROGRESS_ORDER = ["intent", "reranker_init", "retrieval", "rerank", "judge", "generation", "refund_amount", "confirmation", "submitted", "needs_input", "complete", "cancelled", "permission_denied", "safe_fallback", "failure"];

let threadId = localStorage.getItem("supportflow_thread_id") || crypto.randomUUID();
let busy = false;
let activeAssistant = null;
let latestTrace = null;
let progressRun = null;
let pendingInteraction = false;
threadIdLabel.textContent = threadId;

const ADMIN_PERMISSION_LABELS = {
  query_order: "查询订单",
  query_logistics: "查询物流",
  query_inventory: "查询库存",
  query_production: "查询生产进度",
  create_sales_order: "提交订单",
  submit_order: "提交客户订单",
  submit_production_report: "提交生产报工",
  submit_quality_report: "提交质检报工",
  request_outbound: "提交出库申请",
  admin_users: "管理员工账号",
  admin_data: "查看管理数据",
};

function showAuthMessage(message, visible = true) {
  authMessage.textContent = message || "";
  authMessage.classList.toggle("hidden", !visible);
}

function setAuthenticated(user) {
  authPanel.classList.add("hidden");
  appShell.classList.remove("hidden");
  currentUser.textContent = `${user.display_name || user.username} · ${user.department || user.role || "员工"}`;
  logoutButton.classList.remove("hidden");
  adminButton.classList.toggle("hidden", !(user.permissions || []).includes("admin_users"));
}

function setLoggedOut() {
  authPanel.classList.remove("hidden");
  appShell.classList.add("hidden");
  adminPanel.classList.add("hidden");
  currentUser.textContent = "未登录";
  logoutButton.classList.add("hidden");
  adminButton.classList.add("hidden");
}

async function initAuth() {
  try {
    const response = await fetch("/auth/me");
    if (!response.ok) throw new Error("未登录");
    const data = await response.json();
    setAuthenticated(data.user);
  } catch (_) {
    setLoggedOut();
  }
}

async function loadAdminUsers() {
  const response = await fetch("/admin/users");
  if (!response.ok) throw new Error("没有管理员权限");
  const data = await response.json();
  adminUsers.innerHTML = (data.users || []).map((user) => `
    <div class="admin-user" data-user-id="${user.id}">
      <div class="admin-user-title"><strong>${escapeHtml(user.display_name)}</strong><span class="muted">${escapeHtml(user.username)} · ${escapeHtml(user.last_seen_at || "从未登录")}</span></div>
      <div class="permission-grid">${Object.entries(ADMIN_PERMISSION_LABELS).map(([key, label]) => `
        <label><input type="checkbox" data-permission="${key}" ${(user.permissions || []).includes(key) ? "checked" : ""} />${label}</label>`).join("")}</div>
      <button class="button secondary save-permissions" type="button">保存权限</button>
    </div>`).join("") || '<p class="muted">暂无员工账号。</p>';
}

async function saveUserPermissions(button) {
  const card = button.closest(".admin-user");
  const permissions = [...card.querySelectorAll("input[data-permission]:checked")].map((input) => input.dataset.permission);
  const response = await fetch(`/admin/users/${card.dataset.userId}/permissions`, {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({permissions}),
  });
  if (!response.ok) throw new Error("权限保存失败");
  button.textContent = "已保存";
  setTimeout(() => { button.textContent = "保存权限"; }, 1200);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#039;"}[char]));
}

function addMessage(kind, text) {
  const wrapper = document.createElement("div");
  wrapper.className = `message ${kind}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrapper.appendChild(bubble);
  conversation.appendChild(wrapper);
  conversation.scrollTop = conversation.scrollHeight;
  return bubble;
}

function setBusy(value) {
  busy = value;
  sendButton.disabled = value;
  input.disabled = value;
  if (!value) input.focus();
}

function formatElapsed(milliseconds) {
  return `${(Math.max(0, milliseconds) / 1000).toFixed(1)}s`;
}

function elapsedNow() {
  return performance.now() - progressRun.startedAt;
}

function startProgressRun() {
  if (progressRun?.timerId) clearInterval(progressRun.timerId);
  progress.classList.remove("has-error");
  progressRun = {startedAt: performance.now(), timerId: null, steps: new Map(), structure: "", rows: new Map(), terminalStatus: null};
  progressRun.timerId = setInterval(renderProgress, 150);
  renderProgress();
}

function stopProgressRun() {
  if (!progressRun) return;
  if (progressRun.timerId) clearInterval(progressRun.timerId);
  progressRun.timerId = null;
}

function progressStep(stage) {
  if (!progressRun.steps.has(stage)) {
    progressRun.steps.set(stage, {
      stage,
      status: "running",
      message: "处理中…",
      startedAt: performance.now(),
      elapsedMs: null,
    });
  }
  return progressRun.steps.get(stage);
}

function handleProgress(data) {
  if (!progressRun) startProgressRun();
  const now = performance.now();
  const step = progressStep(data.stage);
  if (step.startedAt == null) step.startedAt = now;
  step.status = data.status || "running";
  step.message = data.message || step.message;
  step.elapsedMs = (step.status === "completed" || step.status === "failed")
    ? now - step.startedAt
    : null;
  renderProgress();
  if (step.status === "failed") progress.classList.add("has-error");
}

function finishProgress(status, message) {
  if (!progressRun) return;
  if (progressRun.terminalStatus) return;
  const now = performance.now();
  const stage = status === "completed"
    ? "complete"
    : status === "cancelled"
      ? "cancelled"
      : status === "permission_denied"
        ? "permission_denied"
        : status === "safe_fallback"
          ? "safe_fallback"
          : status === "submitted"
            ? "submitted"
            : status === "needs_input"
              ? "needs_input"
              : "failure";
  const step = progressStep(stage);
  step.status = status === "completed" ? "completed" : status;
  step.message = message;
  // The completion/failure row represents the whole request, not a backend stage.
  step.startedAt = progressRun.startedAt;
  step.elapsedMs = now - progressRun.startedAt;
  progressRun.terminalStatus = status;
  renderProgress();
  stopProgressRun();
}

function renderProgress() {
  if (!progressRun) {
    progress.classList.add("hidden");
    progress.innerHTML = "";
    return;
  }
  const steps = [...progressRun.steps.values()].sort(
    (left, right) => PROGRESS_ORDER.indexOf(left.stage) - PROGRESS_ORDER.indexOf(right.stage),
  );
  const elapsed = elapsedNow();
  const structure = steps.length ? steps.map((step) => step.stage).join("|") : "__waiting__";
  if (progressRun.structure !== structure) {
    progress.innerHTML = `<div class="progress-list">${steps.length ? steps.map((step) => `<div class="progress-step" data-stage="${escapeHtml(step.stage)}"><span class="progress-icon"></span><span class="progress-message"></span><span class="progress-time"></span></div>`).join("") : '<div class="progress-step" data-stage="__waiting__"><span class="progress-icon"></span><span class="progress-message"></span><span class="progress-time"></span></div>'}</div>`;
    progressRun.rows = new Map([...progress.querySelectorAll("[data-stage]")].map((row) => [row.dataset.stage, row]));
    progressRun.structure = structure;
  }
  if (!steps.length) {
    const row = progressRun.rows.get("__waiting__");
    row.className = "progress-step running";
    row.querySelector(".progress-icon").textContent = "●";
    row.querySelector(".progress-message").textContent = "正在准备处理…";
    row.querySelector(".progress-time").textContent = formatElapsed(elapsed);
  }
  for (const step of steps) {
    const row = progressRun.rows.get(step.stage);
    const icon = step.status === "completed"
      ? "✓"
      : step.status === "cancelled"
        ? "○"
      : ["failed", "permission_denied", "safe_fallback"].includes(step.status)
          ? "✕"
          : step.status === "submitted"
            ? "→"
            : step.status === "needs_input"
              ? "?"
          : "●";
    const isOverall = ["complete", "submitted", "needs_input", "failure"].includes(step.stage);
    const stageElapsed = step.elapsedMs ?? (
      step.startedAt == null ? elapsed : elapsed - step.startedAt + progressRun.startedAt
    );
    row.className = `progress-step ${step.status}`;
    row.querySelector(".progress-icon").textContent = icon;
    row.querySelector(".progress-message").textContent = step.message;
    row.querySelector(".progress-time").textContent = formatElapsed(
      isOverall ? (step.elapsedMs ?? elapsed) : stageElapsed,
    );
  }
  progress.classList.remove("hidden");
}

function setTrace(trace) {
  if (!trace) return;
  latestTrace = {...latestTrace, ...trace};
  renderTrace(latestTrace);
}

function listFacts(items) {
  if (!items || !items.length) return '<span class="muted">无</span>';
  return `<ul class="facts">${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function formatTokens(value) {
  if (!value) return "N/A";
  return `${value.total_tokens ?? "N/A"} total (in ${value.input_tokens ?? "N/A"}, out ${value.output_tokens ?? "N/A"})`;
}

function answerabilityLabel(status, trace) {
  if (status && ANSWERABILITY_LABELS[status]) return ANSWERABILITY_LABELS[status];
  if (trace.safe_fallback_reason) return "资料判断未完成，已安全停止";
  return "尚未完成判断";
}

function answerMethod(trace) {
  if (trace.terminal_status === "PENDING_APPROVAL") return "等待管理员审批";
  if (trace.terminal_status === "NEED_USER_INPUT") return "等待补充信息";
  if (trace.terminal_status === "PERMISSION_DENIED") return "权限拒绝";
  if (trace.terminal_status === "CANCELLED") return "已取消";
  if (trace.terminal_status === "FAILED") return "处理失败";
  if (trace.terminal_status === "SAFE_FALLBACK") return "安全兜底";
  if (trace.rag_source === "llm") return "基于知识库生成";
  if (trace.rag_source === "answerability_abstention" || trace.rag_source === "safe_fallback") return "安全兜底";
  if (trace.intent === "order" || trace.intent === "logistics") return "工具查询";
  if (trace.intent === "refund" || trace.need_confirmation) return "人工确认";
  return "后端处理";
}

function formatCurrency(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `¥${number.toFixed(2)}` : "N/A";
}

function spanRows(spans) {
  return (spans || []).map((span) => `<div class="kv"><span class="key">${escapeHtml(span.operation || span.name)}</span><span class="value">${span.latency_ms ?? "N/A"} ms · ${span.success ? "ok" : "failed"}</span></div>`).join("");
}

function renderTrace(trace) {
  const evidence = trace.evidence || {};
  const status = trace.answerability_status;
  const latency = trace.latency || {};
  const usage = trace.usage || {};
  const stages = latency.stages_ms || {};
  const details = `
    <details class="trace-details">
      <summary>查看工程详情</summary>
      <div class="trace-engineering">
        <section class="trace-section"><h3>Runtime</h3><div class="kv">
          <span class="key">Trace ID</span><span class="value"><code>${escapeHtml(trace.trace_id || "N/A")}</code></span>
          <span class="key">Intent</span><span class="value">${escapeHtml(trace.intent || "N/A")}</span>
          <span class="key">Routing Source</span><span class="value">${escapeHtml(trace.routing_source || "N/A")}</span>
          <span class="key">RAG Source</span><span class="value">${escapeHtml(trace.rag_source || "N/A")}</span>
        </div></section>
        <section class="trace-section"><h3>Refund Flow</h3><div class="kv">
          <span class="key">Pending Action</span><span class="value">${escapeHtml(trace.pending_action || "N/A")}</span>
          <span class="key">Missing Fields</span><span class="value">${escapeHtml((trace.missing_fields || []).join(", ") || "N/A")}</span>
          <span class="key">Order ID</span><span class="value">${escapeHtml(trace.order_id || "N/A")}</span>
          <span class="key">Refund Amount</span><span class="value">${trace.refund_amount == null ? "N/A" : formatCurrency(trace.refund_amount)}</span>
          <span class="key">Refundable Amount</span><span class="value">${trace.refundable_amount == null ? "N/A" : formatCurrency(trace.refundable_amount)}</span>
          <span class="key">Cancelled</span><span class="value">${trace.refund_cancelled ? "YES" : "NO"}</span>
        </div></section>
        <section class="trace-section"><h3>Evidence / Rerank</h3><div class="kv">
          <span class="key">Evidence ID</span><span class="value">${escapeHtml(evidence.id || "N/A")}</span>
          <span class="key">Category</span><span class="value">${escapeHtml(evidence.category || "N/A")}</span>
          <span class="key">Version</span><span class="value">${escapeHtml(evidence.version || "N/A")}</span>
          <span class="key">Rerank Score</span><span class="value">${trace.rerank_score ?? "N/A"}</span>
        </div>${evidence.text ? `<div class="evidence-text">${escapeHtml(evidence.text)}</div>` : ""}</section>
        <section class="trace-section"><h3>Answerability / Generation</h3><div class="kv">
          <span class="key">Answerability</span><span class="value">${escapeHtml(status || "N/A")}</span>
          <span class="key">Supported Facts</span><span class="value">${listFacts(trace.supported_facts)}</span>
          <span class="key">Missing Facts</span><span class="value">${listFacts(trace.missing_facts)}</span>
          <span class="key">Generation Called</span><span class="value">${trace.generation_skipped ? "NO (skipped)" : "YES"}</span>
          <span class="key">Safe Fallback Reason</span><span class="value">${escapeHtml(trace.safe_fallback_reason || "N/A")}</span>
        </div></section>
        <section class="trace-section"><h3>Latency</h3><div class="kv">
          <span class="key">Intent</span><span class="value">${stages.intent ?? "N/A"} ms</span>
          <span class="key">Retrieval</span><span class="value">${stages.retrieval ?? "N/A"} ms</span>
          <span class="key">Rerank</span><span class="value">${stages.rerank ?? "N/A"} ms</span>
          <span class="key">Judge</span><span class="value">${stages.answerability_judge ?? "N/A"} ms</span>
          <span class="key">Generator</span><span class="value">${stages.grounded_generation ?? "N/A"} ms</span>
          <span class="key">Total</span><span class="value">${latency.total_ms ?? "N/A"} ms</span>
        </div></section>
        <section class="trace-section"><h3>Token Usage</h3><div class="kv">
          <span class="key">Judge</span><span class="value">${formatTokens(usage.judge)}</span>
          <span class="key">Generator</span><span class="value">${formatTokens(usage.generator)}</span>
          <span class="key">Total</span><span class="value">${formatTokens(usage.total)}</span>
        </div></section>
        <section class="trace-section"><h3>Spans</h3>${spanRows(trace.spans) || '<span class="muted">N/A</span>'}</section>
      </div>
    </details>`;
  const fallbackNote = trace.safe_fallback_reason ? `<div class="trace-note warning">${escapeHtml(trace.safe_fallback_reason)}</div>` : "";
  tracePanel.classList.remove("empty-state");
  tracePanel.innerHTML = `
    <section class="trace-summary">
      <div class="summary-row"><span class="key">问题类型</span><span class="value">${escapeHtml(INTENT_LABELS[trace.intent] || "处理中")}</span></div>
      <div class="summary-row"><span class="key">参考资料</span><span class="value">${escapeHtml(evidence.title || "未找到参考资料")}</span></div>
      <div class="summary-row"><span class="key">回答判断</span><span class="value">${escapeHtml(answerabilityLabel(status, trace))}</span></div>
      <div class="summary-row"><span class="key">回答方式</span><span class="value">${escapeHtml(answerMethod(trace))}</span></div>
      <div class="summary-row"><span class="key">后端总耗时</span><span class="value">${latency.total_ms == null ? "N/A" : `${(latency.total_ms / 1000).toFixed(1)} 秒`}</span></div>
      ${fallbackNote}
    </section>${details}`;
  traceStatus.textContent = trace.trace_id ? `trace ${trace.trace_id.slice(0, 8)}` : "runtime trace";
}

function processEvent(event, data) {
  if (event === "start") handleProgress({stage: "intent", status: "running", message: "正在理解你的问题…"});
  if (event === "progress") handleProgress(data);
  if (event === "result" && data.result) {
    if (!activeAssistant) activeAssistant = addMessage("assistant", data.result);
    else activeAssistant.textContent = data.result;
  }
  if (event === "rag") setTrace({...(latestTrace || {}), answerability_status: data.answerability_status, evidence: {id: data.evidence_id, title: data.evidence_title, category: data.evidence_category, version: data.evidence_version, text: data.evidence_text}, rerank_score: data.rerank_score, generation_skipped: data.generation_skipped, safe_fallback_reason: data.safe_fallback_reason, rag_source: data.rag_source});
  if (event === "refund_amount") {
    pendingInteraction = true;
    handleProgress({stage: "refund_amount", status: "running", message: "等待填写退款金额"});
    showRefundAmount(data);
  }
  if (event === "metrics") {
    const stages = {};
    for (const span of (data.spans || [])) {
      if (span.operation === "intent_classification") stages.intent = span.latency_ms;
      else if (span.operation === "answerability_judge") stages.answerability_judge = span.latency_ms;
      else if (span.operation === "grounded_generation") stages.grounded_generation = span.latency_ms;
      else if (span.name === "retrieval" || span.name === "rerank") stages[span.name] = span.latency_ms;
    }
    setTrace({...(latestTrace || {}), latency: {total_ms: data.total_latency_ms, stages_ms: stages}, usage: data.llm_usage, spans: data.spans});
  }
  if (event === "trace") setTrace(data.trace);
  if (event === "terminal") {
    const status = data.status;
    if (status === "PENDING_APPROVAL") finishProgress("submitted", data.message || "申请已提交，等待管理员审批");
    else if (status === "NEED_USER_INPUT") finishProgress("needs_input", data.message || "请补充必要信息");
    else if (status === "CANCELLED") finishProgress("cancelled", "已取消退款");
    else if (status === "PERMISSION_DENIED") finishProgress("permission_denied", "当前角色没有退款权限");
    else if (status === "SAFE_FALLBACK") finishProgress("safe_fallback", "已安全停止：资料不足或服务不可用");
    else if (status === "FAILED") finishProgress("failed", data.message || "处理失败");
  }
  if (event === "hitl") {
    pendingInteraction = true;
    handleProgress({stage: "confirmation", status: "running", message: "等待人工确认…"});
    showHitl(data);
  }
  if (event === "done" && !pendingInteraction && !progressRun?.terminalStatus) finishProgress("completed", "处理完成");
  if (event === "error") {
    if (!activeAssistant) activeAssistant = addMessage("error", data.user_message || data.message || "后端请求失败，请稍后重试。");
    const status = data.terminal_status;
    if (status === "PERMISSION_DENIED") finishProgress("permission_denied", data.user_message || "当前角色没有退款权限");
    else if (status === "CANCELLED") finishProgress("cancelled", "已取消退款");
    else if (status === "SAFE_FALLBACK") finishProgress("safe_fallback", data.user_message || "已安全停止");
    else finishProgress("failed", data.user_message || "处理失败");
  }
}

function showHitl(data) {
  pendingInteraction = true;
  hitlCard.innerHTML = `<strong>等待退款确认</strong><div>${escapeHtml(data.message || "确认继续执行吗？")}</div><div class="hitl-actions"><button class="button primary" id="confirmRefund">确认退款</button><button class="button secondary" id="cancelRefund">取消</button></div>`;
  hitlCard.classList.remove("hidden");
  handleProgress({stage: "confirmation", status: "running", message: "等待退款确认"});
  document.getElementById("confirmRefund").onclick = () => resumeRefund(true);
  document.getElementById("cancelRefund").onclick = () => resumeRefund(false);
}

function showRefundAmount(data) {
  pendingInteraction = true;
  const orderId = data.order_id || "当前订单";
  const orderTotal = Number(data.order_total);
  const refundable = Number(data.refundable_amount);
  hitlCard.innerHTML = `
    <strong>等待填写退款金额</strong>
    <div>退款订单：${escapeHtml(orderId)}</div>
    <div>订单金额 / 当前可退款金额：${formatCurrency(orderTotal)} / ${formatCurrency(refundable)}</div>
    <form id="refundAmountForm" class="refund-form">
      <label>退款金额：¥ <input id="refundAmountInput" inputmode="decimal" autocomplete="off" placeholder="请输入退款金额" /></label>
      <div id="refundAmountError" class="trace-note warning hidden"></div>
      <div class="hitl-actions">
        <button type="button" class="button secondary" id="fullRefund">全部退款 ${formatCurrency(refundable)}</button>
        <button type="submit" class="button primary">继续退款</button>
        <button type="button" class="button secondary" id="cancelAmount">取消</button>
      </div>
    </form>`;
  hitlCard.classList.remove("hidden");
  handleProgress({stage: "refund_amount", status: "running", message: "等待填写退款金额"});
  const amountInput = document.getElementById("refundAmountInput");
  const errorBox = document.getElementById("refundAmountError");
  document.getElementById("fullRefund").onclick = () => {
    amountInput.value = Number.isFinite(refundable) ? refundable.toFixed(2) : "";
    errorBox.classList.add("hidden");
  };
  document.getElementById("cancelAmount").onclick = () => resumeRefundAmount("cancel_amount");
  document.getElementById("refundAmountForm").onsubmit = (event) => {
    event.preventDefault();
    const value = Number(amountInput.value);
    if (!Number.isFinite(value) || value <= 0 || value > refundable) {
      errorBox.textContent = value > refundable
        ? `退款金额不能超过当前可退款金额 ${formatCurrency(refundable)}。`
        : "请输入大于 0 的有效退款金额。";
      errorBox.classList.remove("hidden");
      return;
    }
    resumeRefundAmount(String(amountInput.value).trim());
  };
  amountInput.focus();
}

async function handleRefundResumeResponse(data) {
  setTrace(data.trace);
  if (data.result && data.interaction?.type !== "refund_amount_required") {
    addMessage(data.success ? "assistant" : "error", data.result);
  }
  if (data.interaction?.type === "refund_amount_required") {
    showRefundAmount(data.interaction);
    return;
  }
  if (data.interaction?.type === "refund_confirmation") {
    showHitl(data.interaction);
    return;
  }
  pendingInteraction = false;
  const status = data.terminal_status;
  if (status === "CANCELLED") finishProgress("cancelled", "已取消退款");
  else if (status === "PERMISSION_DENIED") finishProgress("permission_denied", "当前角色没有退款权限");
  else if (status === "SAFE_FALLBACK") finishProgress("safe_fallback", data.result || "已安全停止");
  else finishProgress(data.success ? "completed" : "failed", data.success ? "退款流程已完成" : (data.result || "退款处理失败"));
}

async function resumeRefundAmount(actionOrAmount) {
  hitlCard.classList.add("hidden");
  setBusy(true);
  startProgressRun();
  handleProgress({stage: "refund_amount", status: "running", message: "正在校验退款金额…"});
  const payload = actionOrAmount === "cancel_amount"
    ? {thread_id: threadId, action: "cancel_amount"}
    : {thread_id: threadId, action: "amount", amount: actionOrAmount};
  try {
    const response = await fetch("/chat/resume", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "退款金额处理失败");
    await handleRefundResumeResponse(data);
  } catch (error) {
    addMessage("error", error.message || "退款金额处理失败");
    finishProgress("failed", "退款处理失败");
  } finally { setBusy(false); }
}

async function resumeRefund(confirmed) {
  hitlCard.classList.add("hidden");
  setBusy(true);
  startProgressRun();
  handleProgress({stage: "confirmation", status: "running", message: "正在通过后端 checkpoint 恢复退款流程…"});
  try {
    const response = await fetch("/chat/resume", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({thread_id: threadId, confirmed})});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "后端恢复失败");
    addMessage(data.terminal_status === "FAILED" || data.terminal_status === "PERMISSION_DENIED" ? "error" : "assistant", data.result || "后端流程已完成。");
    setTrace(data.trace);
    const status = data.terminal_status;
    if (status === "CANCELLED") finishProgress("cancelled", "已取消退款");
    else if (status === "PERMISSION_DENIED") finishProgress("permission_denied", "当前角色没有退款权限");
    else if (status === "FAILED") finishProgress("failed", data.result || "处理失败");
    else finishProgress("completed", "处理完成");
  } catch (error) {
    addMessage("error", error.message || "后端恢复失败");
    finishProgress("failed", "处理失败");
  } finally { setBusy(false); }
}

async function sendMessage() {
  const message = input.value.trim();
  if (!message || busy) return;
  addMessage("user", message);
  input.value = "";
  activeAssistant = null;
  latestTrace = null;
  pendingInteraction = false;
  hitlCard.classList.add("hidden");
  tracePanel.className = "trace-panel empty-state";
  tracePanel.textContent = "等待后端处理详情…";
  setBusy(true);
  startProgressRun();
  try {
    const response = await fetch("/chat/stream", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({message, thread_id: threadId})});
    if (!response.ok || !response.body) throw new Error("SupportFlow 后端不可用");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop();
      for (const block of blocks) {
        const eventLine = block.split("\n").find((line) => line.startsWith("event:"));
        const dataLine = block.split("\n").find((line) => line.startsWith("data:"));
        if (!eventLine || !dataLine) continue;
        try { processEvent(eventLine.slice(6).trim(), JSON.parse(dataLine.slice(5).trim())); } catch (_) { /* malformed event is shown only as generic UI error */ }
      }
    }
  } catch (error) {
    addMessage("error", error.message || "请求失败，请检查后端服务。");
    finishProgress("failed", "处理失败，请检查服务连接");
  } finally { setBusy(false); }
}

function resetConversation() {
  threadId = crypto.randomUUID();
  localStorage.setItem("supportflow_thread_id", threadId);
  threadIdLabel.textContent = threadId;
  conversation.innerHTML = "";
  hitlCard.classList.add("hidden");
  tracePanel.className = "trace-panel empty-state";
  tracePanel.textContent = "发送请求后显示处理详情。";
  traceStatus.textContent = "等待请求";
  if (progressRun?.timerId) clearInterval(progressRun.timerId);
  progressRun = null;
  renderProgress();
  latestTrace = null;
  pendingInteraction = false;
}

composer.addEventListener("submit", (event) => { event.preventDefault(); sendMessage(); });
input.addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); } });
resetButton.addEventListener("click", resetConversation);
showRegisterButton.addEventListener("click", () => {
  loginForm.classList.add("hidden");
  showRegisterButton.classList.add("hidden");
  registerForm.classList.remove("hidden");
  showAuthMessage("");
});
cancelRegisterButton.addEventListener("click", () => {
  registerForm.classList.add("hidden");
  loginForm.classList.remove("hidden");
  showRegisterButton.classList.remove("hidden");
  showAuthMessage("");
});
loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const response = await fetch("/auth/login", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({username: document.getElementById("loginUsername").value, password: document.getElementById("loginPassword").value}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "登录失败");
    setAuthenticated(data.user);
    loginForm.reset();
  } catch (error) {
    showAuthMessage(error.message || "登录失败");
  }
});
registerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const response = await fetch("/auth/register", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        username: document.getElementById("registerUsername").value,
        display_name: document.getElementById("registerName").value,
        department: document.getElementById("registerDepartment").value,
        password: document.getElementById("registerPassword").value,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "注册失败");
    registerForm.reset();
    registerForm.classList.add("hidden");
    loginForm.classList.remove("hidden");
    showRegisterButton.classList.remove("hidden");
    showAuthMessage("注册成功，请直接登录。管理员可在后台调整你的权限。", true);
  } catch (error) {
    showAuthMessage(error.message || "注册失败");
  }
});
logoutButton.addEventListener("click", async () => {
  await fetch("/auth/logout", {method: "POST"});
  setLoggedOut();
});
adminButton.addEventListener("click", async () => {
  try {
    adminPanel.classList.remove("hidden");
    await loadAdminUsers();
  } catch (error) {
    adminPanel.classList.add("hidden");
    alert(error.message || "管理员面板打开失败");
  }
});
closeAdmin.addEventListener("click", () => adminPanel.classList.add("hidden"));
adminUsers.addEventListener("click", async (event) => {
  if (!event.target.classList.contains("save-permissions")) return;
  try {
    await saveUserPermissions(event.target);
  } catch (error) {
    alert(error.message || "权限保存失败");
  }
});
initAuth();
localStorage.setItem("supportflow_thread_id", threadId);
