"""
临时授权判定服务

判定链条（全部基于同一时刻 now，保证一致语义）：
  账号角色 → 档案状态 → 预约（存在/已确认/时段内）→ 授权版本（active 且区间内）
  → 资源范围 → 案件归属。

跨午夜：预约与授权都使用绝对时间的半开区间 [start, end)，天然支持跨午夜。
重复授权 / 范围交叠：同一人员-案件可存在多张授权，版本之间允许区间与范围交叠；
判定时确定性地选择「版本号最大、其次主键最大」的有效覆盖版本，
任何一次放行都能回答“由哪个授权版本放行”。
"""
from dataclasses import dataclass
from django.db import transaction
from django.utils import timezone
from .models import (
    AccessAudit, Appointment, ExternalAppraiser, Grant, GrantVersion,
)


@dataclass
class AccessDecision:
    allowed: bool
    deny_reason: str = ''
    appointment: Appointment = None
    grant: Grant = None
    grant_version: GrantVersion = None


def find_appointments(appraiser, case, at):
    """返回 at 时段内覆盖该案件的全部已确认预约（跨午夜区间照常命中）"""
    return list(
        Appointment.objects.filter(
            appraiser=appraiser,
            case=case,
            status='confirmed',
            start_time__lte=at,
            end_time__gt=at,
        )
        .order_by('-start_time', '-id')
    )


def find_appointment(appraiser, case, at):
    """确定性选取一个时段内预约（优先序：开始时间最晚、主键最大）"""
    appointments = find_appointments(appraiser, case, at)
    return appointments[0] if appointments else None


def _effective_versions(appraiser, case, at, appointments=None):
    """
    该人员-案件在 at 时刻处于生效区间内的 active 授权版本，按确定性优先序排列。
    只考虑挂载在「当前时段内已确认预约」名下的授权，避免被其他预约的授权串用。
    """
    if appointments is None:
        appointments = find_appointments(appraiser, case, at)
    appointment_ids = [a.id for a in appointments]
    if not appointment_ids:
        return []
    return list(
        GrantVersion.objects.filter(
            grant__appraiser_id__in=appointment_ids,
            grant__case=case,
            status='active',
            valid_from__lte=at,
            valid_to__gt=at,
        )
        .select_related('grant')
        .order_by('-version_no', '-id')
    )


def evaluate(user, case, evidence, at=None):
    """
    评估某次访问尝试，返回 AccessDecision（不落审计）。

    user: 已认证的登录账号
    case/evidence: 试图访问的案件与在案物资
    """
    at = at or timezone.now()

    if getattr(user, 'role', None) != 'external':
        return AccessDecision(False, 'not_appraiser')

    appraiser = ExternalAppraiser.objects.filter(user=user).first()
    if appraiser is None or not appraiser.is_active:
        return AccessDecision(False, 'appraiser_inactive')

    appointments = find_appointments(appraiser, case, at)
    if not appointments:
        # 区分“完全没有预约”、“预约已取消”与“不在时段内”，审计更清晰
        all_appointments = list(
            Appointment.objects.filter(appraiser=appraiser, case=case)
        )
        if not all_appointments:
            return AccessDecision(False, 'no_appointment')
        if all(a.status == 'cancelled' for a in all_appointments):
            return AccessDecision(False, 'appointment_cancelled')
        return AccessDecision(False, 'appointment_outside_window')

    appointment = appointments[0]
    grants = list(Grant.objects.filter(appointment__in=appointments))
    if not grants:
        return AccessDecision(False, 'no_active_grant', appointment=appointment)

    versions = _effective_versions(appraiser, case, at, appointments=appointments)
    if not versions:
        any_revoked = GrantVersion.objects.filter(
            grant_id__in=[g.id for g in grants], status='revoked'
        ).exists()
        reason = 'grant_revoked' if any_revoked else 'grant_outside_window'
        return AccessDecision(False, reason, appointment=appointment)

    # 交叠授权：优先序最高的有效版本用于归因；范围不覆盖时继续找覆盖的版本
    latest = versions[0]
    chosen = next((v for v in versions if v.covers_evidence(evidence.id)), None)
    if chosen is None:
        return AccessDecision(
            False, 'out_of_scope',
            appointment=_appointment_of(appointments, latest.grant.appointment_id),
            grant=latest.grant, grant_version=latest,
        )

    if evidence.case_id != case.id:
        return AccessDecision(
            False, 'evidence_not_in_case',
            appointment=_appointment_of(appointments, chosen.grant.appointment_id),
            grant=chosen.grant, grant_version=chosen,
        )

    return AccessDecision(
        True,
        appointment=_appointment_of(appointments, chosen.grant.appointment_id),
        grant=chosen.grant, grant_version=chosen,
    )


def _appointment_of(appointments, appointment_id):
    return next((a for a in appointments if a.id == appointment_id), appointments[0])


@transaction.atomic
def record_audit(user, case, evidence, request=None, at=None, decision=None):
    """落一条只追加审计记录；返回 (decision, audit)。"""
    at = at or timezone.now()
    if decision is None:
        decision = evaluate(user, case, evidence, at=at)

    appraiser = ExternalAppraiser.objects.filter(user=user).first()
    ip = None
    user_agent = ''
    if request is not None:
        xff = request.META.get('HTTP_X_FORWARDED_FOR')
        ip = xff.split(',')[0].strip() if xff else request.META.get('REMOTE_ADDR')
        user_agent = request.headers.get('User-Agent', '')[:500]

    audit = AccessAudit.objects.create(
        appraiser=appraiser,
        appraiser_name=appraiser.name if appraiser else (getattr(user, 'username', '') or ''),
        case=case,
        evidence=evidence,
        appointment=decision.appointment,
        grant=decision.grant,
        grant_version=decision.grant_version,
        grant_version_no=decision.grant_version.version_no if decision.grant_version else None,
        result='allowed' if decision.allowed else 'denied',
        deny_reason='' if decision.allowed else decision.deny_reason,
        accessed_at=at,
        ip_address=ip,
        user_agent=user_agent,
    )
    return decision, audit


def check_access(user, case, evidence, request=None, at=None):
    """完整入口：评估并写审计，返回 (allowed, decision, audit)。"""
    at = at or timezone.now()
    decision = evaluate(user, case, evidence, at=at)
    _, audit = record_audit(user, case, evidence, request=request, at=at, decision=decision)
    return decision.allowed, decision, audit
