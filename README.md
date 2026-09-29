# 法律证据保管与流转后台

仅使用 Python 3.11+ 标准库实现的证据保管项目。支持真实 SHA-256 入册、封存/开箱/移交、分析衍生关系、案件成员权限、法律保留、保留期限、**保留期限复核（延期 / 提前结束的提交-审批工作流）**、不可变保管事件链和 JSON 报告导出。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8105>，默认数据库 `custody.db`。测试：

```bash
python3 -m unittest -v
```

演示身份：`custodian1`、`custodian2`、`analyst1`、`auditor1`、`outsider`。请求使用 `X-User-Id`。

## 代码分层（分开维护）

- `retention_rules.py`：后端期限规则（纯函数）。校验延期/提前结束的新日期、法律保留中不得缩短、延期照常。
- `retention_reviews.py`：复核服务。保管员提交、案件创建人或审计员处理、批准前重新核对状态、批准后更新到期日并追加事件。
- `schema_migrations.py`：旧库升级与事件迁移（`PRAGMA user_version` 版本化，幂等）。
- `app.py`：存储层与 HTTP 接口；`web/index.html` + `web/app.js`：页面交互。
- `errors.py`：共享的 `BusinessError` 与时钟。

## 保留期限复核规则

- 保管员提交延期（`extend`）或提前结束（`early_end`），写明原因和新日期；只有案件创建人或审计员能处理，**提交人不能自批**。
- 批准前按当前最新状态重新核对：提交后法律保留、释放状态、提交人角色或当前到期日一旦变化，旧申请不能盖掉新状态（`state_changed_since_submit`），只能拒绝后重新提交。
- 法律保留中不得缩短（提前结束在提交时即被拒绝）；延期在法律保留中照常。
- 每条证据同一时间只允许一条待处理申请；拒绝必须填写原因。
- 批准后更新 `retention_until` 并追加 `RETENTION_CHANGED` 事件；`original_retention_until` 保留入册原日期。

## 主要接口

- `POST /api/cases`：创建案件，创建人自动成为保管员。
- `POST /api/cases/{id}/members`：授予 custodian、analyst 或 auditor 角色。
- `POST /api/cases/{id}/evidence`：以 Base64 入册证据，服务端计算 SHA-256 和大小。
- `GET /api/evidence/{id}`：查看元数据、完整保管事件链、完整性结果、衍生关系和全部期限复核记录。
- `POST /api/evidence/{id}/open`：保管员开箱。
- `POST /api/evidence/{id}/transfer`：移交保管人并记录位置。
- `POST /api/evidence/{id}/derive`：分析员从已开箱证据创建衍生证据。
- `POST /api/evidence/{id}/hold`：审计员或案件创建人设置/解除法律保留。
- `POST /api/evidence/{id}/release`：存在法律保留时拒绝释放。
- `POST /api/evidence/{id}/retention-reviews`：保管员提交延期/提前结束申请。
- `GET /api/retention-reviews/{id}`：查看单条申请；`GET /api/cases/{id}/retention-reviews?status=pending`：列出案件申请。
- `POST /api/retention-reviews/{id}/decision`：创建人或审计员批准/拒绝（拒绝须带 `decision_note`）。
- `GET /api/cases/{id}/report`：校验所有证据哈希和每条事件链，导出完整报告（含原日期、每次申请与处理结果）。
- 所有 `DELETE` 请求返回 405；证据和保管记录不提供删除接口。

## 旧库升级

老库的 `custody_events` CHECK 不含 `RETENTION_CHANGED`，升级时重建事件表并**原样保留每条事件的 sequence、previous_hash、event_hash**，旧证据编号和事件链不被割开、继续可查；`evidence` 回填 `original_retention_until`，新建 `retention_reviews` 表。升级幂等，可重复执行。

保管事件通过前一条事件哈希串联；报告会重新计算文件哈希和事件链。项目适合流程与完整性原型，不涵盖现实中的签名证书、WORM 存储、证据文件加密或司法辖区合规认证。
