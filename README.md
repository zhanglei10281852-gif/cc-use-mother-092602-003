# 科学计算任务运营服务

这是一个面向科研平台、实验室和计算中心的 Python 后端，使用 FastAPI 与 SQLite 管理参数模板、计算任务提交、优先级排队、工作者领取、取消、失败重试、租约恢复、用户配额、结果版本和管理员人工干预记录。服务同时保留用户、角色、会话和审计等基础能力，所有运行数据都在单个本地数据库文件中，不需要另行部署数据库、缓存、消息队列或浏览器界面。

## 已有能力

- 参数模板：保存参数类型、必填项、数值范围、默认值、最大运行时间和最大尝试次数。
- 任务提交：根据模板校验参数，使用用户与幂等键避免重复创建，并保存项目、提交人和输入摘要。
- 排队领取：按优先级和进入队列的顺序分配任务，工作者可声明算法能力并获得有期限的租约。
- 执行回执：工作者可以续租、提交结果或报告失败；可重试错误使用确定的退避时间重新排队。
- 失败恢复：租约过期后可由恢复入口将任务重新排队，达到最大尝试次数的任务转为失败。
- 配额控制：可保存用户、角色或项目的排队数、运行数和每日提交上限；当前提交路径执行用户配额。
- 结果版本：每次成功回执保存不可变结果、指标摘要和内容摘要，任务指向当前结果版本。
- 人工干预：取消、人工重试、优先级调整和批量操作均保留操作者、原因、前后状态和批次标识。
- 登录与角色：基础管理模块提供管理员初始化、用户、角色、会话和细粒度权限。

## 卫星功率预算（/api/power）

面向发射前评估 AI 推理作业能否放入卫星功率预算（默认 1 千瓦）的场景：

- 载荷登记：记录载荷编码、类型、额定与最大功耗，支持停用与版本化更新。
- 日照段登记：保存每段日照的起止时间与可输出功率，重叠的日照段会被明确拒绝（409）。
- 功率预算：保存总功率、安全预留和阴影区能量余量；每次调整生成新的预算版本，历史版本保留。
- 临时降额：按时间窗口扣减有效功率上限，可停用；降额会改变同时段可执行的作业组合。
- 作业计划：每个作业引用载荷并携带分段常值功耗曲线（相对偏移秒 + 瓦数），计划内容按版本保存，原始申请永不被修改。
- 计划生命周期：草案（draft）→ 提交（submitted）→ 审批（approved）→ 发布（published），撤回按状态回到草案或终止；同一时间只允许一个发布中的计划。
- 评估计算：公式版本 `power-budget-v1` 扫描瞬时峰值（预算 − 预留 − 降额），按日照段核算能量、单独核算阴影区能量，并校验载荷上限；拒绝时返回带上下文的原因列表（峰值超限、段能量超限、阴影区超限、载荷缺失/停用/超限）。
- 重新评估：预算调整或降额变化后可对计划重新评估，`adjust` 接口支持 `reevaluate` 自动重估所有已发布计划；每次评估保存完整输入快照、快照摘要和公式版本，管理员可随时查询。
- 版本比较：比较两个计划版本，返回新增、移除、修改的作业及字段级差异，并附带各版本最近一次评估结论。
- 并发冲突：计划修改、状态流转、预算调整和载荷更新均要求 `expected_version`，不一致时返回 409 与当前版本号，不会静默覆盖；计划创建使用幂等键，同键同内容重放返回原计划，同键不同内容返回 409。

常用接口：

```text
POST   /api/power/payloads                     登记载荷
POST   /api/power/sunlight-segments            登记日照段（重叠返回 409）
POST   /api/power/budgets                      创建功率预算（版本 1）
POST   /api/power/budgets/{id}/adjust          调整预算（新版本，可选 reevaluate）
POST   /api/power/budgets/{id}/deratings       登记临时降额
POST   /api/power/plans                        创建计划草案（幂等键）
PUT    /api/power/plans/{id}/versions          修改草案（新版本，expected_version）
POST   /api/power/plans/{id}/submit|approve|publish|withdraw
GET    /api/power/plans/{id}/compare           比较两个计划版本
POST   /api/power/plans/{id}/evaluate          对计划版本执行评估
GET    /api/power/evaluations/{id}             查询评估的输入快照与公式版本
```

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/township.db`。可以复制 `.env.example` 并通过 `TOWNSHIP_DATABASE_PATH` 指定其他本地路径。

## 数据库初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动 API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

计算任务摘要位于 `/api/compute/summary`，模板、配额、提交、领取、回执和人工操作接口统一使用 `/api/compute` 前缀。

## 测试

```bash
python -m pytest
```

测试覆盖参数规则、幂等提交、配额拒绝、优先级领取、能力匹配、租约续期、失败退避、结果版本、取消、人工重试、批量操作和租约恢复，并覆盖功率预算的载荷与日照段登记、计划版本与乐观锁、峰值与能量评估、拒绝原因、降额与预算调整重估、发布冲突、版本比较和评估快照，同时保留身份与既有科学计算模块的回归用例。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
python -m app.cli compute-demo
python -m app.cli power-demo
```

`smoke` 在进程内检查根路径和健康接口，`compute-demo` 会创建示例参数模板、提交一个计算任务并让匹配能力的工作者领取，用于快速确认核心运营链路；`power-demo` 登记示例载荷、日照段与 1kW 预算，提交一份推理作业计划并执行评估，可重复运行验证幂等链路。

## 目录结构

```text
app/
  compute/         计算模板、配额、任务、结果版本和人工干预
  power/           卫星载荷、日照段、功率预算、作业计划与评估
  api/             用户、角色、认证、审计和系统管理接口
  core/            时钟、安全、异常和分页能力
  repositories/    通用 SQLite 查询
  seismic/         既有地震计算示例领域
  services/        身份、审计和通用后台任务服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、计算运营、功率预算和身份回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL、busy timeout 和同步写入策略。提交、领取、回执和人工干预使用即时事务；任务领取通过条件更新避免同一条排队记录被重复领取。服务保存 UTC 时间字符串，测试可以注入固定时钟验证退避、租约到期和跨日配额。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
