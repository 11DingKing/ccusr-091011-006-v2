"""
临时授权服务层 - 授权判定与生命周期管理

一致性规则:
- 命中规则: 多个当前版本同时可放行时(范围交叠), 选择生效起始时间最晚者,
  若相同则选择 ID 最大(创建最晚)者, 判定结果确定且可解释。
- 重复授权: 同一被授权人、物资集合完全相同、时间区间相交且仍可用的授权
  视为重复, 创建/变更时拒绝; 部分交叠允许, 由命中规则确定放行版本。
- 撤销: 作用于整个授权组(所有版本), 事务内生效, 其后的新请求立即被拒绝。
"""
import logging
from dataclasses import dataclass
from typing import Optional

from django.db import transaction
from django.utils import timezone

from .models import AccessAuditLog, TemporaryGrant, TemporaryGrantScope

logger = logging.getLogger('apps')


@dataclass
class AccessDecision:
    """访问判定结果"""
    allowed: bool
    basis: str = ''                          # 'role' | 'grant'
    grant: Optional[TemporaryGrant] = None   # 命中放行的授权版本
    reason_code: str = ''                    # 拒绝原因代码
    reason_text: str = ''                    # 拒绝原因说明


def current_versions():
    """当前版本(未被取代)的授权查询集"""
    return TemporaryGrant.objects.filter(superseded_at__isnull=True)


def generate_grant_no():
    """生成授权编号 TG-YYYYMMDD-XXXX, 按日递增"""
    today = timezone.localdate()
    prefix = f"TG-{today:%Y%m%d}"
    used = (
        TemporaryGrant.objects.filter(grant_no__startswith=prefix)
        .values_list('grant_no', flat=True).distinct()
    )
    return f"{prefix}-{len(used) + 1:04d}"


def find_duplicate_grant(grantee, goods_ids, effective_from, effective_until, exclude_grant_no=None):
    """
    查找重复授权: 同一被授权人、物资集合完全相同、时间区间相交且仍可用的当前授权。
    区间相交判定(左闭右开): existing.from < new.until 且 new.from < existing.until。
    已撤销、已被取代、已过期的授权不参与判定(过期后允许重新授权)。
    """
    candidates = current_versions().filter(
        grantee=grantee,
        revoked_at__isnull=True,
        effective_from__lt=effective_until,
        # 区间相交且仍可用: existing.until 需同时大于新区间起点与当前时刻
        effective_until__gt=max(effective_from, timezone.now()),
    )
    if exclude_grant_no:
        candidates = candidates.exclude(grant_no=exclude_grant_no)
    target = set(goods_ids)
    for grant in candidates.prefetch_related('goods'):
        if set(grant.goods.values_list('id', flat=True)) == target:
            return grant
    return None


def evaluate_access(user, goods, at=None):
    """
    判定用户在指定时刻是否可访问指定物资。
    管理员凭角色放行; 其他用户需命中生效中的临时授权版本。
    """
    at = at or timezone.now()

    if user.is_admin:
        return AccessDecision(allowed=True, basis='role')

    candidates = list(
        current_versions()
        .filter(grantee=user, goods=goods)
        .order_by('-effective_from', '-id')
    )
    usable = [
        g for g in candidates
        if g.revoked_at is None and g.contains_moment(at)
    ]
    if usable:
        return AccessDecision(allowed=True, basis='grant', grant=usable[0])

    return AccessDecision(allowed=False, **_deny_reason(user, candidates, at))


def _deny_reason(user, candidates, at):
    """根据候选授权状态给出确定的拒绝原因"""
    if not candidates:
        has_any = current_versions().filter(
            grantee=user, revoked_at__isnull=True, effective_until__gt=at
        ).exists()
        if has_any:
            return {'reason_code': 'goods_not_in_scope', 'reason_text': '物资不在授权范围内'}
        return {'reason_code': 'no_grant', 'reason_text': '无有效授权'}

    live = [g for g in candidates if g.revoked_at is None]
    if not live:
        return {'reason_code': 'revoked', 'reason_text': '授权已被撤销'}

    def distance(grant):
        """访问时刻到授权区间的距离"""
        if at < grant.effective_from:
            return (grant.effective_from - at).total_seconds()
        return (at - grant.effective_until).total_seconds()

    nearest = min(live, key=distance)
    if at < nearest.effective_from:
        return {'reason_code': 'not_started', 'reason_text': '授权尚未生效'}
    return {'reason_code': 'expired', 'reason_text': '授权已过期'}


def record_access(*, user, goods, decision, path='', ip_address=None):
    """形成访问审计记录(放行与拒绝均记录, 记录后不可删改)"""
    grant = decision.grant if decision.basis == 'grant' else None
    log = AccessAuditLog.objects.create(
        user=user,
        username=user.username,
        goods=goods,
        goods_code=goods.code,
        goods_name=goods.name,
        grant=grant,
        grant_no=grant.grant_no if grant else '',
        grant_version=grant.version if grant else None,
        decision='allowed' if decision.allowed else 'denied',
        basis=decision.basis,
        deny_reason=decision.reason_code,
        deny_detail=decision.reason_text,
        path=path,
        ip_address=ip_address,
    )
    logger.info(
        f"Access {log.decision}: {user.username} -> goods {goods.code} "
        f"(basis={decision.basis or '-'}, grant={log.grant_no or '-'} v{log.grant_version or '-'})"
    )
    return log


@transaction.atomic
def create_grant(*, grantee, granted_by, reason, goods_ids, effective_from, effective_until):
    """创建临时授权(版本 1)"""
    grant = TemporaryGrant.objects.create(
        grant_no=generate_grant_no(),
        version=1,
        grantee=grantee,
        granted_by=granted_by,
        reason=reason,
        effective_from=effective_from,
        effective_until=effective_until,
    )
    TemporaryGrantScope.objects.bulk_create([
        TemporaryGrantScope(grant=grant, goods_id=goods_id)
        for goods_id in sorted(set(goods_ids))
    ])
    logger.info(f"Grant created: {grant.grant_no} v1 by {granted_by.username} for {grantee.username}")
    return grant


@transaction.atomic
def amend_grant(*, current, operator, reason, goods_ids, effective_from, effective_until):
    """变更授权: 旧版本标记被取代, 产生同组新版本"""
    now = timezone.now()
    TemporaryGrant.objects.filter(pk=current.pk).update(superseded_at=now)
    new_version = TemporaryGrant.objects.create(
        grant_no=current.grant_no,
        version=current.version + 1,
        grantee=current.grantee,
        granted_by=operator,
        reason=reason,
        effective_from=effective_from,
        effective_until=effective_until,
    )
    TemporaryGrantScope.objects.bulk_create([
        TemporaryGrantScope(grant=new_version, goods_id=goods_id)
        for goods_id in sorted(set(goods_ids))
    ])
    logger.info(f"Grant amended: {new_version.grant_no} v{new_version.version} by {operator.username}")
    return new_version


@transaction.atomic
def revoke_grant(*, grant, operator, revoke_reason):
    """撤销授权: 整个授权组(所有版本)立即失效"""
    now = timezone.now()
    TemporaryGrant.objects.filter(grant_no=grant.grant_no).update(
        revoked_at=now,
        revoked_by=operator,
        revoke_reason=revoke_reason,
    )
    logger.info(f"Grant revoked: {grant.grant_no} by {operator.username}")
