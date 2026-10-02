"""
临时授权模块测试用例

覆盖: 授权依据必填、生效区间(含跨午夜)、重复授权与范围交叠的一致处理、
撤销立即失效、版本化变更、审计记录不可删改、访问放行版本可追溯。
"""
from datetime import datetime, timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import Category, Goods, Unit, Variety

from .models import AccessAuditLog, ImmutableAuditLogError, TemporaryGrant
from .services import create_grant, evaluate_access


def aware(dt):
    """生成带时区的本地时间"""
    return timezone.make_aware(dt)


class AuthorizationFixture(TestCase):
    """公共数据准备"""

    def setUp(self):
        self.admin = User.objects.create_user("sec-admin", "admin-pass-1", role="admin")
        self.appraiser = User.objects.create_user("appraiser-1", "appr-pass-1", role="appraiser")
        self.appraiser2 = User.objects.create_user("appraiser-2", "appr-pass-2", role="appraiser")

        self.admin_client = APIClient()
        self.admin_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.admin)}")
        self.appraiser_client = APIClient()
        self.appraiser_client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.appraiser)}")

        unit = Unit.objects.create(name="件", created_by=self.admin)
        category = Category.objects.create(name="证物", unit=unit, created_by=self.admin)
        variety = Variety.objects.create(name="检材", category=category, created_by=self.admin)
        self.goods_a = Goods.objects.create(variety=variety, name="物证甲", code="EV-A", quantity=3)
        self.goods_b = Goods.objects.create(variety=variety, name="物证乙", code="EV-B", quantity=5)

        self.now = timezone.now()
        self.active_window = {
            "effective_from": (self.now - timedelta(hours=1)).isoformat(),
            "effective_until": (self.now + timedelta(hours=1)).isoformat(),
        }

    def grant_payload(self, **overrides):
        payload = {
            "grantee_id": self.appraiser.id,
            "goods_ids": [self.goods_a.id],
            "reason": "司法鉴定委托书 SF-2026-001, 预约时段内查验物证",
            **self.active_window,
        }
        payload.update(overrides)
        return payload

    def create_active_grant(self, goods=None, grantee=None, **window_overrides):
        """直接通过服务层创建一个当前生效的授权"""
        window = {
            "effective_from": self.now - timedelta(hours=1),
            "effective_until": self.now + timedelta(hours=1),
        }
        window.update(window_overrides)
        return create_grant(
            grantee=grantee or self.appraiser,
            granted_by=self.admin,
            reason="司法鉴定委托书 SF-2026-001",
            goods_ids=[(goods or self.goods_a).id],
            **window,
        )


class GrantCreateAPITest(AuthorizationFixture):
    """授权创建与依据校验"""

    url = "/api/authorization/grants/"

    def test_create_grant_success(self):
        response = self.admin_client.post(self.url, self.grant_payload(), format="json")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["reason"], "司法鉴定委托书 SF-2026-001, 预约时段内查验物证")
        self.assertEqual([g["id"] for g in data["goods_list"]], [self.goods_a.id])
        self.assertTrue(data["grant_no"].startswith("TG-"))

    def test_reason_is_mandatory(self):
        payload = self.grant_payload()
        del payload["reason"]
        response = self.admin_client.post(self.url, payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("授权依据", response.json()["message"])

    def test_blank_reason_rejected(self):
        response = self.admin_client.post(self.url, self.grant_payload(reason="   "), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("授权依据", response.json()["message"])

    def test_non_admin_cannot_create(self):
        response = self.appraiser_client.post(self.url, self.grant_payload(), format="json")
        self.assertEqual(response.status_code, 403)

    def test_non_admin_cannot_list(self):
        response = self.appraiser_client.get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_admin_cannot_be_grantee(self):
        response = self.admin_client.post(
            self.url, self.grant_payload(grantee_id=self.admin.id), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("管理员", response.json()["message"])

    def test_invalid_window_rejected(self):
        # 失效时间早于生效时间
        response = self.admin_client.post(
            self.url,
            self.grant_payload(
                effective_from=(self.now + timedelta(hours=2)).isoformat(),
                effective_until=(self.now + timedelta(hours=1)).isoformat(),
            ),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("失效时间必须晚于生效时间", response.json()["message"])

    def test_past_window_rejected(self):
        response = self.admin_client.post(
            self.url,
            self.grant_payload(
                effective_from=(self.now - timedelta(hours=3)).isoformat(),
                effective_until=(self.now - timedelta(hours=1)).isoformat(),
            ),
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("当前时间", response.json()["message"])

    def test_unknown_goods_rejected(self):
        response = self.admin_client.post(
            self.url, self.grant_payload(goods_ids=[self.goods_a.id, 99999]), format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("物资不存在", response.json()["message"])

    def test_empty_scope_rejected(self):
        response = self.admin_client.post(
            self.url, self.grant_payload(goods_ids=[]), format="json"
        )
        self.assertEqual(response.status_code, 400)


class CrossMidnightWindowTest(AuthorizationFixture):
    """跨午夜时段与区间边界的一致处理(左闭右开)"""

    def setUp(self):
        super().setUp()
        # 预约时段: 10月2日 22:00 至 10月3日 02:00(跨午夜)
        self.overnight_grant = create_grant(
            grantee=self.appraiser,
            granted_by=self.admin,
            reason="夜间鉴定预约",
            goods_ids=[self.goods_a.id],
            effective_from=aware(datetime(2026, 10, 2, 22, 0)),
            effective_until=aware(datetime(2026, 10, 3, 2, 0)),
        )

    def test_before_midnight_allowed(self):
        decision = evaluate_access(self.appraiser, self.goods_a, at=aware(datetime(2026, 10, 2, 23, 30)))
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.grant.id, self.overnight_grant.id)

    def test_after_midnight_allowed(self):
        decision = evaluate_access(self.appraiser, self.goods_a, at=aware(datetime(2026, 10, 3, 0, 30)))
        self.assertTrue(decision.allowed)

    def test_window_start_is_inclusive(self):
        decision = evaluate_access(self.appraiser, self.goods_a, at=aware(datetime(2026, 10, 2, 22, 0)))
        self.assertTrue(decision.allowed)

    def test_window_end_is_exclusive(self):
        decision = evaluate_access(self.appraiser, self.goods_a, at=aware(datetime(2026, 10, 3, 2, 0)))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "expired")

    def test_before_window_denied(self):
        decision = evaluate_access(self.appraiser, self.goods_a, at=aware(datetime(2026, 10, 2, 21, 59)))
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason_code, "not_started")

    def test_adjacent_windows_do_not_overlap(self):
        """相邻区间(一个的终点等于另一个的起点)不构成重复授权"""
        response = self.admin_client.post(
            "/api/authorization/grants/",
            self.grant_payload(
                effective_from=aware(datetime(2026, 10, 3, 2, 0)).isoformat(),
                effective_until=aware(datetime(2026, 10, 3, 6, 0)).isoformat(),
            ),
            format="json",
        )
        self.assertEqual(response.status_code, 200)


class DuplicateAndOverlapTest(AuthorizationFixture):
    """重复授权与范围交叠的一致处理"""

    url = "/api/authorization/grants/"

    def test_exact_duplicate_rejected(self):
        payload = self.grant_payload()
        first = self.admin_client.post(self.url, payload, format="json")
        second = self.admin_client.post(self.url, payload, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertIn("重复授权", second.json()["message"])

    def test_same_scope_overlapping_time_rejected(self):
        self.admin_client.post(self.url, self.grant_payload(), format="json")
        shifted = self.grant_payload(
            effective_from=self.now.isoformat(),
            effective_until=(self.now + timedelta(hours=2)).isoformat(),
        )
        response = self.admin_client.post(self.url, shifted, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("重复授权", response.json()["message"])

    def test_same_scope_disjoint_time_allowed(self):
        self.admin_client.post(self.url, self.grant_payload(), format="json")
        later = self.grant_payload(
            effective_from=(self.now + timedelta(hours=2)).isoformat(),
            effective_until=(self.now + timedelta(hours=3)).isoformat(),
        )
        response = self.admin_client.post(self.url, later, format="json")
        self.assertEqual(response.status_code, 200)

    def test_partial_scope_overlap_allowed(self):
        """物资集合不同(部分交叠)不属于重复授权"""
        self.admin_client.post(self.url, self.grant_payload(), format="json")
        wider = self.grant_payload(goods_ids=[self.goods_a.id, self.goods_b.id])
        response = self.admin_client.post(self.url, wider, format="json")
        self.assertEqual(response.status_code, 200)

    def test_overlapping_grants_resolve_deterministically(self):
        """范围交叠时, 放行命中的授权版本确定且被审计记录"""
        early = self.create_active_grant(
            effective_from=self.now - timedelta(hours=2),
            effective_until=self.now + timedelta(hours=2),
        )
        late = self.create_active_grant(
            effective_from=self.now - timedelta(minutes=30),
            effective_until=self.now + timedelta(minutes=30),
        )
        response = self.appraiser_client.get(f"/api/authorization/case-goods/{self.goods_a.id}/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        # 命中规则: 生效起始时间更晚的授权优先
        self.assertEqual(data["grant_no"], late.grant_no)
        self.assertNotEqual(data["grant_no"], early.grant_no)
        log = AccessAuditLog.objects.get(id=data["audit_id"])
        self.assertEqual(log.grant_no, late.grant_no)
        self.assertEqual(log.grant_version, late.version)

    def test_regrant_after_expiry_allowed(self):
        """授权过期后允许按相同范围重新授权"""
        expired = create_grant(
            grantee=self.appraiser,
            granted_by=self.admin,
            reason="首次鉴定",
            goods_ids=[self.goods_a.id],
            effective_from=self.now - timedelta(hours=3),
            effective_until=self.now - timedelta(hours=1),
        )
        self.assertEqual(expired.status, "expired")
        response = self.admin_client.post(self.url, self.grant_payload(), format="json")
        self.assertEqual(response.status_code, 200)


class RevokeGrantAPITest(AuthorizationFixture):
    """撤销: 立即失效且不可重复操作"""

    def test_revoke_blocks_new_requests_immediately(self):
        grant = self.create_active_grant()
        access_url = f"/api/authorization/case-goods/{self.goods_a.id}/"

        allowed = self.appraiser_client.get(access_url)
        self.assertEqual(allowed.status_code, 200)

        revoked = self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/revoke/",
            {"revoke_reason": "鉴定提前完成, 回收权限"},
            format="json",
        )
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(revoked.json()["data"]["status"], "revoked")

        denied = self.appraiser_client.get(access_url)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["data"]["deny_reason"], "revoked")

    def test_revoke_reason_required(self):
        grant = self.create_active_grant()
        response = self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/revoke/", {}, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("撤销原因", response.json()["message"])

    def test_double_revoke_rejected(self):
        grant = self.create_active_grant()
        url = f"/api/authorization/grants/{grant.id}/revoke/"
        first = self.admin_client.post(url, {"revoke_reason": "原因"}, format="json")
        second = self.admin_client.post(url, {"revoke_reason": "原因"}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)
        self.assertIn("已被撤销", second.json()["message"])

    def test_revoke_expired_grant_rejected(self):
        grant = create_grant(
            grantee=self.appraiser,
            granted_by=self.admin,
            reason="首次鉴定",
            goods_ids=[self.goods_a.id],
            effective_from=self.now - timedelta(hours=3),
            effective_until=self.now - timedelta(hours=1),
        )
        response = self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/revoke/",
            {"revoke_reason": "原因"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("已过期", response.json()["message"])

    def test_revoke_covers_all_versions(self):
        """撤销作用于整个授权组, 包括历史版本"""
        grant = self.create_active_grant()
        self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/amend/",
            {
                "goods_ids": [self.goods_a.id],
                "effective_from": (self.now - timedelta(hours=1)).isoformat(),
                "effective_until": (self.now + timedelta(hours=2)).isoformat(),
                "reason": "延长鉴定时段",
            },
            format="json",
        )
        current = TemporaryGrant.objects.get(grant_no=grant.grant_no, version=2)
        self.admin_client.post(
            f"/api/authorization/grants/{current.id}/revoke/",
            {"revoke_reason": "任务取消"},
            format="json",
        )
        versions = TemporaryGrant.objects.filter(grant_no=grant.grant_no)
        self.assertEqual(versions.count(), 2)
        for version in versions:
            self.assertEqual(version.status, "revoked")


class AmendGrantAPITest(AuthorizationFixture):
    """版本化变更: 旧版本被取代, 审计可追溯版本号"""

    def test_amend_creates_new_version(self):
        grant = self.create_active_grant()
        response = self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/amend/",
            {
                "goods_ids": [self.goods_a.id, self.goods_b.id],
                "effective_from": (self.now - timedelta(hours=1)).isoformat(),
                "effective_until": (self.now + timedelta(hours=3)).isoformat(),
                "reason": "补充鉴定事项, 扩大查验范围",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["grant_no"], grant.grant_no)
        self.assertEqual(len(data["goods_list"]), 2)

        grant.refresh_from_db()
        self.assertEqual(grant.status, "superseded")
        self.assertFalse(grant.is_current)

    def test_amend_revoked_grant_rejected(self):
        grant = self.create_active_grant()
        self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/revoke/",
            {"revoke_reason": "任务取消"},
            format="json",
        )
        response = self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/amend/",
            {
                "goods_ids": [self.goods_a.id],
                "effective_from": (self.now - timedelta(hours=1)).isoformat(),
                "effective_until": (self.now + timedelta(hours=2)).isoformat(),
                "reason": "尝试变更已撤销授权",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("已撤销", response.json()["message"])

    def test_amend_superseded_version_rejected(self):
        grant = self.create_active_grant()
        amend_payload = {
            "goods_ids": [self.goods_a.id],
            "effective_from": (self.now - timedelta(hours=1)).isoformat(),
            "effective_until": (self.now + timedelta(hours=2)).isoformat(),
            "reason": "第一次变更",
        }
        self.admin_client.post(f"/api/authorization/grants/{grant.id}/amend/", amend_payload, format="json")
        # 对已取代的旧版本再次变更
        response = self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/amend/", amend_payload, format="json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("已被取代", response.json()["message"])

    def test_audit_traces_grant_version(self):
        """同一授权编号下, 不同版本放行的访问在审计中可区分"""
        grant = self.create_active_grant()
        access_url = f"/api/authorization/case-goods/{self.goods_a.id}/"

        first = self.appraiser_client.get(access_url).json()["data"]
        self.assertEqual(first["grant_version"], 1)

        self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/amend/",
            {
                "goods_ids": [self.goods_a.id],
                "effective_from": (self.now - timedelta(hours=1)).isoformat(),
                "effective_until": (self.now + timedelta(hours=2)).isoformat(),
                "reason": "延长鉴定时段",
            },
            format="json",
        )
        second = self.appraiser_client.get(access_url).json()["data"]
        self.assertEqual(second["grant_version"], 2)

        logs = AccessAuditLog.objects.filter(grant_no=grant.grant_no, decision="allowed")
        self.assertEqual(
            sorted(logs.values_list("grant_version", flat=True)),
            [1, 2],
        )

    def test_grant_detail_shows_version_history(self):
        grant = self.create_active_grant()
        self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/amend/",
            {
                "goods_ids": [self.goods_a.id],
                "effective_from": (self.now - timedelta(hours=1)).isoformat(),
                "effective_until": (self.now + timedelta(hours=2)).isoformat(),
                "reason": "延长鉴定时段",
            },
            format="json",
        )
        response = self.admin_client.get(f"/api/authorization/grants/{grant.id}/")
        self.assertEqual(response.status_code, 200)
        versions = response.json()["data"]["versions"]
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[0]["status"], "superseded")
        self.assertEqual(versions[1]["status"], "active")


class CaseGoodsAccessAPITest(AuthorizationFixture):
    """鉴定查看通道: 放行、拒绝与审计留痕"""

    def test_access_without_grant_denied_and_audited(self):
        response = self.appraiser_client.get(f"/api/authorization/case-goods/{self.goods_a.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertIn("无有效授权", response.json()["message"])
        log = AccessAuditLog.objects.get(id=response.json()["data"]["audit_id"])
        self.assertEqual(log.decision, "denied")
        self.assertEqual(log.deny_reason, "no_grant")
        self.assertEqual(log.username, self.appraiser.username)

    def test_access_outside_scope_denied(self):
        self.create_active_grant(goods=self.goods_a)
        response = self.appraiser_client.get(f"/api/authorization/case-goods/{self.goods_b.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["data"]["deny_reason"], "goods_not_in_scope")

    def test_access_with_active_grant_allowed(self):
        grant = self.create_active_grant()
        response = self.appraiser_client.get(f"/api/authorization/case-goods/{self.goods_a.id}/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["goods"]["code"], "EV-A")
        self.assertEqual(data["basis"], "grant")
        self.assertEqual(data["grant_no"], grant.grant_no)
        self.assertEqual(data["grant_version"], 1)

    def test_admin_access_allowed_by_role(self):
        response = self.admin_client.get(f"/api/authorization/case-goods/{self.goods_a.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["basis"], "role")

    def test_pending_grant_denies_access(self):
        self.create_active_grant(
            effective_from=self.now + timedelta(hours=1),
            effective_until=self.now + timedelta(hours=2),
        )
        response = self.appraiser_client.get(f"/api/authorization/case-goods/{self.goods_a.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["data"]["deny_reason"], "not_started")

    def test_anonymous_access_rejected(self):
        response = APIClient().get(f"/api/authorization/case-goods/{self.goods_a.id}/")
        self.assertEqual(response.status_code, 401)

    def test_access_unknown_goods_returns_404(self):
        response = self.appraiser_client.get("/api/authorization/case-goods/99999/")
        self.assertEqual(response.status_code, 404)


class AccessCheckAPITest(AuthorizationFixture):
    """决策预检: 安全管理员可看清某次访问由哪个授权版本放行"""

    url = "/api/authorization/check/"

    def test_check_returns_grant_version(self):
        grant = self.create_active_grant()
        response = self.admin_client.get(
            self.url, {"user": self.appraiser.id, "goods": self.goods_a.id}
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertTrue(data["allowed"])
        self.assertEqual(data["basis"], "grant")
        self.assertEqual(data["grant"]["grant_no"], grant.grant_no)
        self.assertEqual(data["grant"]["version"], 1)

    def test_check_at_specific_moment(self):
        grant = create_grant(
            grantee=self.appraiser,
            granted_by=self.admin,
            reason="夜间鉴定预约",
            goods_ids=[self.goods_a.id],
            effective_from=aware(datetime(2026, 10, 2, 22, 0)),
            effective_until=aware(datetime(2026, 10, 3, 2, 0)),
        )
        response = self.admin_client.get(
            self.url,
            {
                "user": self.appraiser.id,
                "goods": self.goods_a.id,
                "at": aware(datetime(2026, 10, 3, 0, 30)).isoformat(),
            },
        )
        data = response.json()["data"]
        self.assertTrue(data["allowed"])
        self.assertEqual(data["grant"]["grant_no"], grant.grant_no)

    def test_check_denied_explains_reason(self):
        response = self.admin_client.get(
            self.url, {"user": self.appraiser.id, "goods": self.goods_a.id}
        )
        data = response.json()["data"]
        self.assertFalse(data["allowed"])
        self.assertEqual(data["reason_code"], "no_grant")
        self.assertIsNone(data["grant"])

    def test_check_does_not_write_audit(self):
        self.create_active_grant()
        self.admin_client.get(self.url, {"user": self.appraiser.id, "goods": self.goods_a.id})
        self.assertEqual(AccessAuditLog.objects.count(), 0)

    def test_non_admin_cannot_check(self):
        response = self.appraiser_client.get(
            self.url, {"user": self.appraiser.id, "goods": self.goods_a.id}
        )
        self.assertEqual(response.status_code, 403)


class AccessAuditLogAPITest(AuthorizationFixture):
    """审计记录查询与不可删除"""

    def setUp(self):
        super().setUp()
        self.grant = self.create_active_grant()
        self.appraiser_client.get(f"/api/authorization/case-goods/{self.goods_a.id}/")
        self.log = AccessAuditLog.objects.get()

    def test_admin_can_list_audit_logs(self):
        response = self.admin_client.get("/api/authorization/audit-logs/")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total"], 1)
        entry = data["list"][0]
        self.assertEqual(entry["grant_no"], self.grant.grant_no)
        self.assertEqual(entry["grant_version"], 1)
        self.assertEqual(entry["decision"], "allowed")

    def test_filter_by_grant_no_and_decision(self):
        matched = self.admin_client.get(
            "/api/authorization/audit-logs/", {"grant_no": self.grant.grant_no}
        )
        missed = self.admin_client.get(
            "/api/authorization/audit-logs/", {"decision": "denied"}
        )
        self.assertEqual(matched.json()["data"]["total"], 1)
        self.assertEqual(missed.json()["data"]["total"], 0)

    def test_non_admin_cannot_list_audit_logs(self):
        response = self.appraiser_client.get("/api/authorization/audit-logs/")
        self.assertEqual(response.status_code, 403)

    def test_delete_via_api_rejected(self):
        response = self.admin_client.delete(f"/api/authorization/audit-logs/{self.log.id}/")
        self.assertEqual(response.status_code, 403)
        self.assertIn("不可删除", response.json()["message"])
        self.assertTrue(AccessAuditLog.objects.filter(pk=self.log.id).exists())


class AccessAuditImmutabilityTest(AuthorizationFixture):
    """审计记录一经形成不可修改、不可删除(ORM 层强制)"""

    def setUp(self):
        super().setUp()
        self.grant = self.create_active_grant()
        self.appraiser_client.get(f"/api/authorization/case-goods/{self.goods_a.id}/")
        self.log = AccessAuditLog.objects.get()

    def test_model_delete_blocked(self):
        with self.assertRaises(ImmutableAuditLogError):
            self.log.delete()
        self.assertTrue(AccessAuditLog.objects.filter(pk=self.log.id).exists())

    def test_queryset_delete_blocked(self):
        with self.assertRaises(ImmutableAuditLogError):
            AccessAuditLog.objects.all().delete()
        self.assertEqual(AccessAuditLog.objects.count(), 1)

    def test_queryset_update_blocked(self):
        with self.assertRaises(ImmutableAuditLogError):
            AccessAuditLog.objects.filter(pk=self.log.id).update(decision="denied")

    def test_model_save_mutation_blocked(self):
        self.log.decision = "denied"
        with self.assertRaises(ImmutableAuditLogError):
            self.log.save()
        self.log.refresh_from_db()
        self.assertEqual(self.log.decision, "allowed")

    def test_audit_survives_grant_revocation(self):
        """授权撤销后, 授权期间形成的审计记录仍然完整可查"""
        self.admin_client.post(
            f"/api/authorization/grants/{self.grant.id}/revoke/",
            {"revoke_reason": "鉴定完成"},
            format="json",
        )
        log = AccessAuditLog.objects.get(pk=self.log.id)
        self.assertEqual(log.decision, "allowed")
        self.assertEqual(log.grant_no, self.grant.grant_no)
        self.assertEqual(log.grant_version, 1)


class GrantListAPITest(AuthorizationFixture):
    """授权列表与状态过滤"""

    def test_list_defaults_to_current_versions(self):
        grant = self.create_active_grant()
        self.admin_client.post(
            f"/api/authorization/grants/{grant.id}/amend/",
            {
                "goods_ids": [self.goods_a.id],
                "effective_from": (self.now - timedelta(hours=1)).isoformat(),
                "effective_until": (self.now + timedelta(hours=2)).isoformat(),
                "reason": "延长鉴定时段",
            },
            format="json",
        )
        default = self.admin_client.get("/api/authorization/grants/")
        self.assertEqual(default.json()["data"]["total"], 1)
        self.assertEqual(default.json()["data"]["list"][0]["version"], 2)

        full = self.admin_client.get("/api/authorization/grants/", {"include_superseded": "true"})
        self.assertEqual(full.json()["data"]["total"], 2)

    def test_filter_by_status(self):
        self.create_active_grant()
        create_grant(
            grantee=self.appraiser2,
            granted_by=self.admin,
            reason="次日鉴定预约",
            goods_ids=[self.goods_b.id],
            effective_from=self.now + timedelta(hours=3),
            effective_until=self.now + timedelta(hours=5),
        )
        active = self.admin_client.get("/api/authorization/grants/", {"status": "active"})
        pending = self.admin_client.get("/api/authorization/grants/", {"status": "pending"})
        self.assertEqual(active.json()["data"]["total"], 1)
        self.assertEqual(pending.json()["data"]["total"], 1)

    def test_filter_by_grantee(self):
        self.create_active_grant()
        response = self.admin_client.get(
            "/api/authorization/grants/", {"grantee": self.appraiser.id}
        )
        self.assertEqual(response.json()["data"]["total"], 1)
        response = self.admin_client.get(
            "/api/authorization/grants/", {"grantee": self.appraiser2.id}
        )
        self.assertEqual(response.json()["data"]["total"], 0)
