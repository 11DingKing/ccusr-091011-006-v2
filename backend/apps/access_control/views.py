"""
访问控制视图

管理端（安全管理员）：
  案件 / 在案物资 / 外部鉴定人员档案 / 预约 / 临时授权的签发、换发、撤销 /
  审计台账只读查询（不提供任何修改、删除入口）。

外部端（外部鉴定人员）：
  仅能通过访问判定入口查看授权范围内的在案物资，每次尝试（含拒绝）均写审计。
"""
import logging
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from apps.core.response import success_response, error_response
from apps.warehouse.models import Goods
from .models import (
    AccessAudit, Appointment, Case, CaseEvidence,
    ExternalAppraiser, Grant, GrantVersion,
)
from .serializers import (
    AccessAuditSerializer, AppointmentCreateSerializer, AppointmentSerializer,
    CaseCreateSerializer, CaseEvidenceAddSerializer, CaseEvidenceSerializer,
    CaseSerializer, ExternalAppraiserSerializer, GrantIssueSerializer,
    GrantReissueSerializer, GrantRevokeSerializer, GrantSerializer, GrantVersionSerializer,
)
from .grant_services import (
    GrantError, cancel_appointment, issue_grant, reissue_grant, revoke_grant_version,
)
from .services import check_access

logger = logging.getLogger('apps')


def _is_admin(user):
    return bool(user.is_authenticated and user.is_admin)


def _first_error(serializer):
    errors = serializer.errors
    first = list(errors.values())[0]
    if isinstance(first, list):
        first = first[0]
    return str(first)


def _paginate(request, queryset):
    page = max(int(request.query_params.get('page', 1)), 1)
    page_size = max(min(int(request.query_params.get('page_size', 10)), 200), 1)
    total = queryset.count()
    return queryset[(page - 1) * page_size:page * page_size], total, page, page_size


# ==================== 案件与在案物资（管理端） ====================

class CaseListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        queryset = Case.objects.all().order_by('-created_at')
        keyword = request.query_params.get('keyword')
        if keyword:
            queryset = queryset.filter(case_no__icontains=keyword) | \
                       queryset.filter(name__icontains=keyword)
        items, total, page, page_size = _paginate(request, queryset)
        return success_response(data={
            'list': CaseSerializer(items, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        if not _is_admin(request.user):
            return error_response(message='无权限操作', code=403)
        serializer = CaseCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        case = Case.objects.create(
            case_no=serializer.validated_data['case_no'],
            name=serializer.validated_data['name'],
            created_by=request.user,
        )
        logger.info("admin %s created case %s", request.user.username, case.case_no)
        return success_response(data=CaseSerializer(case).data, message='创建成功')


class CaseDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        case = Case.objects.filter(pk=pk).first()
        if not case:
            return error_response(message='案件不存在', code=404)
        return success_response(data=CaseSerializer(case).data)


class CaseEvidenceListView(APIView):
    """案件下在案物资的查询与登记"""
    permission_classes = [IsAuthenticated]

    def get(self, request, case_pk):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        case = Case.objects.filter(pk=case_pk).first()
        if not case:
            return error_response(message='案件不存在', code=404)
        items, total, page, page_size = _paginate(
            request, case.evidences.select_related('goods').order_by('-created_at')
        )
        return success_response(data={
            'list': CaseEvidenceSerializer(items, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request, case_pk):
        if not _is_admin(request.user):
            return error_response(message='无权限操作', code=403)
        case = Case.objects.filter(pk=case_pk).first()
        if not case:
            return error_response(message='案件不存在', code=404)
        serializer = CaseEvidenceAddSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        goods_ids = serializer.validated_data['goods_ids']
        existing = set(
            CaseEvidence.objects.filter(case=case, goods_id__in=goods_ids)
            .values_list('goods_id', flat=True)
        )
        goods = Goods.objects.filter(id__in=goods_ids)
        if goods.count() != len(set(goods_ids)):
            return error_response(message='存在不存在的物资')
        created = []
        for g in goods:
            if g.id in existing:
                continue
            created.append(CaseEvidence.objects.create(
                case=case, goods=g,
                registered_by=request.user,
                remark=serializer.validated_data.get('remark', ''),
            ))
        return success_response(
            data=CaseEvidenceSerializer(created, many=True).data,
            message=f'登记成功 {len(created)} 项（已在案 {len(existing)} 项跳过）',
        )


# ==================== 外部鉴定人员档案（管理端） ====================

class ExternalAppraiserListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        queryset = ExternalAppraiser.objects.select_related('user').all().order_by('-created_at')
        keyword = request.query_params.get('keyword')
        if keyword:
            queryset = queryset.filter(name__icontains=keyword) | \
                       queryset.filter(organization__icontains=keyword)
        items, total, page, page_size = _paginate(request, queryset)
        return success_response(data={
            'list': ExternalAppraiserSerializer(items, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        if not _is_admin(request.user):
            return error_response(message='无权限操作', code=403)
        serializer = ExternalAppraiserSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        appraiser = serializer.save()
        logger.info("admin %s registered appraiser %s", request.user.username, appraiser.name)
        return success_response(data=ExternalAppraiserSerializer(appraiser).data, message='登记成功')


class ExternalAppraiserDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        appraiser = ExternalAppraiser.objects.filter(pk=pk).first()
        if not appraiser:
            return error_response(message='鉴定人员不存在', code=404)
        return success_response(data=ExternalAppraiserSerializer(appraiser).data)

    def put(self, request, pk):
        if not _is_admin(request.user):
            return error_response(message='无权限操作', code=403)
        appraiser = ExternalAppraiser.objects.filter(pk=pk).first()
        if not appraiser:
            return error_response(message='鉴定人员不存在', code=404)
        serializer = ExternalAppraiserSerializer(appraiser, data=request.data, partial=True)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        serializer.save()
        return success_response(data=serializer.data, message='更新成功')


# ==================== 预约（管理端） ====================

class AppointmentListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        queryset = Appointment.objects.select_related('appraiser', 'case') \
            .prefetch_related('evidences__goods').all().order_by('-start_time')
        for field, param in (('appraiser_id', 'appraiser'), ('case_id', 'case'), ('status', 'status')):
            value = request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})
        items, total, page, page_size = _paginate(request, queryset)
        return success_response(data={
            'list': AppointmentSerializer(items, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        if not _is_admin(request.user):
            return error_response(message='无权限操作', code=403)
        serializer = AppointmentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data
        appointment = Appointment.objects.create(
            appraiser=data['appraiser'], case=data['case'],
            start_time=data['start_time'], end_time=data['end_time'],
            created_by=request.user,
        )
        appointment.evidences.set(
            CaseEvidence.objects.filter(
                case=data['case'], id__in=data['evidence_ids']
            )
        )
        logger.info(
            "admin %s created appointment %s", request.user.username, appointment
        )
        return success_response(data=AppointmentSerializer(appointment).data, message='预约成功')


class AppointmentCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        if not _is_admin(request.user):
            return error_response(message='无权限操作', code=403)
        appointment = Appointment.objects.filter(pk=pk).first()
        if not appointment:
            return error_response(message='预约不存在', code=404)
        try:
            cancel_appointment(appointment, cancelled_by=request.user)
        except GrantError as exc:
            return error_response(message=str(exc))
        return success_response(message='预约已取消，关联授权同步撤销')


# ==================== 临时授权（管理端） ====================

class GrantListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        queryset = Grant.objects.select_related(
            'appraiser', 'case', 'appointment'
        ).prefetch_related('versions').all().order_by('-created_at')
        for field, param in (('appraiser_id', 'appraiser'), ('case_id', 'case')):
            value = request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})
        items, total, page, page_size = _paginate(request, queryset)
        data = []
        for grant in items:
            row = GrantSerializer(grant).data
            row['current_version'] = GrantVersionSerializer(grant.current_version).data
            data.append(row)
        return success_response(data={
            'list': data, 'total': total, 'page': page, 'page_size': page_size,
        })

    def post(self, request):
        """签发临时授权（v1），必须说明依据。"""
        if not _is_admin(request.user):
            return error_response(message='无权限操作', code=403)
        serializer = GrantIssueSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data
        try:
            grant, version = issue_grant(
                appraiser=data['appointment'].appraiser,
                appointment=data['appointment'],
                basis_type=data['basis_type'],
                basis_ref=data['basis_ref'],
                basis_reason=data['basis_reason'],
                valid_from=data['valid_from'],
                valid_to=data['valid_to'],
                scope_evidence_ids=data['scope_evidence_ids'],
                issued_by=request.user,
            )
        except GrantError as exc:
            return error_response(message=str(exc))
        logger.info(
            "admin %s issued grant %s v%s", request.user.username, grant.id, version.version_no
        )
        return success_response(data=GrantSerializer(grant).data, message='授权签发成功')


class GrantDetailView(APIView):
    """授权详情：含全部历史版本，安全管理员可看清每次访问的归因版本"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        grant = Grant.objects.select_related('appraiser', 'case', 'appointment') \
            .filter(pk=pk).first()
        if not grant:
            return error_response(message='授权不存在', code=404)
        versions = grant.versions.order_by('-version_no')
        data = GrantSerializer(grant).data
        data['versions'] = GrantVersionSerializer(versions, many=True).data
        return success_response(data=data)


class GrantReissueView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        """换发新版本（旧版本 superseded 留痕）。"""
        if not _is_admin(request.user):
            return error_response(message='无权限操作', code=403)
        grant = Grant.objects.select_related('appointment', 'appointment__case', 'case') \
            .filter(pk=pk).first()
        if not grant:
            return error_response(message='授权不存在', code=404)
        serializer = GrantReissueSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        data = serializer.validated_data
        try:
            _, version = reissue_grant(
                grant=grant,
                basis_type=data['basis_type'],
                basis_ref=data['basis_ref'],
                basis_reason=data['basis_reason'],
                valid_from=data['valid_from'],
                valid_to=data['valid_to'],
                scope_evidence_ids=data['scope_evidence_ids'],
                issued_by=request.user,
            )
        except GrantError as exc:
            return error_response(message=str(exc))
        logger.info(
            "admin %s reissued grant %s v%s", request.user.username, grant.id, version.version_no
        )
        return success_response(data=GrantVersionSerializer(version).data, message='换发成功')


class GrantVersionRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, grant_pk, version_no):
        """提前撤销授权版本，撤销后新的访问请求立即失效。"""
        if not _is_admin(request.user):
            return error_response(message='无权限操作', code=403)
        version = GrantVersion.objects.filter(
            grant_id=grant_pk, version_no=version_no
        ).first()
        if not version:
            return error_response(message='授权版本不存在', code=404)
        serializer = GrantRevokeSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))
        try:
            revoke_grant_version(version, revoked_by=request.user,
                                 reason=serializer.validated_data['reason'])
        except GrantError as exc:
            return error_response(message=str(exc))
        logger.warning(
            "admin %s revoked grant %s v%s", request.user.username, grant_pk, version_no
        )
        return success_response(data=GrantVersionSerializer(version).data, message='授权已撤销')


# ==================== 审计台账（管理端只读） ====================

class AccessAuditListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        queryset = AccessAudit.objects.select_related(
            'appraiser', 'case', 'evidence__goods', 'grant_version'
        ).all().order_by('-accessed_at')
        for field, param in (
            ('appraiser_id', 'appraiser'), ('case_id', 'case'),
            ('result', 'result'), ('deny_reason', 'deny_reason'),
            ('grant_id', 'grant'), ('grant_version_no', 'version_no'),
        ):
            value = request.query_params.get(param)
            if value:
                queryset = queryset.filter(**{field: value})
        items, total, page, page_size = _paginate(request, queryset)
        return success_response(data={
            'list': AccessAuditSerializer(items, many=True).data,
            'total': total, 'page': page, 'page_size': page_size,
        })


class AccessAuditDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        if not _is_admin(request.user):
            return error_response(message='无权限访问', code=403)
        audit = AccessAudit.objects.select_related(
            'appraiser', 'case', 'evidence__goods', 'grant_version'
        ).filter(pk=pk).first()
        if not audit:
            return error_response(message='审计记录不存在', code=404)
        return success_response(data=AccessAuditSerializer(audit).data)


# ==================== 外部鉴定人员访问入口 ====================

class _ExternalOnlyMixin:
    def check_external(self, request):
        if request.user.role != 'external':
            return error_response(message='该入口仅限外部鉴定人员使用', code=403)
        profile = ExternalAppraiser.objects.filter(user=request.user).first()
        if profile is None or not profile.is_active:
            return error_response(message='外部鉴定人员档案不存在或已停用', code=403)
        return profile


class ExternalEvidenceAccessView(_ExternalOnlyMixin, APIView):
    """外部人员查看某项在案物资：逐次判定并写审计。"""
    permission_classes = [IsAuthenticated]

    def get(self, request, evidence_pk):
        profile = self.check_external(request)
        if hasattr(profile, 'status_code'):
            return profile
        evidence = CaseEvidence.objects.select_related('goods', 'case') \
            .filter(pk=evidence_pk).first()
        if not evidence:
            return error_response(message='在案物资不存在', code=404)

        allowed, decision, audit = check_access(
            request.user, evidence.case, evidence, request=request
        )
        if not allowed:
            logger.warning(
                "external %s denied evidence %s: %s (audit %s)",
                request.user.username, evidence_pk, decision.deny_reason, audit.id,
            )
            return error_response(
                message=f'访问被拒绝：{dict(AccessAudit.DENY_REASON_CHOICES).get(decision.deny_reason)}',
                code=403,
                data={'audit_id': audit.id, 'deny_reason': decision.deny_reason},
            )

        goods = evidence.goods
        return success_response(data={
            'audit_id': audit.id,
            'authorized_by': {
                'grant_id': decision.grant.id,
                'grant_version_pk': decision.grant_version.id,
                'grant_version_no': decision.grant_version.version_no,
                'basis_type': decision.grant_version.basis_type,
                'basis_ref': decision.grant_version.basis_ref,
                'valid_from': decision.grant_version.valid_from,
                'valid_to': decision.grant_version.valid_to,
            },
            'case': {'id': evidence.case_id, 'case_no': evidence.case.case_no},
            'goods': {
                'id': goods.id, 'name': goods.name, 'code': goods.code,
                'specification': goods.specification, 'remark': goods.remark,
            },
        }, message='访问已记录')


class ExternalAccessibleListView(_ExternalOnlyMixin, APIView):
    """外部人员查看自己当前时刻可访问的案件物资清单（不落审计）。"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        profile = self.check_external(request)
        if hasattr(profile, 'status_code'):
            return profile
        from django.utils import timezone
        from .services import _effective_versions
        at = timezone.now()

        result = []
        appointments = Appointment.objects.filter(
            appraiser=profile, status='confirmed',
            start_time__lte=at, end_time__gt=at,
        ).prefetch_related('evidences__goods', 'case')
        for appointment in appointments:
            versions = _effective_versions(profile, appointment.case, at)
            accessible_ids = set()
            for version in versions:
                accessible_ids.update(version.scope_evidence_ids or [])
            for evidence in appointment.evidences.all():
                if evidence.id in accessible_ids:
                    result.append({
                        'case_id': appointment.case_id,
                        'case_no': appointment.case.case_no,
                        'evidence_id': evidence.id,
                        'goods_id': evidence.goods_id,
                        'goods_name': evidence.goods.name,
                        'goods_code': evidence.goods.code,
                        'appointment_end': appointment.end_time,
                    })
        return success_response(data={'list': result, 'total': len(result)})
