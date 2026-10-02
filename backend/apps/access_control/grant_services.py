"""
临时授权的签发、换发与撤销

- issue_grant：首次签发，生成 v1；依据（类型/文号/说明）必填；
  生效区间必须落在预约时段内，资源范围必须是预约且在案的物资。
- reissue_grant：调整区间/范围/依据时换发，旧 active 版本置为 superseded，
  生成 v(n+1)；旧版本快照永久保留，撤销后新的请求立即按新版本判定。
- revoke_grant_version：提前撤销某版本，立即生效；已有审计记录不受影响。
- 同一 (授权, 版本号) 通过行级锁串行化，杜绝并发产生重复版本。
"""
from django.db import transaction
from django.utils import timezone
from .models import Appointment, Grant, GrantVersion


class GrantError(ValueError):
    """授权业务校验失败"""


def _validate_window(valid_from, valid_to, appointment=None):
    if valid_from >= valid_to:
        raise GrantError('生效时间必须早于失效时间')
    if appointment is not None:
        if valid_from < appointment.start_time or valid_to > appointment.end_time:
            raise GrantError('授权生效区间必须位于预约时段之内')


def _validate_scope(appointment, case, evidence_ids):
    if not evidence_ids:
        raise GrantError('资源范围不能为空')
    if len(evidence_ids) != len(set(evidence_ids)):
        raise GrantError('资源范围存在重复条目')
    # 范围必须属于该案件，且在预约清单内（外部人员只应看到预约指定的物资）
    valid_ids = set(
        appointment.evidences.filter(case=case).values_list('id', flat=True)
    ) if appointment else set()
    invalid = set(evidence_ids) - valid_ids
    if invalid:
        raise GrantError(f'资源范围包含未预约或不属于该案件的物资: {sorted(invalid)}')


def _validate_basis(basis_type, basis_ref, basis_reason):
    if not (basis_type or '').strip():
        raise GrantError('必须说明授权依据类型')
    if not (basis_ref or '').strip():
        raise GrantError('必须填写依据文号')
    if not (basis_reason or '').strip():
        raise GrantError('必须说明授权依据')


@transaction.atomic
def issue_grant(*, appraiser, appointment, basis_type, basis_ref, basis_reason,
                valid_from, valid_to, scope_evidence_ids, issued_by):
    """首次签发临时授权（v1）。重复授权：同一人员-案件允许多张授权并存。"""
    if appointment.appraiser_id != appraiser.id:
        raise GrantError('预约与被授权人不一致')
    if appointment.status != 'confirmed':
        raise GrantError('只能针对已确认的预约签发授权')
    case = appointment.case
    _validate_basis(basis_type, basis_ref, basis_reason)
    _validate_window(valid_from, valid_to, appointment)
    _validate_scope(appointment, case, scope_evidence_ids)

    grant = Grant.objects.create(
        appraiser=appraiser, appointment=appointment, case=case, created_by=issued_by
    )
    version = GrantVersion.objects.create(
        grant=grant,
        version_no=1,
        basis_type=basis_type.strip(),
        basis_ref=basis_ref.strip(),
        basis_reason=basis_reason.strip(),
        valid_from=valid_from,
        valid_to=valid_to,
        scope_evidence_ids=sorted(scope_evidence_ids),
        status='active',
        issued_by=issued_by,
    )
    return grant, version


@transaction.atomic
def reissue_grant(*, grant, basis_type, basis_ref, basis_reason,
                  valid_from, valid_to, scope_evidence_ids, issued_by):
    """换发新版本：旧 active 版本标记 superseded，生成下一版本快照。"""
    grant = Grant.objects.select_for_update().get(pk=grant.pk)
    current = grant.versions.order_by('-version_no').first()
    if current is None:
        raise GrantError('授权不存在任何版本')
    if current.status == 'revoked':
        raise GrantError('授权已撤销，不能换发；如需继续访问请重新签发授权')

    _validate_basis(basis_type, basis_ref, basis_reason)
    _validate_window(valid_from, valid_to, grant.appointment)
    _validate_scope(grant.appointment, grant.case, scope_evidence_ids)

    next_no = current.version_no + 1
    # 并发兜底：唯一约束 (grant, version_no)
    if grant.versions.filter(version_no=next_no).exists():
        raise GrantError('授权版本冲突，请重试')

    if current.status == 'active':
        current.status = 'superseded'
        current.save(update_fields=['status'])

    version = GrantVersion.objects.create(
        grant=grant,
        version_no=next_no,
        basis_type=basis_type.strip(),
        basis_ref=basis_ref.strip(),
        basis_reason=basis_reason.strip(),
        valid_from=valid_from,
        valid_to=valid_to,
        scope_evidence_ids=sorted(scope_evidence_ids),
        status='active',
        issued_by=issued_by,
    )
    return grant, version


@transaction.atomic
def revoke_grant_version(version, *, revoked_by, reason):
    """提前撤销授权版本，立即生效（之后新的访问请求按无有效授权拒绝）。"""
    version = GrantVersion.objects.select_for_update().get(pk=version.pk)
    if version.status != 'active':
        raise GrantError(f'版本当前状态为「{version.get_status_display()}」，无需撤销')
    if not (reason or '').strip():
        raise GrantError('撤销必须说明原因')
    version.status = 'revoked'
    version.revoked_by = revoked_by
    version.revoked_at = timezone.now()
    version.revoke_reason = reason.strip()
    version.save(update_fields=['status', 'revoked_by', 'revoked_at', 'revoke_reason'])
    return version


@transaction.atomic
def cancel_appointment(appointment, *, cancelled_by):
    """取消预约：同时立即撤销其名下所有 active 授权版本。"""
    appointment = type(appointment).objects.select_for_update().get(pk=appointment.pk)
    if appointment.status != 'confirmed':
        raise GrantError('仅已确认的预约可以取消')
    now = timezone.now()
    active_versions = GrantVersion.objects.filter(
        grant__appointment=appointment, status='active'
    ).select_for_update()
    for version in active_versions:
        version.status = 'revoked'
        version.revoked_by = cancelled_by
        version.revoked_at = now
        version.revoke_reason = '关联预约已取消，授权同步撤销'
        version.save(update_fields=['status', 'revoked_by', 'revoked_at', 'revoke_reason'])
    appointment.status = 'cancelled'
    appointment.save(update_fields=['status', 'updated_at'])
    return appointment
