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

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```

## 外部鉴定人员临时授权

针对外部鉴定人员「只应在预约时段内查看指定案件物资」的要求，系统提供带生效区间、
资源范围与授权依据的临时授权（`apps/access_control`）。

### 模型与判定

- **预约（Appointment）**：人员 + 案件 + 物资清单 + 半开时间区间 `[start, end)`，
  区间基于绝对时间，跨午夜照常生效。
- **临时授权（Grant / GrantVersion）**：依据（类型、文号、文字说明）必填，
  生效区间必须位于预约时段内，资源范围必须是预约清单中的在案物资。
  所有要素按版本快照保存；调整区间/范围/依据须换发（旧版置 `superseded` 留痕），
  撤销置 `revoked` 并立即生效。
- **判定链**：角色 → 档案状态 → 时段内已确认预约 → 生效中的授权版本 → 资源范围。
  重复授权、范围交叠允许存在，放行时按 `(版本号, 主键)` 确定性归因到唯一版本。
- **审计（AccessAudit）**：每次访问尝试（含拒绝）都追加一条记录，
  记录结论、拒绝原因以及 `grant_id` / `grant_version_no` / 版本主键；
  台账只追加，ORM 层禁止 update/delete，外键 PROTECT 阻止授权与版本被物理删除，
  撤销授权不影响授权期间已形成的审计记录。

### 主要接口（前缀 `/api/access`，均需管理员或外部人员角色）

| 方法 & 路径 | 说明 |
| --- | --- |
| POST/GET `cases/`、`cases/<id>/evidences/` | 案件与在案物资登记（管理员） |
| POST/GET `appraisers/` | 外部鉴定人员档案（管理员） |
| POST/GET `appointments/`、`appointments/<id>/cancel/` | 预约与取消（取消联动撤销授权，管理员） |
| POST/GET `grants/` | 签发临时授权 / 列表（管理员） |
| GET `grants/<id>/` | 授权详情，含全部历史版本 |
| POST `grants/<id>/reissue/` | 换发新版本（旧版 `superseded`） |
| POST `grants/<id>/versions/<n>/revoke/` | 提前撤销某版本（立即生效，需填原因） |
| GET `access-audits/`、`access-audits/<id>/` | 审计台账只读查询（可按授权、版本号、结论筛选） |
| GET `external/evidences/<id>/` | 外部人员访问在案物资，逐次判定并写审计；响应体 `authorized_by` 标明放行的授权版本 |
| GET `external/accessible/` | 外部人员当前可访问物资清单 |

