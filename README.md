# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 临时授权

外部鉴定人员不授予长期角色，改为按"生效区间 + 资源范围"发放临时授权，授权人必须填写授权依据。

- `POST /api/authorization/grants/` 创建授权（管理员）：`grantee_id`、`goods_ids`、`effective_from`、`effective_until`、`reason`（必填）
- `GET /api/authorization/grants/` 授权列表（默认仅当前版本，`include_superseded=true` 含历史版本，可按 `status`/`grantee`/`grant_no` 过滤）
- `GET /api/authorization/grants/<id>/` 授权详情，含同组全部版本历史
- `POST /api/authorization/grants/<id>/amend/` 变更授权：产生新版本，旧版本立即被取代
- `POST /api/authorization/grants/<id>/revoke/` 撤销授权（`revoke_reason` 必填）：整个授权组立即失效，新请求即时被拒绝
- `GET /api/authorization/case-goods/<goods_id>/` 鉴定查看通道：放行与拒绝均形成不可删改的审计记录
- `GET /api/authorization/check/?user=&goods=&at=` 决策预检（管理员）：查看某次访问由哪个授权版本放行，不写审计
- `GET /api/authorization/audit-logs/` 审计记录查询（管理员），记录精确到授权编号与版本号，不可删除

一致性约定：

- 生效区间为左闭右开 `[effective_from, effective_until)`：到达失效时刻即不可访问，相邻区间不相交，跨午夜时段按普通区间处理
- 同一被授权人、物资集合完全相同且时间相交的可用授权视为重复，创建/变更时拒绝；范围部分交叠允许，放行时命中生效起始更晚（其次创建更晚）的版本
- 审计记录一经形成不可修改、不可删除（ORM 层与 API 层双重阻断），授权撤销后依然完整可查

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
