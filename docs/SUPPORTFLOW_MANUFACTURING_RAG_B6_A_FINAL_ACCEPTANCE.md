# SupportFlow Manufacturing RAG B6-A 最终验收记录

> 本文记录 B6-A 本地验收，不代表生产环境认证，也不包含真实企业数据或真实客户指标。

## 验收基线

- 仓库：`D:\opencode_project\supportflow-agent`
- 分支：`main`
- B6-A 基线：`c880e2e54d400ca71a2965991cda9762fa0da61f`
- 本次不执行 Git Push，不修改数据库 Schema、检索算法或审批语义。
- B0 架构审计和 B1-B6 计划继续作为 `docs/` 下的历史规划文档保留；当前代码和本记录优先。

## 场景验收

### A：制造 SOP 问答

- 制造稳定知识问题由 `manufacturing_knowledge` 路由到 `manufacturing_demo`。
- 客服 Corpus 与制造 Corpus 通过显式 Corpus 和独立资源缓存隔离。
- 文档角色和 `effective_status` 在检索前由确定性逻辑过滤。
- 制造知识问答复用 Hybrid 检索、Evidence Judge、Grounded Generation 和 Safe Fallback。
- 证据保留 `document_id`、`source_version`、`source_section` 等来源字段，并标记 `synthetic=true`。
- B4.1 的真实本地模型结果属于历史烟雾证据；B6-A 没有重新消耗模型或付费 LLM。

### B：质检混合查询

- `quality_mixed` 路由只读查询 SQLite 质检记录，并独立检索制造质检 SOP。
- Trace 分开记录 `database_source_ids` 和 `sop_source_ids`。
- SOP 只能提供通用处理规范，不能证明具体订单的失败原因。
- 当前 `quality_reports` 没有独立缺陷原因字段；缺少原因时返回未知，不进行推断。
- 混合路径不会创建申请、审批或其他写操作。

### C：出库申请与审批

- 明确的自然语言出库申请进入 `outbound_submit`；缺少订单号或数量时返回补充信息。
- 合法员工申请创建 `SUBMITTED` 的 `operation_requests`，审批前不扣库存。
- 提交阶段检查订单剩余数量和当前库存；管理员批准时重新检查订单、库存和申请条件。
- 驳回、无权限、重复待审批申请和审批前条件变化均不会造成未经批准的出库。
- 管理员审批权限在后端再次验证，非管理员不能通过聊天或接口审批。
- 当前边界：审批校验覆盖现有订单剩余数量、库存和权限规则，没有扩展为完整 ERP/MES/WMS 状态模型。

## 测试结果

- A/B/C 相关回归测试：`22 passed in 3.12s`。
- 完整测试：`137 passed in 38.52s`。
- 测试使用 Fake/Mock Provider；真实 DeepSeek 调用：`0`。
- 本轮未下载模型、未执行 OCR、未执行付费 LLM。

## 历史真实 Hybrid RAG 证据

B4.1 曾在本地离线环境执行 5 个固定 Gold 查询，真实走过：

```text
BM25 → Dense Embedding → RRF → BGE Reranker
```

- Dense：`paraphrase-multilingual-MiniLM-L12-v2`，本地缓存、CPU、离线加载。
- BGE：`BAAI/bge-reranker-v2-m3`，本地目录、CPU、离线加载。
- 5/5 查询的 BGE Top-1 命中人工预先定义的 Gold 文档。
- 这只是 5 个 Demo 查询的本地 Smoke Test，不是生产准确率、召回率或客户指标。
- BGE CPU 重排历史单查询约 `1.28–2.10s`，首次加载另有冷启动成本。

制造 Corpus 当前只有 5 份 synthetic 文档，采用文档级检索，`chunk_id=None`。文档标记为 `authority=teaching_demo_only`，不代表正式企业制度或行业标准。

## 安全与交付检查

- `.env`、SQLite、日志、模型和缓存不在 Git 跟踪文件中。
- `.env.example` 只包含空 Secret 配置项。
- Dockerfile 不包含 Secret；`.dockerignore` 排除 `.env`、日志、SQLite、`.local` 和模型文件。
- README 已同步当前双 Corpus、A/B/C、限制和历史证据。
- `InMemorySaver` 仅提供进程内会话检查点，重启后不持久化。
- 当前 SQLite 和 Demo Corpus 只定位为学习/作品集/小规模演示，不宣称生产级部署。
