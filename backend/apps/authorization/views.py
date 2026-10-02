"""
临时授权视图
"""
import logging
from django.utils import timezone
from rest_framework.fields import DateTimeField
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated
from apps.core.response import success_response, error_response
from apps.authentication.models import User
from apps.warehouse.models import Goods
from apps.warehouse.serializers import GoodsSerializer
from .models import AccessAuditLog, TemporaryGrant
from .serializers import (
    AccessAuditLogSerializer,
    TemporaryGrantAmendSerializer,
    TemporaryGrantCreateSerializer,
    TemporaryGrantDetailSerializer,
    TemporaryGrantRevokeSerializer,
    TemporaryGrantSerializer,
)
from .filters import AccessAuditLogFilter, TemporaryGrantFilter
from .services import (
    amend_grant, create_grant, evaluate_access,
    find_duplicate_grant, record_access, revoke_grant,
)

logger = logging.getLogger('apps')


def _first_error(errors):
    """提取首个校验错误信息"""
    first = list(errors.values())[0]
    if isinstance(first, (list, tuple)):
        return str(first[0])
    return str(first)


def _paginate(request, queryset):
    """与项目其余模块一致的分页方式"""
    page = int(request.query_params.get('page', 1))
    page_size = int(request.query_params.get('page_size', 10))
    start = (page - 1) * page_size
    end = start + page_size
    return queryset.count(), queryset[start:end], page, page_size


def _client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def _admin_required(request):
    if not request.user.is_admin:
        return error_response(message='无权限操作', code=403)
    return None


class TemporaryGrantListView(APIView):
    """临时授权列表与创建(仅管理员)"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        denied = _admin_required(request)
        if denied:
            return denied

        queryset = TemporaryGrant.objects.prefetch_related('goods')
        # 默认只展示当前版本, 历史版本通过详情接口查看
        if request.query_params.get('include_superseded') != 'true':
            queryset = queryset.filter(superseded_at__isnull=True)

        filterset = TemporaryGrantFilter(request.query_params, queryset=queryset)
        total, grants, page, page_size = _paginate(request, filterset.qs)

        return success_response(data={
            'list': TemporaryGrantSerializer(grants, many=True).data,
            'total': total,
            'page': page,
            'page_size': page_size
        })

    def post(self, request):
        denied = _admin_required(request)
        if denied:
            return denied

        serializer = TemporaryGrantCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        data = serializer.validated_data
        duplicate = find_duplicate_grant(
            data['grantee'], data['goods_ids'],
            data['effective_from'], data['effective_until'],
        )
        if duplicate:
            return error_response(
                message=f'已存在相同资源范围且时间重叠的授权({duplicate.grant_no}), 请勿重复授权'
            )

        grant = create_grant(
            grantee=data['grantee'],
            granted_by=request.user,
            reason=data['reason'],
            goods_ids=data['goods_ids'],
            effective_from=data['effective_from'],
            effective_until=data['effective_until'],
        )
        logger.info(f"User {request.user.username} created grant {grant.grant_no}")
        return success_response(
            data=TemporaryGrantSerializer(grant).data,
            message='授权创建成功'
        )


class TemporaryGrantDetailView(APIView):
    """临时授权详情(含同组版本历史, 仅管理员)"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        denied = _admin_required(request)
        if denied:
            return denied

        grant = TemporaryGrant.objects.prefetch_related('goods').filter(pk=pk).first()
        if not grant:
            return error_response(message='授权不存在', code=404)
        return success_response(data=TemporaryGrantDetailSerializer(grant).data)


class TemporaryGrantAmendView(APIView):
    """临时授权变更 - 产生新版本, 旧版本立即被取代(仅管理员)"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _admin_required(request)
        if denied:
            return denied

        current = TemporaryGrant.objects.filter(pk=pk).first()
        if not current:
            return error_response(message='授权不存在', code=404)
        if current.revoked_at is not None:
            return error_response(message='授权已撤销, 无法变更')
        if not current.is_current:
            return error_response(message='该版本已被取代, 请对当前版本操作')
        if current.status == 'expired':
            return error_response(message='授权已过期, 无法变更, 请重新创建授权')

        serializer = TemporaryGrantAmendSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        data = serializer.validated_data
        duplicate = find_duplicate_grant(
            current.grantee, data['goods_ids'],
            data['effective_from'], data['effective_until'],
            exclude_grant_no=current.grant_no,
        )
        if duplicate:
            return error_response(
                message=f'已存在相同资源范围且时间重叠的授权({duplicate.grant_no}), 请勿重复授权'
            )

        new_version = amend_grant(
            current=current,
            operator=request.user,
            reason=data['reason'],
            goods_ids=data['goods_ids'],
            effective_from=data['effective_from'],
            effective_until=data['effective_until'],
        )
        logger.info(f"User {request.user.username} amended grant {current.grant_no} to v{new_version.version}")
        return success_response(
            data=TemporaryGrantSerializer(new_version).data,
            message=f'授权已变更, 当前版本 v{new_version.version}'
        )


class TemporaryGrantRevokeView(APIView):
    """临时授权撤销 - 整个授权组立即失效(仅管理员)"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _admin_required(request)
        if denied:
            return denied

        grant = TemporaryGrant.objects.filter(pk=pk).first()
        if not grant:
            return error_response(message='授权不存在', code=404)
        if grant.revoked_at is not None:
            return error_response(message='该授权已被撤销, 请勿重复操作')
        if grant.status == 'expired':
            return error_response(message='授权已过期, 无需撤销')

        serializer = TemporaryGrantRevokeSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer.errors))

        revoke_grant(
            grant=grant,
            operator=request.user,
            revoke_reason=serializer.validated_data['revoke_reason'],
        )
        grant.refresh_from_db()
        logger.info(f"User {request.user.username} revoked grant {grant.grant_no}")
        return success_response(
            data=TemporaryGrantSerializer(grant).data,
            message='授权已撤销, 立即生效'
        )


class CaseGoodsAccessView(APIView):
    """
    鉴定查看通道 - 外部鉴定人员凭临时授权查看指定案件物资。
    每次访问(无论放行或拒绝)均形成不可删改的审计记录。
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, goods_id):
        goods = Goods.objects.filter(pk=goods_id).first()
        if not goods:
            return error_response(message='物资不存在', code=404)

        decision = evaluate_access(request.user, goods)
        audit_log = record_access(
            user=request.user,
            goods=goods,
            decision=decision,
            path=request.path,
            ip_address=_client_ip(request),
        )

        if not decision.allowed:
            return error_response(
                message=f'访问被拒绝: {decision.reason_text}',
                code=403,
                data={'audit_id': audit_log.id, 'deny_reason': decision.reason_code}
            )
        return success_response(
            data={
                'audit_id': audit_log.id,
                'basis': decision.basis,
                'grant_no': audit_log.grant_no or None,
                'grant_version': audit_log.grant_version,
                'goods': GoodsSerializer(goods).data,
            },
            message='访问成功'
        )


class AccessCheckView(APIView):
    """访问决策预检 - 安全管理员可查看指定用户/物资/时刻的判定结果及命中授权版本(不写审计)"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        denied = _admin_required(request)
        if denied:
            return denied

        user_id = request.query_params.get('user')
        goods_id = request.query_params.get('goods')
        if not user_id or not goods_id:
            return error_response(message='请提供 user 与 goods 参数')

        user = User.objects.filter(pk=user_id).first()
        if not user:
            return error_response(message='用户不存在', code=404)
        goods = Goods.objects.filter(pk=goods_id).first()
        if not goods:
            return error_response(message='物资不存在', code=404)

        at = timezone.now()
        at_param = request.query_params.get('at')
        if at_param:
            try:
                at = DateTimeField().to_internal_value(at_param)
            except Exception:
                return error_response(message='时间格式无效, 请使用 ISO 8601 格式')

        decision = evaluate_access(user, goods, at)
        grant = decision.grant
        return success_response(data={
            'allowed': decision.allowed,
            'basis': decision.basis or None,
            'grant': {
                'id': grant.id,
                'grant_no': grant.grant_no,
                'version': grant.version,
                'effective_from': grant.effective_from,
                'effective_until': grant.effective_until,
            } if grant else None,
            'reason_code': decision.reason_code or None,
            'reason_text': decision.reason_text or None,
            'checked_at': at,
        })


class AccessAuditLogListView(APIView):
    """访问审计记录列表(仅管理员)"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        denied = _admin_required(request)
        if denied:
            return denied

        filterset = AccessAuditLogFilter(
            request.query_params,
            queryset=AccessAuditLog.objects.select_related('user', 'goods', 'grant')
        )
        total, logs, page, page_size = _paginate(request, filterset.qs)

        return success_response(data={
            'list': AccessAuditLogSerializer(logs, many=True).data,
            'total': total,
            'page': page,
            'page_size': page_size
        })


class AccessAuditLogDetailView(APIView):
    """访问审计记录详情(仅管理员); 审计记录不可删除"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        denied = _admin_required(request)
        if denied:
            return denied

        log = AccessAuditLog.objects.filter(pk=pk).first()
        if not log:
            return error_response(message='审计记录不存在', code=404)
        return success_response(data=AccessAuditLogSerializer(log).data)

    def delete(self, request, pk):
        denied = _admin_required(request)
        if denied:
            return denied
        return error_response(message='审计记录不可删除', code=403)
