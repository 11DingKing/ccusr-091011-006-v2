"""
访问控制序列化器
"""
from rest_framework import serializers
from django.utils import timezone
from .models import (
    AccessAudit, Appointment, Case, CaseEvidence,
    ExternalAppraiser, Grant, GrantVersion,
)


class AwareDateTimeField(serializers.DateTimeField):
    """无时区输入按系统时区（Asia/Shanghai）解释，保证跨午夜判定一致"""

    def to_internal_value(self, value):
        dt = super().to_internal_value(value)
        if timezone.is_naive(dt):
            dt = timezone.make_aware(dt, timezone.get_current_timezone())
        return dt


class CaseSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    evidence_count = serializers.SerializerMethodField()

    class Meta:
        model = Case
        fields = [
            'id', 'case_no', 'name', 'is_active', 'created_by', 'created_by_name',
            'created_at', 'updated_at', 'evidence_count',
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']

    def get_evidence_count(self, obj):
        return obj.evidences.count()


class CaseCreateSerializer(serializers.Serializer):
    case_no = serializers.CharField(max_length=64, required=True)
    name = serializers.CharField(max_length=200, required=True)

    def validate_case_no(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError('案件编号不能为空')
        if Case.objects.filter(case_no=value).exists():
            raise serializers.ValidationError('案件编号已存在')
        return value


class CaseEvidenceSerializer(serializers.ModelSerializer):
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    goods_code = serializers.CharField(source='goods.code', read_only=True)
    goods_specification = serializers.CharField(source='goods.specification', read_only=True)
    case_no = serializers.CharField(source='case.case_no', read_only=True)

    class Meta:
        model = CaseEvidence
        fields = [
            'id', 'case', 'case_no', 'goods', 'goods_name', 'goods_code',
            'goods_specification', 'remark', 'registered_by', 'created_at',
        ]
        read_only_fields = ['id', 'registered_by', 'created_at']


class CaseEvidenceAddSerializer(serializers.Serializer):
    goods_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, required=True
    )
    remark = serializers.CharField(max_length=200, required=False, default='')


class ExternalAppraiserSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source='user.username', read_only=True)
    user_id = serializers.IntegerField(source='user.id', read_only=True)

    class Meta:
        model = ExternalAppraiser
        fields = [
            'id', 'user', 'user_id', 'username', 'name', 'id_card',
            'organization', 'phone', 'is_active', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
        extra_kwargs = {'user': {'required': True, 'write_only': True}}

    def validate_user(self, user):
        if user.role != 'external':
            raise serializers.ValidationError('关联账号必须是外部鉴定人员角色')
        return user

    def validate_id_card(self, value):
        qs = ExternalAppraiser.objects.filter(id_card=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError('该身份证号已登记')
        return value


class AppointmentSerializer(serializers.ModelSerializer):
    appraiser_name = serializers.CharField(source='appraiser.name', read_only=True)
    case_no = serializers.CharField(source='case.case_no', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    evidence_ids = serializers.SerializerMethodField()
    evidences_detail = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = [
            'id', 'appraiser', 'appraiser_name', 'case', 'case_no',
            'evidences', 'evidence_ids', 'evidences_detail',
            'start_time', 'end_time', 'status', 'status_display',
            'created_by', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'status', 'created_by', 'created_at', 'updated_at']

    def get_evidence_ids(self, obj):
        return list(obj.evidences.values_list('id', flat=True))

    def get_evidences_detail(self, obj):
        return [
            {
                'id': e.id,
                'goods_id': e.goods_id,
                'goods_name': e.goods.name,
                'goods_code': e.goods.code,
            }
            for e in obj.evidences.select_related('goods')
        ]


class AppointmentCreateSerializer(serializers.Serializer):
    appraiser = serializers.PrimaryKeyRelatedField(
        queryset=ExternalAppraiser.objects.filter(is_active=True)
    )
    case = serializers.PrimaryKeyRelatedField(queryset=Case.objects.filter(is_active=True))
    evidence_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, required=True
    )
    start_time = AwareDateTimeField(required=True)
    end_time = AwareDateTimeField(required=True)

    def validate(self, data):
        if data['start_time'] >= data['end_time']:
            raise serializers.ValidationError('预约开始时间必须早于结束时间')
        valid_ids = set(
            CaseEvidence.objects.filter(case=data['case']).values_list('id', flat=True)
        )
        invalid = set(data['evidence_ids']) - valid_ids
        if invalid:
            raise serializers.ValidationError(
                f'物资不属于该案件或未在案登记: {sorted(invalid)}'
            )
        if len(data['evidence_ids']) != len(set(data['evidence_ids'])):
            raise serializers.ValidationError('预约物资存在重复条目')
        return data


class GrantVersionSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    issued_by_name = serializers.CharField(source='issued_by.username', read_only=True)
    revoked_by_name = serializers.CharField(source='revoked_by.username', read_only=True)
    is_effective_now = serializers.SerializerMethodField()

    class Meta:
        model = GrantVersion
        fields = [
            'id', 'grant', 'version_no', 'basis_type', 'basis_ref', 'basis_reason',
            'valid_from', 'valid_to', 'scope_evidence_ids', 'status', 'status_display',
            'issued_by', 'issued_by_name', 'issued_at',
            'revoked_by', 'revoked_by_name', 'revoked_at', 'revoke_reason',
            'is_effective_now',
        ]
        read_only_fields = fields

    def get_is_effective_now(self, obj):
        return obj.is_effective_at(timezone.now())


class GrantSerializer(serializers.ModelSerializer):
    appraiser_name = serializers.CharField(source='appraiser.name', read_only=True)
    case_no = serializers.CharField(source='case.case_no', read_only=True)
    current_version = GrantVersionSerializer(read_only=True)
    appointment_info = serializers.SerializerMethodField()

    class Meta:
        model = Grant
        fields = [
            'id', 'appraiser', 'appraiser_name', 'appointment', 'appointment_info',
            'case', 'case_no', 'created_by', 'created_at', 'current_version',
        ]
        read_only_fields = ['id', 'created_by', 'created_at']

    def get_appointment_info(self, obj):
        return {
            'id': obj.appointment_id,
            'start_time': obj.appointment.start_time,
            'end_time': obj.appointment.end_time,
        }


class GrantIssueSerializer(serializers.Serializer):
    appointment = serializers.PrimaryKeyRelatedField(
        queryset=Appointment.objects.filter(status='confirmed')
    )
    basis_type = serializers.CharField(max_length=50, required=True)
    basis_ref = serializers.CharField(max_length=200, required=True)
    basis_reason = serializers.CharField(required=True)
    valid_from = AwareDateTimeField(required=True)
    valid_to = AwareDateTimeField(required=True)
    scope_evidence_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, required=True
    )


class GrantReissueSerializer(serializers.Serializer):
    """换发请求：预约取自授权本身，不允许更改"""
    basis_type = serializers.CharField(max_length=50, required=True)
    basis_ref = serializers.CharField(max_length=200, required=True)
    basis_reason = serializers.CharField(required=True)
    valid_from = AwareDateTimeField(required=True)
    valid_to = AwareDateTimeField(required=True)
    scope_evidence_ids = serializers.ListField(
        child=serializers.IntegerField(), allow_empty=False, required=True
    )


class GrantRevokeSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError('撤销原因不能为空')
        return value


class AccessAuditSerializer(serializers.ModelSerializer):
    result_display = serializers.CharField(source='get_result_display', read_only=True)
    deny_reason_display = serializers.CharField(source='get_deny_reason_display', read_only=True)
    appraiser_name = serializers.CharField(read_only=True)
    case_no = serializers.CharField(source='case.case_no', read_only=True)
    goods_code = serializers.CharField(source='evidence.goods.code', read_only=True)
    goods_name = serializers.CharField(source='evidence.goods.name', read_only=True)
    grant_id = serializers.IntegerField(source='grant.id', read_only=True)
    grant_version_pk = serializers.IntegerField(source='grant_version.id', read_only=True)

    class Meta:
        model = AccessAudit
        fields = [
            'id', 'appraiser', 'appraiser_name', 'case', 'case_no',
            'evidence', 'goods_code', 'goods_name',
            'appointment', 'grant_id', 'grant_version_pk', 'grant_version_no',
            'result', 'result_display', 'deny_reason', 'deny_reason_display',
            'accessed_at', 'ip_address', 'user_agent',
        ]
        read_only_fields = fields
