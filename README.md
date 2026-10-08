# 永好塑胶订单运营助手

一个面向塑胶制品制造场景的内部订单、生产、质检与库存协同系统。

项目以永好塑胶制品（深圳）有限公司的内部业务场景为背景，围绕塑胶载体卷轴等产品，模拟业务员、生产员工、质检员工、仓库员工和管理员之间的日常协作。

> 这是学习、作品集和流程演示项目。仓库中的客户、订单、库存和生产数据均为虚构演示数据，不包含公司真实业务数据。

## 项目解决什么问题

小型制造企业中，客户订单、生产进度、质检结果和库存信息容易分散在 Excel、聊天记录和纸质台账中。这个系统把这些信息集中到一个内部工作台中：

- 业务员提交客户订单；
- 管理员审批订单并查看整体进度；
- 生产员工申报完成数量；
- 质检员工提交合格/不合格数量；
- 仓库员工提交出库申请；
- 管理员统一审批，并在审批通过后真正修改业务数据；
- 员工通过个人看板查看自己的工作量和申请状态；
- 管理员通过总看板查看订单、生产、质检、库存和待审批事项。

## 系统预览

### 整体架构

![SupportFlow 系统架构](docs/images/architecture.svg)

### 员工申请与管理员审批

![申请审批流程](docs/images/approval-flow.svg)

> GitHub 会直接渲染仓库内的 SVG 图片。后续可以在 `docs/images/` 中补充实际工作台截图。

## 核心功能

### 1. 账号与权限

- 员工注册和登录；
- 新员工注册后默认获得基础业务权限，可以正常进入系统；
- 管理员可以查看员工账号并增删权限；
- 后端接口再次校验权限，前端隐藏按钮不是安全边界；
- 员工只能提交申请，不能直接审批或修改核心业务数据。

| 账号类型 | 主要工作 |
| --- | --- |
| 业务员 | 查询订单和物流，提交客户订单 |
| 生产员工 | 查询订单和生产进度，提交生产报工 |
| 质检员工 | 查询订单和生产进度，提交质检结果 |
| 仓库员工 | 查询订单、库存和物流，提交出库申请 |
| 管理员 | 查看全局数据、管理权限、审批申请、直接创建订单 |

### 2. 订单管理

- 员工提交客户订单申请；
- 管理员审批或驳回员工提交的订单；
- 管理员可以直接创建已批准订单；
- 订单包含客户、产品 SKU、规格、需求数量、单价和交期；
- 订单可以关联生产任务、质检记录、库存和出库记录；
- 支持通过聊天入口查询订单需求量、完成量和剩余量。

### 3. 生产报工

- 员工提交订单的生产完成数量；
- 申请先进入待审批状态；
- 管理员批准后才写入生产记录；
- 报工数量不能为零、负数或非法数字；
- 累计完成量不能超过订单需求数量；
- 员工可以查看自己的报工记录和审批状态。

### 4. 质检管理

- 提交检验数量、合格数量和不合格数量；
- 后端校验：

```text
合格数量 + 不合格数量 = 检验数量
```

- 员工无质检权限时，不能通过聊天或接口绕过权限；
- 质检结果经过管理员审批后进入正式记录；
- 管理员看板可以汇总检验量、合格量和不良量。

### 5. 库存与出库

- 查询产品、仓库、批次和可用库存；
- 仓库员工提交出库申请；
- 出库申请审批前库存不变；
- 管理员批准时重新读取当前库存；
- 出库数量不能超过订单剩余数量；
- 库存不足时拒绝出库，不产生部分扣减；
- 审批成功后写入库存流水和订单发货数量。

### 6. 员工和管理员看板

员工看板包括：

- 我的待审批申请；
- 今日/累计生产报工量；
- 今日/累计质检量；
- 最近订单申请、报工、质检和出库状态；
- 驳回原因和可重新提交的信息。

管理员看板包括：

- 订单数量和订单状态汇总；
- 总需求量、完成量、发货量；
- 库存总量和可用库存；
- 合格量、不良量和质检统计；
- 员工工作量；
- 待审批申请列表；
- 批准、驳回和审批历史；
- 员工账号及权限管理。

## 业务状态示例

```text
订单申请：SUBMITTED → APPROVED / REJECTED
生产报工：SUBMITTED → APPROVED / REJECTED / FAILED
质检申请：SUBMITTED → APPROVED / REJECTED / FAILED
出库申请：SUBMITTED → APPROVED / REJECTED / FAILED
```

所有写入型操作遵循：

```text
员工提交
  → 后端确定性校验
  → 等待管理员审批
  → 审批时重新读取订单/库存/权限
  → 校验通过后才修改数据库
```

## LLM 在系统中的作用

LLM 是自然语言入口，不是管理员，也不是数据库。

### LLM 可以做什么

- 理解“SF2002 现在完成多少了”对应订单/生产查询；
- 理解“给客户 CUST-001 下 500 个黑色卷轴”对应订单申请；
- 发现缺少订单号或数量时，用自然语言追问；
- 把后端已经查到的订单、库存和生产结果整理成易读回答。

### LLM 不能做什么

- 不能给员工授予权限；
- 不能批准订单、报工、质检或出库；
- 不能直接修改库存、订单状态或完成数量；
- 不能把用户声称的库存、订单总量当成可信事实；
- 不能绕过数量校验、审批和幂等控制。

查询问题优先使用后端确定性查询；写入问题必须进入申请、审批和后端验证流程。

## 技术架构

```text
浏览器
  ↓
TanStack Start + Cloudflare Worker 前端（可选）
  ↓ /api 代理
FastAPI 后端
  ├── 登录、会话与权限
  ├── LangGraph 多轮对话与检查点
  ├── 确定性业务工具
  ├── SQLite 演示数据库
  ├── BM25 / Dense / RRF / BGE 本地检索链
  └── 可选 DeepSeek Provider
```

主要技术：

- Python 3.11+
- FastAPI
- LangGraph
- Pydantic
- SQLite
- BM25、Dense Embedding、RRF、BGE Reranker
- React + TanStack Start
- Cloudflare Workers
- Docker CPU-only

## 本地运行

### 环境要求

- Python 3.11 或更高版本；
- Node.js 20 或更高版本（仅构建 Worker 前端需要）；
- Docker Desktop（可选）；
- 本地 Dense 模型缓存和 BGE Reranker 模型（启用真实检索时需要）。

### 启动后端

```powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.api:api --host 127.0.0.1 --port 8001
```

打开：

```text
http://127.0.0.1:8001/
```

健康检查：

```text
GET /health       检查进程是否存活
GET /readiness    检查本地模型和启动资源是否准备完成
```

### 生成演示账号

密码只从本地 `.env` 读取，不写入代码、不打印到日志：

```powershell
python scripts/seed_demo_accounts.py
```

示例账号密码请自行配置在未提交的 `.env` 中，不要写入 README 或 GitHub。

## 本地模型配置

```text
SUPPORTFLOW_ENABLE_DENSE_RETRIEVAL=true
SUPPORTFLOW_EMBEDDING_MODEL=paraphrase-multilingual-MiniLM-L12-v2
SUPPORTFLOW_EMBEDDING_DEVICE=cpu
SUPPORTFLOW_RERANKER_PATH=D:\models\bge-reranker-v2-m3
SUPPORTFLOW_RERANKER_DEVICE=cpu
```

模型必须通过本地缓存或本地目录加载。运行时不应偷偷访问 Hugging Face 下载模型。

## Cloudflare Worker 部署

`frontend/` 是独立的 TanStack Start 前端项目。Cloudflare Worker 只负责网页和 `/api/*` 代理，FastAPI 后端仍负责数据库、本地模型、权限和 DeepSeek 调用。

```powershell
cd frontend
npm install
npm run build
npx.cmd wrangler login
npx.cmd wrangler deploy --var "BACKEND_ORIGIN:https://你的后端公网地址"
```

注意：

- `BACKEND_ORIGIN` 不能填写 `http://127.0.0.1:8001`；
- 临时演示可以使用 Quick Tunnel，但本机后端和隧道必须一直运行；
- 稳定内部使用需要一台长期在线的服务器或办公电脑；
- DeepSeek API Key 只放在后端运行环境，不进入 Worker 前端和 GitHub。

## Docker 部署

模型不复制进镜像，而是使用只读 Bind Mount：

```powershell
docker build -t supportflow-agent:v1.4 .
docker run --rm -p 8001:8001 `
  --mount "type=bind,source=D:\models\bge-reranker-v2-m3,target=/models/bge-reranker-v2-m3,readonly" `
  --env-file .env `
  -e SUPPORTFLOW_ENABLE_DENSE_RETRIEVAL=true `
  -e SUPPORTFLOW_RERANKER_PATH=/models/bge-reranker-v2-m3 `
  -e HF_HUB_OFFLINE=1 `
  -e TRANSFORMERS_OFFLINE=1 `
  supportflow-agent:v1.4
```

## 测试

运行自动化测试：

```powershell
pytest -q
```

自动化测试使用 Fake/Mock Provider，不调用真实 DeepSeek。当前版本完整测试结果为：

```text
89 passed
```

真实 DeepSeek Smoke Test 是单独的显式操作：

```powershell
python scripts/smoke_test_deepseek.py
```

这个命令可能产生真实 API 调用，只有在明确需要验证 Provider 时才运行。

## 数据与安全边界

- `data/supportflow_demo.db` 是本地生成的 SQLite 演示数据库；
- `.env` 被 `.gitignore` 忽略；
- `.env.example` 不包含真实 Secret；
- 数据库文件、日志、模型目录和前端构建产物不提交；
- 密码以哈希形式保存，不在 README、日志或 Trace 中显示；
- 订单、库存、权限和审批状态以后端数据库为准；
- 所有写入操作都经过后端权限、参数、状态和幂等校验；
- 拒绝非法请求时不返回 API Key、环境变量或堆栈信息。

## 项目目录

```text
app/                 FastAPI、LangGraph 和业务逻辑
data/                虚构业务数据、知识库和评估结果
docs/                业务目标、架构图和可靠性说明
frontend/            TanStack Start + Cloudflare Worker 前端
scripts/             演示账号、Smoke Test 和评估脚本
tests/               Fake Provider 自动化测试
Dockerfile           CPU-only 后端镜像
.env.example         无 Secret 的配置模板
```

## 当前限制

- 当前 SQLite 和内存检查点适合本地/小规模演示，不适合高并发生产环境；
- 真实公司数据需要经过字段确认、权限审查和脱敏/授权流程后才能接入；
- 当前管理员审批是单级审批；
- Cloudflare Worker 依赖单独运行且可公网访问的 FastAPI 后端；
- 当前是阶段事件流，不是逐 Token 的生成流；
- LLM 只提供自然语言理解和回答组织能力，系统不宣称零幻觉或百分之百自动化。

## 项目状态

第一版制造运营核心流程已经完成：账号权限、订单申请、管理员审批、生产报工、质检、库存、出库、员工看板、管理员看板、LLM 查询入口和安全校验均已接入，并通过自动化回归测试。

后续真正上线前，优先确认真实业务字段、导入经授权的数据、选择持久化数据库，并将 FastAPI 后端部署到长期在线且受控的机器上。
