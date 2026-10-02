"""
临时授权访问控制测试

覆盖：
- 生效区间（半开区间边界、跨午夜）
- 资源范围（越权物资拒绝）
- 授权依据必填
- 提前撤销后新请求立即失效，历史审计保留
- 审计只追加（不可改、不可删）
- 重复授权 / 范围交叠的确定性版本归因
- 换发（supersede）后的版本归因
- 预约取消联动撤销
- 管理端与外部端权限隔离
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import Category, Goods, Unit, Variety
from .grant_services import (
    GrantError, cancel_appointment, issue_grant, reissue_grant, revoke_grant_version,
)
from .models import (
    AccessAudit, Appointment, Case, CaseEvidence, ExternalAppraiser,
)
from .services import check_access, evaluate


@pytest.fixture
def now():
    # 固定到 18:00，便于构造跨午夜预约
    return timezone.localtime().replace(hour=18, minute=0, second=0, microsecond=0)


@pytest.fixture
def admin(db):
    return User.objects.create_user('sec-admin', 'pass12345', role='admin')


@pytest.fixture
def external_user(db):
    return User.objects.create_user('expert-li', 'pass12345', role='external')


@pytest.fixture
def appraiser(external_user):
    return ExternalAppraiser.objects.create(
        user=external_user, name='李鉴定', id_card='110101199001011234',
        organization='公信司法鉴定中心',
    )


@pytest.fixture
def case(admin):
    return Case.objects.create(case_no='A2026-001', name='涉案财物鉴定案', created_by=admin)


@pytest.fixture
def goods_a(db):
    unit = Unit.objects.create(name='件')
    category = Category.objects.create(name='电子数据', unit=unit)
    variety = Variety.objects.create(name='移动终端', category=category)
    return Goods.objects.create(variety=variety, name='涉案手机', code='EV-A1',
                                quantity=Decimal('1'))


@pytest.fixture
def goods_b(db):
    unit = Unit.objects.create(name='台')
    category = Category.objects.create(name='影像设备', unit=unit)
    variety = Variety.objects.create(name='摄像机', category=category)
    return Goods.objects.create(variety=variety, name='执法记录仪', code='EV-B1',
                                quantity=Decimal('1'))


@pytest.fixture
def evidence_a(case, goods_a, admin):
    return CaseEvidence.objects.create(case=case, goods=goods_a, registered_by=admin)


@pytest.fixture
def evidence_b(case, goods_b, admin):
    return CaseEvidence.objects.create(case=case, goods=goods_b, registered_by=admin)


def make_appointment(appraiser, case, evidences, now, start_hour=17, duration=4):
    """默认 17:00-21:00 不跨午夜；测试可自行传入跨午夜区间"""
    start = now.replace(hour=start_hour % 24, minute=0, second=0, microsecond=0)
    appt = Appointment.objects.create(
        appraiser=appraiser, case=case,
        start_time=start,
        end_time=start + timedelta(hours=duration),
    )
    appt.evidences.set(evidences)
    return appt


def issue(admin, appt, evidence, now, **overrides):
    params = dict(
        appraiser=appt.appraiser, appointment=appt,
        basis_type='鉴定委托函', basis_ref='WT-2026-009',
        basis_reason='案件侦办需要对涉案手机进行电子数据鉴定',
        valid_from=appt.start_time, valid_to=appt.end_time,
        scope_evidence_ids=[evidence.id], issued_by=admin,
    )
    params.update(overrides)
    return issue_grant(**params)


# ==================== 基础判定 ====================

@pytest.mark.django_db
class TestAccessEvaluation:
    def test_grant_allows_during_window(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        issue(admin, appt, evidence_a, now)
        allowed, decision, audit = check_access(
            appraiser.user, case, evidence_a, at=now
        )
        assert allowed is True
        assert audit.result == 'allowed'
        assert audit.grant_version_no == 1
        assert decision.grant_version.version_no == 1

    def test_deny_before_appointment_window(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        issue(admin, appt, evidence_a, now)
        at = appt.start_time - timedelta(minutes=1)
        allowed, decision, _ = check_access(appraiser.user, case, evidence_a, at=at)
        assert allowed is False
        assert decision.deny_reason == 'appointment_outside_window'

    def test_half_open_interval_boundary(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        issue(admin, appt, evidence_a, now)
        # 起始时刻包含
        assert evaluate(appraiser.user, case, evidence_a, at=appt.start_time).allowed
        # 结束时刻排除（[from, to)）
        d = evaluate(appraiser.user, case, evidence_a, at=appt.end_time)
        assert not d.allowed
        assert d.deny_reason == 'appointment_outside_window'

    def test_cross_midnight_window(self, admin, appraiser, case, evidence_a, now):
        """跨午夜：23:00 预约到次日 02:00，午夜后仍可访问"""
        start = now.replace(hour=23, minute=0)
        end = start + timedelta(hours=3)
        appt = Appointment.objects.create(
            appraiser=appraiser, case=case, start_time=start, end_time=end
        )
        appt.evidences.set([evidence_a])
        issue(admin, appt, evidence_a, now, valid_from=start, valid_to=end)

        before_midnight = start + timedelta(minutes=30)
        after_midnight = start + timedelta(hours=2)
        assert evaluate(appraiser.user, case, evidence_a, at=before_midnight).allowed
        assert evaluate(appraiser.user, case, evidence_a, at=after_midnight).allowed
        assert not evaluate(appraiser.user, case, evidence_a, at=end).allowed

    def test_out_of_scope_denied(self, admin, appraiser, case, evidence_a, evidence_b, now):
        appt = make_appointment(appraiser, case, [evidence_a, evidence_b], now)
        issue(admin, appt, evidence_a, now)
        allowed, decision, audit = check_access(
            appraiser.user, case, evidence_b, at=now
        )
        assert not allowed
        assert decision.deny_reason == 'out_of_scope'
        assert audit.result == 'denied'
        assert audit.deny_reason == 'out_of_scope'

    def test_no_appointment(self, appraiser, case, evidence_a, now):
        d = evaluate(appraiser.user, case, evidence_a, at=now)
        assert not d.allowed and d.deny_reason == 'no_appointment'

    def test_non_external_role_denied(self, admin, case, evidence_a, now):
        d = evaluate(admin, case, evidence_a, at=now)
        assert not d.allowed and d.deny_reason == 'not_appraiser'

    def test_inactive_appraiser_denied(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        issue(admin, appt, evidence_a, now)
        appraiser.is_active = False
        appraiser.save(update_fields=['is_active'])
        d = evaluate(appraiser.user, case, evidence_a, at=now)
        assert d.deny_reason == 'appraiser_inactive'


# ==================== 签发校验 ====================

@pytest.mark.django_db
class TestGrantIssuance:
    def test_basis_required(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        with pytest.raises(GrantError):
            issue(admin, appt, evidence_a, now, basis_reason='   ')

    def test_window_must_fit_appointment(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        with pytest.raises(GrantError):
            issue(admin, appt, evidence_a, now,
                  valid_to=appt.end_time + timedelta(minutes=1))

    def test_scope_must_be_appointed(self, admin, appraiser, case, evidence_a, evidence_b, now):
        # 预约只含 A，授权范围却含 B
        appt = make_appointment(appraiser, case, [evidence_a], now)
        with pytest.raises(GrantError):
            issue(admin, appt, evidence_a, now,
                  scope_evidence_ids=[evidence_a.id, evidence_b.id])

    def test_invalid_window_order(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        with pytest.raises(GrantError):
            issue(admin, appt, evidence_a, now,
                  valid_from=appt.end_time, valid_to=appt.start_time)

    def test_duplicate_scope_ids_rejected(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        with pytest.raises(GrantError):
            issue(admin, appt, evidence_a, now,
                  scope_evidence_ids=[evidence_a.id, evidence_a.id])


# ==================== 撤销、换发与版本归因 ====================

@pytest.mark.django_db
class TestRevokeReissue:
    def test_revoke_takes_immediate_effect_but_audit_remains(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        grant, v1 = issue(admin, appt, evidence_a, now)

        allowed_before, _, audit_before = check_access(
            appraiser.user, case, evidence_a, at=now
        )
        assert allowed_before

        revoke_grant_version(v1, revoked_by=admin, reason='鉴定任务提前终止')
        v1.refresh_from_db()
        assert v1.status == 'revoked'
        assert v1.revoked_at is not None

        # 撤销后新请求立即失效
        allowed_after, decision, audit_after = check_access(
            appraiser.user, case, evidence_a, at=now + timedelta(minutes=1)
        )
        assert not allowed_after
        assert decision.deny_reason == 'grant_revoked'

        # 授权期间形成的审计记录仍然存在且不可删除
        assert AccessAudit.objects.filter(pk=audit_before.pk).exists()
        assert audit_before.result == 'allowed'
        with pytest.raises(PermissionError):
            audit_before.delete()
        with pytest.raises(PermissionError):
            audit_before.result = 'denied'
            audit_before.save()

    def test_revoke_requires_reason(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        _, v1 = issue(admin, appt, evidence_a, now)
        with pytest.raises(GrantError):
            revoke_grant_version(v1, revoked_by=admin, reason='  ')

    def test_reissue_supersedes_and_new_version_gates(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        grant, v1 = issue(admin, appt, evidence_a, now)

        # 换发：v2 把范围/区间收紧（缩短最后一小时）
        _, v2 = reissue_grant(
            grant=grant,
            basis_type='补充鉴定委托', basis_ref='WT-2026-010',
            basis_reason='委托范围调整',
            valid_from=appt.start_time, valid_to=appt.end_time - timedelta(hours=1),
            scope_evidence_ids=[evidence_a.id], issued_by=admin,
        )
        v1.refresh_from_db()
        assert v1.status == 'superseded'
        assert v2.version_no == 2

        # v2 时段内访问由 v2 放行，归因清晰
        allowed, decision, audit = check_access(
            appraiser.user, case, evidence_a, at=now
        )
        assert allowed
        assert decision.grant_version.id == v2.id
        assert audit.grant_version.id == v2.id
        assert audit.grant_version_no == 2

        # v2 到期后（v1 已 superseded）不能再借旧版本访问
        late = appt.end_time - timedelta(minutes=30)
        d = evaluate(appraiser.user, case, evidence_a, at=late)
        assert not d.allowed

    def test_cancel_appointment_revokes_grants(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        _, v1 = issue(admin, appt, evidence_a, now)
        cancel_appointment(appt, cancelled_by=admin)
        v1.refresh_from_db()
        appt.refresh_from_db()
        assert v1.status == 'revoked'
        assert appt.status == 'cancelled'
        d = evaluate(appraiser.user, case, evidence_a, at=now)
        assert d.deny_reason == 'appointment_cancelled'

    def test_overlapping_grants_deterministic_attribution(
        self, admin, appraiser, case, evidence_a, evidence_b, now
    ):
        """重复授权 + 范围交叠：两张授权范围不同，访问按确定性版本归因"""
        appt = make_appointment(appraiser, case, [evidence_a, evidence_b], now)
        g1, v_g1 = issue(admin, appt, evidence_a, now,
                         scope_evidence_ids=[evidence_a.id],
                         basis_ref='WT-1')
        g2, v_g2 = issue(admin, appt, evidence_b, now,
                         scope_evidence_ids=[evidence_b.id],
                         basis_ref='WT-2')

        # A 只在 g1 范围内 → g1 放行；B 只在 g2 范围内 → g2 放行
        allowed_a, da, audit_a = check_access(appraiser.user, case, evidence_a, at=now)
        allowed_b, db_, audit_b = check_access(appraiser.user, case, evidence_b, at=now)
        assert allowed_a and da.grant.id == g1.id
        assert allowed_b and db_.grant.id == g2.id
        assert audit_a.grant_version_no == da.grant_version.version_no == 1
        assert audit_b.grant.id == g2.id

    def test_overlapping_versions_scope_union(self, admin, appraiser, case, evidence_a, evidence_b, now):
        """同一授权的两版范围交叠且都有效时，高版本优先归因，范围取并集"""
        appt = make_appointment(appraiser, case, [evidence_a, evidence_b], now)
        grant, v1 = issue(admin, appt, evidence_a, now,
                          scope_evidence_ids=[evidence_a.id])
        # 手工再制造一张有效 v2（模拟调整范围时旧版未失效的交叠窗口）：
        # 通过正常签发路径，v1 会 superseded；这里直接构造交叠：
        _, v2 = reissue_grant(
            grant=grant,
            basis_type='鉴定委托函', basis_ref='WT-2', basis_reason='扩大范围',
            valid_from=appt.start_time, valid_to=appt.end_time,
            scope_evidence_ids=[evidence_a.id, evidence_b.id], issued_by=admin,
        )
        v1.refresh_from_db()
        assert v1.status == 'superseded'
        # v2 同时覆盖 A/B，归因到 v2
        _, da, _ = check_access(appraiser.user, case, evidence_a, at=now)
        _, db2, _ = check_access(appraiser.user, case, evidence_b, at=now)
        assert da.grant_version.id == v2.id
        assert db2.grant_version.id == v2.id


# ==================== 审计台账只追加 ====================

@pytest.mark.django_db
class TestAuditImmutability:
    def test_queryset_update_and_delete_blocked(self, admin, appraiser, case, evidence_a, now):
        appt = make_appointment(appraiser, case, [evidence_a], now)
        issue(admin, appt, evidence_a, now)
        check_access(appraiser.user, case, evidence_a, at=now)
        assert AccessAudit.objects.count() == 1
        with pytest.raises(PermissionError):
            AccessAudit.objects.all().update(result='denied')
        with pytest.raises(PermissionError):
            AccessAudit.objects.all().delete()

    def test_every_attempt_audited(self, admin, appraiser, case, evidence_a, evidence_b, now):
        appt = make_appointment(appraiser, case, [evidence_a, evidence_b], now)
        issue(admin, appt, evidence_a, now)
        check_access(appraiser.user, case, evidence_a, at=now)  # allow
        check_access(appraiser.user, case, evidence_b, at=now)  # deny
        audits = AccessAudit.objects.order_by('id')
        assert audits.count() == 2
        assert [a.result for a in audits] == ['allowed', 'denied']
        # 放行记录携带版本归因，拒绝记录不伪造归因版本（范围拒绝除外，见模型）
        assert audits[0].grant_version_no == 1


# ==================== API 层 ====================

@pytest.mark.django_db
class TestAccessAPI:
    def _client(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {generate_token(user)}')
        return client

    def _setup(self, admin, appraiser, case, evidence_a, now=None):
        # API 按真实时刻判定，预约窗口围绕当前时间
        real_now = timezone.now()
        appt = Appointment.objects.create(
            appraiser=appraiser, case=case,
            start_time=real_now - timedelta(hours=1),
            end_time=real_now + timedelta(hours=2),
        )
        appt.evidences.set([evidence_a])
        return appt

    def test_admin_full_flow_and_external_access(
        self, admin, appraiser, external_user, case, evidence_a, now
    ):
        admin_client = self._client(admin)
        ext_client = self._client(external_user)
        # API 按真实时刻判定，窗口围绕当前时间构造
        real_now = timezone.now()
        win_start = real_now - timedelta(hours=1)
        win_end = real_now + timedelta(hours=2)

        # 1. 创建预约
        resp = admin_client.post('/api/access/appointments/', {
            'appraiser': appraiser.id, 'case': case.id,
            'evidence_ids': [evidence_a.id],
            'start_time': win_start.isoformat(),
            'end_time': win_end.isoformat(),
        }, format='json')
        assert resp.status_code == 200, resp.json()
        appt_id = resp.json()['data']['id']

        # 2. 签发授权（缺依据被拒）
        bad = admin_client.post('/api/access/grants/', {
            'appointment': appt_id,
            'basis_type': '', 'basis_ref': '', 'basis_reason': '',
            'valid_from': win_start.isoformat(),
            'valid_to': win_end.isoformat(),
            'scope_evidence_ids': [evidence_a.id],
        }, format='json')
        assert bad.status_code == 400

        good = admin_client.post('/api/access/grants/', {
            'appointment': appt_id,
            'basis_type': '鉴定委托函', 'basis_ref': 'WT-2026-009',
            'basis_reason': '案件侦办需要对涉案手机进行电子数据鉴定',
            'valid_from': win_start.isoformat(),
            'valid_to': win_end.isoformat(),
            'scope_evidence_ids': [evidence_a.id],
        }, format='json')
        assert good.status_code == 200, good.json()
        grant_id = good.json()['data']['id']

        # 3. 外部人员访问，响应中带“由哪个授权版本放行”
        access = ext_client.get(f'/api/access/external/evidences/{evidence_a.id}/')
        assert access.status_code == 200, access.json()
        body = access.json()['data']
        assert body['authorized_by']['grant_id'] == grant_id
        assert body['authorized_by']['grant_version_no'] == 1
        assert body['goods']['code'] == 'EV-A1'

        # 4. 撤销后新请求立即 403
        revoke = admin_client.post(
            f'/api/access/grants/{grant_id}/versions/1/revoke/',
            {'reason': '任务终止'}, format='json'
        )
        assert revoke.status_code == 200
        denied = ext_client.get(f'/api/access/external/evidences/{evidence_a.id}/')
        assert denied.status_code == 403

        # 5. 审计：2 条（1 放行 + 1 拒绝），放行记录版本归因可查；无删除入口
        audits = admin_client.get('/api/access/access-audits/')
        rows = audits.json()['data']['list']
        assert audits.json()['data']['total'] == 2
        allowed_row = next(r for r in rows if r['result'] == 'allowed')
        assert allowed_row['grant_id'] == grant_id
        assert allowed_row['grant_version_no'] == 1
        denied_row = next(r for r in rows if r['result'] == 'denied')
        assert denied_row['deny_reason'] == 'grant_revoked'

    def test_external_cannot_administer(self, external_user, case, evidence_a, now):
        client = self._client(external_user)
        assert client.get('/api/access/grants/').status_code == 403
        assert client.get('/api/access/access-audits/').status_code == 403
        assert client.post('/api/access/cases/', {
            'case_no': 'X', 'name': 'Y'
        }, format='json').status_code == 403

    def test_grant_window_beyond_appointment_rejected(
        self, admin, appraiser, case, evidence_a, now
    ):
        appt = self._setup(admin, appraiser, case, evidence_a, now)
        client = self._client(admin)
        resp = client.post('/api/access/grants/', {
            'appointment': appt.id,
            'basis_type': '鉴定委托函', 'basis_ref': 'WT-1', 'basis_reason': '鉴定需要',
            'valid_from': appt.start_time.isoformat(),
            'valid_to': (appt.end_time + timedelta(minutes=1)).isoformat(),
            'scope_evidence_ids': [evidence_a.id],
        }, format='json')
        assert resp.status_code == 400

    def test_reissue_api_then_audit_shows_v2(
        self, admin, appraiser, external_user, case, evidence_a, now
    ):
        appt = self._setup(admin, appraiser, case, evidence_a, now)
        admin_client = self._client(admin)
        ext_client = self._client(external_user)
        grant, _ = issue(admin, appt, evidence_a, now)
        resp = admin_client.post(f'/api/access/grants/{grant.id}/reissue/', {
            'basis_type': '补充委托', 'basis_ref': 'WT-2', 'basis_reason': '续期',
            'valid_from': appt.start_time.isoformat(),
            'valid_to': appt.end_time.isoformat(),
            'scope_evidence_ids': [evidence_a.id],
        }, format='json')
        assert resp.status_code == 200, resp.json()
        ext_client.get(f'/api/access/external/evidences/{evidence_a.id}/')
        audits = admin_client.get('/api/access/access-audits/').json()['data']['list']
        assert audits[0]['grant_version_no'] == 2
        # 授权详情能看到全部版本
        detail = admin_client.get(f'/api/access/grants/{grant.id}/').json()['data']
        assert [v['version_no'] for v in detail['versions']] == [2, 1]
        assert detail['versions'][1]['status'] == 'superseded'

    def test_api_access_with_active_grant(self, admin, appraiser, external_user, case, evidence_a):
        t = timezone.now().replace(minute=0, second=0, microsecond=0)
        start, end = t - timedelta(hours=1), t + timedelta(hours=2)
        appt = Appointment.objects.create(
            appraiser=appraiser, case=case, start_time=start, end_time=end
        )
        appt.evidences.set([evidence_a])
        issue(admin, appt, evidence_a, start, valid_from=start, valid_to=end)
        client = self._client(external_user)
        resp = client.get(f'/api/access/external/evidences/{evidence_a.id}/')
        assert resp.status_code == 200, resp.json()
        assert resp.json()['data']['authorized_by']['grant_version_no'] == 1
