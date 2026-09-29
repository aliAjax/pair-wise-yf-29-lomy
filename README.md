# 法律证据保管与流转后台

仅使用 Python 3.11+ 标准库实现的证据保管项目。支持真实 SHA-256 入册、封存/开箱/移交、分析衍生关系、案件成员权限、法律保留、保留期限、不可变保管事件链和 JSON 报告导出。

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

## 主要接口

- `POST /api/cases`：创建案件，创建人自动成为保管员。
- `POST /api/cases/{id}/members`：授予 custodian、analyst 或 auditor 角色。
- `POST /api/cases/{id}/evidence`：以 Base64 入册证据，服务端计算 SHA-256 和大小。
- `GET /api/evidence/{id}`：查看元数据、完整保管事件链、完整性结果和衍生关系。
- `POST /api/evidence/{id}/open`：保管员开箱。
- `POST /api/evidence/{id}/transfer`：移交保管人并记录位置。
- `POST /api/evidence/{id}/derive`：分析员从已开箱证据创建衍生证据。
- `POST /api/evidence/{id}/hold`：审计员或案件创建人设置/解除法律保留。
- `POST /api/evidence/{id}/release`：存在法律保留时拒绝释放。
- `POST /api/evidence/{id}/retention-reviews`：保管员提交保留期限复核（延期 `extend` / 提前结束 `shorten`），写明原因和新到期日；同一证据同时只允许一条在途申请。
- `POST /api/retention-reviews/{id}`：案件创建人或审计员处理申请，`{"approve": true/false}`；拒绝必须填写 `decision_note`，提交人不能自批。
- `GET /api/evidence/{id}/retention-reviews`、`GET /api/cases/{id}/retention-reviews`：查看证据或案件下的全部复核申请（含拒绝原因）。
- `GET /api/cases/{id}/report`：校验所有证据哈希和每条事件链，导出完整报告。
- 所有 `DELETE` 请求返回 405；证据和保管记录不提供删除接口。

## 保留期限复核规则

- 到期日变更不重新入册：批准后直接更新 `retention_until`，并在原保管编号上追加 `RETENTION_EXTEND` / `RETENTION_SHORTEN` 事件，事件哈希链不被割开。
- 提交与批准分离：保管员提交；案件创建人或审计员处理；提交人不能自批。
- 批准前重新核对最新状态：法律保留、证据释放、提交人成员角色、当前到期日任一变化都会阻止旧申请盖掉新状态（返回 409，申请保留待处理，可拒绝后重新提交）。
- 法律保留中不得缩短保留期限（提交与批准两处均校验）；保留中延期照常。
- 报告随每条证据保留申请时的原到期日快照、申请内容、批准前日期、处理人与拒绝原因。

## 分层结构

- `retention.py`：期限规则（纯规则，可独立测试）。
- `reviews.py`：期限复核服务（提交、审批、批准前再核对、事件与审计写入）。
- `migrations.py`：schema 版本与旧库升级（V0 → V1，重建事件表逐条搬运旧事件，旧证据继续可查）。
- `app.py`：存储与 HTTP 路由层。
- `web/index.html`：页面交互（提交申请、批准/拒绝、查看拒绝原因和完整报告）。

保管事件通过前一条事件哈希串联；报告会重新计算文件哈希和事件链。项目适合流程与完整性原型，不涵盖现实中的签名证书、WORM 存储、证据文件加密或司法辖区合规认证。
