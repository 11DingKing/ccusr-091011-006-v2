"""
临时授权序列化器
"""
from django.utils import timezone
from rest_framework import serializers
from apps.authentication.models import User
from apps.warehouse.models import Goods
from .models import AccessAuditLog, TemporaryGrant


def _validate_window_and_scope(data):
    """校验生效区间与资源范围(创建与变更共用)"""
    if data['effective_until'] <= data['effective_from']:
        raise serializers.ValidationError('失效时间必须晚于生效时间')
    if data['effective_until'] <= timezone.now():
        raise serializers.ValidationError('授权失效时间必须晚于当前时间')

    goods_ids = list(dict.fromkeys(data['goods_ids']))
    goods_map = {g.id: g for g in Goods.objects.filter(id__in=goods_ids)}
    missing = [gid for gid in goods_ids if gid not in goods_map]
    if missing:
        raise serializers.ValidationError(f'物资不存在: {missing}')
    inactive = [goods_map[gid].name for gid in goods_ids if not goods_map[gid].is_active]
    if inactive:
        raise serializers.ValidationError(f'物资已停用: {inactive}')
    data['goods_ids'] = goods_ids
    return data


class TemporaryGrantCreateSerializer(serializers.Serializer):
    """临时授权创建序列化器"""
    grantee_id = serializers.IntegerField(required=True, error_messages={
        'required': '请选择被授权人',
        'invalid': '被授权人无效',
    })
    goods_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, required=True,
        error_messages={'required': '请选择授权物资范围', 'empty': '授权物资范围不能为空'}
    )
    effective_from = serializers.DateTimeField(required=True, error_messages={
        'required': '请填写生效时间',
        'invalid': '生效时间格式无效',
    })
    effective_until = serializers.DateTimeField(required=True, error_messages={
        'required': '请填写失效时间',
        'invalid': '失效时间格式无效',
    })
    reason = serializers.CharField(max_length=500, required=True, trim_whitespace=True, error_messages={
        'required': '必须填写授权依据',
        'blank': '必须填写授权依据',
    })

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError('必须填写授权依据')
        return value.strip()

    def validate(self, data):
        data = _validate_window_and_scope(data)
        grantee = User.objects.filter(pk=data['grantee_id'], is_active=True).first()
        if not grantee:
            raise serializers.ValidationError('被授权人不存在或已停用')
        if grantee.is_admin:
            raise serializers.ValidationError('管理员无需临时授权, 不能作为被授权人')
        data['grantee'] = grantee
        return data


class TemporaryGrantAmendSerializer(serializers.Serializer):
    """临时授权变更序列化器(被授权人不可变, 其余字段整体生效为新版本)"""
    goods_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, required=True,
        error_messages={'required': '请选择授权物资范围', 'empty': '授权物资范围不能为空'}
    )
    effective_from = serializers.DateTimeField(required=True, error_messages={
        'required': '请填写生效时间',
        'invalid': '生效时间格式无效',
    })
    effective_until = serializers.DateTimeField(required=True, error_messages={
        'required': '请填写失效时间',
        'invalid': '失效时间格式无效',
    })
    reason = serializers.CharField(max_length=500, required=True, trim_whitespace=True, error_messages={
        'required': '必须填写授权依据',
        'blank': '必须填写授权依据',
    })

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError('必须填写授权依据')
        return value.strip()

    def validate(self, data):
        return _validate_window_and_scope(data)


class TemporaryGrantRevokeSerializer(serializers.Serializer):
    """临时授权撤销序列化器"""
    revoke_reason = serializers.CharField(max_length=500, required=True, trim_whitespace=True, error_messages={
        'required': '必须填写撤销原因',
        'blank': '必须填写撤销原因',
    })

    def validate_revoke_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError('必须填写撤销原因')
        return value.strip()


class TemporaryGrantSerializer(serializers.ModelSerializer):
    """临时授权序列化器"""
    grantee_username = serializers.CharField(source='grantee.username', read_only=True)
    grantee_name = serializers.CharField(source='grantee.real_name', read_only=True)
    granted_by_username = serializers.CharField(source='granted_by.username', read_only=True)
    revoked_by_username = serializers.CharField(source='revoked_by.username', read_only=True, default='')
    status = serializers.CharField(read_only=True)
    status_display = serializers.CharField(read_only=True)
    is_current = serializers.BooleanField(read_only=True)
    goods_list = serializers.SerializerMethodField()

    class Meta:
        model = TemporaryGrant
        fields = [
            'id', 'grant_no', 'version', 'is_current',
            'grantee', 'grantee_username', 'grantee_name',
            'granted_by', 'granted_by_username', 'reason',
            'goods_list', 'effective_from', 'effective_until',
            'status', 'status_display',
            'superseded_at', 'revoked_at', 'revoked_by_username', 'revoke_reason',
            'created_at',
        ]

    def get_goods_list(self, obj):
        return [
            {'id': g.id, 'name': g.name, 'code': g.code}
            for g in obj.goods.all()
        ]


class TemporaryGrantDetailSerializer(TemporaryGrantSerializer):
    """临时授权详情序列化器 - 附带同组全部版本历史"""
    versions = serializers.SerializerMethodField()

    class Meta(TemporaryGrantSerializer.Meta):
        fields = TemporaryGrantSerializer.Meta.fields + ['versions']

    def get_versions(self, obj):
        versions = (
            TemporaryGrant.objects.filter(grant_no=obj.grant_no)
            .order_by('version')
        )
        return [
            {
                'id': v.id,
                'version': v.version,
                'status': v.status,
                'status_display': v.status_display,
                'effective_from': v.effective_from,
                'effective_until': v.effective_until,
                'reason': v.reason,
                'created_at': v.created_at,
            }
            for v in versions
        ]


class AccessAuditLogSerializer(serializers.ModelSerializer):
    """访问审计记录序列化器"""
    decision_display = serializers.CharField(source='get_decision_display', read_only=True)
    basis_display = serializers.SerializerMethodField()

    class Meta:
        model = AccessAuditLog
        fields = [
            'id', 'user', 'username',
            'goods', 'goods_code', 'goods_name',
            'grant', 'grant_no', 'grant_version',
            'decision', 'decision_display', 'basis', 'basis_display',
            'deny_reason', 'deny_detail',
            'path', 'ip_address', 'accessed_at',
        ]

    def get_basis_display(self, obj):
        return dict(AccessAuditLog.BASIS_CHOICES).get(obj.basis, '')
