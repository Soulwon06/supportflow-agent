# SupportFlow Agent — Tool Reliability Design

## 1. Tool Architecture

SupportFlow Agent 当前包含三个核心 Tool。

### query_order

- 功能：查询订单信息
- 类型：Read Operation（读操作）
- 支持参数校验
- 通过 Dispatcher 统一调用

### query_logistics

- 功能：查询物流信息
- 类型：Read Operation（读操作）
- 支持参数校验
- 支持 Timeout
- 支持 Bounded Retry
- 支持 Exponential Backoff
- 支持 Circuit Breaker

### refund_order

- 功能：执行退款
- 类型：Write Operation（写操作）
- 属于 Sensitive Action（敏感操作）
- 需要 Human Confirmation
- 需要 Authorization
- 使用 Idempotency Key 防止重复退款

---

## 2. Unified Tool Result

所有 Tool 使用统一的 `ToolResult` 返回结构。

字段包括：

- `success`：Tool 是否执行成功
- `data`：成功时返回的数据
- `error_code`：失败类型
- `error_message`：错误信息
- `retryable`：该类错误是否具有可重试性质

统一返回结构的目的：

1. Node 不需要分别理解不同 Tool 的异常格式。
2. 可以统一进行 Error Classification。
3. 可以根据 `retryable` 判断错误性质。
4. 方便 Logging、Evaluation 和 Failure Analysis。

---

## 3. Validation

Tool 执行前需要检查参数是否合法。

例如：

- `order_id` 必须是非空字符串。
- `amount` 必须是大于 0 的数字。
- `reason` 不能为空。
- `idempotency_key` 不能为空。

非法参数返回：

`INVALID_ARGUMENT`

核心原则：

**Fail Fast**

也就是错误数据应该尽早被拒绝，而不是继续进入后续业务执行。

---

## 4. Retry

物流查询属于 Read Operation。

如果发生临时 Timeout，可以执行 Bounded Retry（有限重试）。

当前策略：

- 最大尝试次数：3
- Attempt 1 Timeout 后等待 0.5 秒
- Attempt 2 Timeout 后等待 1.0 秒
- Attempt 3 返回最终结果

当前 Backoff 计算方式：

`0.5 × 2^(attempt - 1)`

Retry 解决的问题是：

> 当前这一次请求失败后，要不要再次尝试？

Retry 只负责一次 Tool 调用内部的重试过程。

---

## 5. Refund Retry Policy

退款属于 Write Operation。

退款不能因为 Timeout 就无条件自动重试。

例如：

退款请求已经到达服务端，服务端实际完成退款，但是响应在返回途中丢失。

此时 Client 可能认为退款失败。

如果 Client 直接再次执行退款，就可能造成重复退款。

因此退款操作需要使用：

`Idempotency Key`

同一个 `idempotency_key` 被重复提交时，系统不会再次执行退款，而是返回之前已经产生的结果。

---

## 6. Tool Whitelist

Agent 只能调用明确允许的 Tool。

当前 Whitelist：

- `query_order`
- `query_logistics`
- `refund_order`

例如：

`delete_database`

不在 Whitelist 中，因此 Dispatcher 返回：

`TOOL_NOT_ALLOWED`

Whitelist 解决的问题是：

> 这个 Tool 是否允许进入 Agent 的可用能力范围？

---

## 7. Authorization

当前角色权限设计如下。

### customer_service

允许：

- `query_order`
- `query_logistics`

不允许：

- `refund_order`

### manager

允许：

- `query_order`
- `query_logistics`
- `refund_order`

对于未知角色，系统使用空权限集合：

`set()`

也就是说，未知角色默认没有任何 Tool 权限。

这种设计属于：

**Fail Closed**

含义是：

> 当身份或权限不明确时默认拒绝，而不是默认放行。

没有权限时返回：

`PERMISSION_DENIED`

---

## 8. Human Confirmation

退款属于 Sensitive Action，因此执行退款之前必须获得用户确认。

当前 Workflow：

Refund Prepare  
→ Human Confirmation  
→ Interrupt  
→ Checkpoint  
→ User Confirmation  
→ Resume  
→ Refund Execute

Human Confirmation 不能替代 Authorization。

例如：

`customer_service + 用户确认退款`

最终仍然返回：

`PERMISSION_DENIED`

因此退款真正执行至少需要同时满足：

1. 用户已经确认退款。
2. 当前角色拥有 `refund_order` 权限。

---

## 9. Circuit Breaker

物流服务使用 Circuit Breaker（熔断器）。

当前参数：

- `failure_threshold = 2`
- `recovery_timeout = 5 seconds`

Circuit Breaker 有三个主要状态。

### CLOSED

正常状态。

请求允许访问物流服务。

### OPEN

熔断状态。

新的物流请求不会真正访问物流 Tool，而是直接 Fast Fail。

当前返回：

`SERVICE_UNAVAILABLE`

### HALF_OPEN

恢复试探状态。

等待 `recovery_timeout` 后，系统允许请求再次访问物流服务，用于判断服务是否恢复。

如果试探成功：

`HALF_OPEN → CLOSED`

如果试探失败：

应重新进入 `OPEN`。

---

## 10. Retry vs Circuit Breaker

Retry 和 Circuit Breaker 解决的是两个不同层级的问题。

### Retry

关注：

> 一次请求内部是否应该再次尝试？

例如一次 `query_logistics()`：

Attempt 1 → Timeout  
Attempt 2 → Timeout  
Attempt 3 → Timeout

这是一次 Tool 调用内部的 Retry。

### Circuit Breaker

关注：

> 一个外部服务持续不健康时，后续新的请求是否还应该继续访问它？

即使一次 `query_logistics()` 内部 Retry 了 3 次并全部失败，对于 Circuit Breaker 来说，也只算：

`failure_count += 1`

因为这是一次完整 Tool 调用最终失败。

---

## 11. Circuit Breaker Verification

实际测试结果如下。

### Initial

- State：`CLOSED`
- Failure Count：`0`

### First Failed Tool Call

物流 Tool 内部三次 Timeout。

最终：

- Error：`TIMEOUT`
- State：`CLOSED`
- Failure Count：`1`

### Second Failed Tool Call

物流 Tool 再次经过三次 Timeout 后失败。

最终：

- Error：`TIMEOUT`
- State：`OPEN`
- Failure Count：`2`

### Request While OPEN

第三次请求没有进入 `query_logistics()`。

系统直接返回：

`SERVICE_UNAVAILABLE`

这证明 Fast Fail 生效。

### Recovery

等待 5.5 秒以后，Circuit Breaker 进入恢复窗口。

系统允许一次试探请求。

试探请求最终成功后：

- State：`CLOSED`
- Failure Count：`0`

说明 Circuit Breaker 成功恢复。

---

## 12. Checkpoint / Interrupt / Resume

SupportFlow Agent 使用 LangGraph Checkpointer 保存 Graph State。

当前使用：

`InMemorySaver`

并通过：

`thread_id`

区分不同的状态链。

几个概念的关系如下。

### State

当前 Workflow 正在使用的数据。

例如：

- `user_message`
- `intent`
- `role`
- `order_id`
- `confirmed`
- `execution_log`

### Checkpoint

State 在某个执行时刻保存下来的快照。

### Checkpointer

负责保存和读取 Checkpoint 的机制。

当前使用的是 LangGraph 的 `InMemorySaver`。

### thread_id

用于标识需要读取或写入哪一条状态链。

### Interrupt

主动暂停当前 Graph，例如等待用户确认退款。

### Resume

外部信息到达以后，从之前保存的状态继续执行。

当前 `InMemorySaver` 只适合学习和测试。

因为它将 Checkpoint 保存在当前 Python 进程内存中。

Python 进程退出以后，这些数据会消失。

---

## 13. Conversational State

已经完成两轮对话测试。

### 第一轮

用户：

> 帮我查一下订单9527

执行后 State 中：

`order_id = "9527"`

Execution Log：

`router_node → order_node`

### 第二轮

用户：

> 刚才那个订单物流到哪里了？

第二轮没有显式提供订单号。

系统继续使用相同的 `thread_id`。

LangGraph Checkpointer 恢复上一轮 State，因此仍然存在：

`order_id = "9527"`

`logistics_node` 当前轮无法通过正则找到新的订单号，因此回退读取历史 State 中的 `order_id`。

最终成功查询订单 9527 的物流。

第二轮 Execution Log 在第一轮基础上继续增加：

`router_node → order_node → router_node → logistics_node`

这说明 Checkpoint 保存和恢复的是 Graph State，而不仅仅是单独保存 `order_id`。

当前实现更准确属于：

**Checkpoint-backed conversational state**

也就是：

**基于 Checkpoint 的会话状态延续。**

它还不是完整意义上的 Long-term Memory。

---

## 14. State / Checkpoint / Memory Boundary

### State

回答：

> 当前 Workflow 正在处理什么？

### Checkpoint

回答：

> 这条 Workflow 上次执行到哪里，当时的 State 是什么？

### Long-term Memory

回答：

> 有哪些信息需要在未来的交互中长期保存、检索和复用？

例如用户一个月以后创建新的 `thread_id`，系统仍然能够知道该用户过去关注过订单 9527，这更接近 Long-term Memory。

当前 V0.3 主要实现：

- State
- Checkpoint
- Interrupt
- Resume
- 同一个 `thread_id` 下的跨轮状态延续

没有实现完整的 Long-term Memory Store。

---

## 15. Tool Failure Case

### Failure Scenario

用户请求：

> 查询订单9527的物流

执行链：

Router  
→ `logistics`

Dispatcher：

- Whitelist PASS
- Authorization PASS

随后：

`query_logistics`

内部执行：

Attempt 1 → TIMEOUT  
Attempt 2 → TIMEOUT  
Attempt 3 → TIMEOUT

最终 ToolResult：

- `success = False`
- `error_code = TIMEOUT`
- `retryable = True`

### Root Cause

Root Cause：

**Logistics Tool / External Service Timeout**

原因：

- Router 正确
- Whitelist 正确
- Authorization 正确
- Tool Arguments 正确
- 最早出现异常的位置是物流服务 Timeout

因此不能把该问题归因为 Router Error、Permission Error 或 Generation Error。

### Recovery Strategy

当前恢复策略：

Timeout  
→ Bounded Retry  
→ Exponential Backoff  
→ Final Failure  
→ Circuit Breaker 记录失败

连续两次完整物流 Tool 调用最终失败后：

`Circuit = OPEN`

后续请求：

`Fast Fail → SERVICE_UNAVAILABLE`

等待恢复窗口以后：

`OPEN → HALF_OPEN → Probe Request`

试探成功：

`HALF_OPEN → CLOSED`

---

## 16. Refund Security Layers

当前退款链路：

Refund Request  
→ Parameter Extraction / Validation  
→ Human Confirmation  
→ Interrupt / Checkpoint  
→ Resume  
→ Dispatcher  
→ Tool Whitelist  
→ Authorization  
→ Refund Tool  
→ Idempotency Check  
→ Execute

这些安全层解决的问题不同。

### Validation

参数是否合法？

### Human Confirmation

用户是否同意执行敏感操作？

### Tool Whitelist

Agent 是否被允许使用这个 Tool？

### Authorization

当前角色是否拥有执行这个 Tool 的权限？

### Idempotency

重复请求是否会造成重复写操作？

这种多层安全设计属于：

**Defense in Depth（纵深防御）**

---

## 17. Current V0.3 Reliability Status

目前已经验证：

- Unified Tool Result：完成
- Parameter Validation：完成
- Error Classification：完成
- Timeout：完成
- Bounded Retry：完成
- Exponential Backoff：完成
- Refund Idempotency：完成
- Human Confirmation：完成
- Interrupt / Resume：完成
- Checkpoint：完成
- Tool Whitelist：完成
- Authorization：完成
- Fail Closed：完成
- Circuit Breaker：完成
- Fast Fail：完成
- Circuit Recovery：完成
- Conversational State：完成
- Tool Failure Analysis：完成

尚未把当前 `InMemorySaver` 升级为生产级持久化 Checkpointer，也尚未实现完整 Long-term Memory。