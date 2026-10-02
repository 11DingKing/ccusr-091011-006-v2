"""
临时授权过滤器 - 使用 django-filter
"""
import django_filters
from django.utils import timezone
from .models import AccessAuditLog, TemporaryGrant


class TemporaryGrantFilter(django_filters.FilterSet):
    """临时授权过滤器"""
    grantee = django_filters.NumberFilter(field_name='grantee_id')
    grant_no = django_filters.CharFilter(field_name='grant_no', lookup_expr='exact')
    status = django_filters.CharFilter(method='filter_status')

    class Meta:
        model = TemporaryGrant
        fields = ['grantee', 'grant_no', 'status']

    def filter_status(self, queryset, name, value):
        now = timezone.now()
        current = queryset.filter(superseded_at__isnull=True)
        if value == 'revoked':
            return queryset.filter(revoked_at__isnull=False)
        if value == 'superseded':
            return queryset.filter(revoked_at__isnull=True, superseded_at__isnull=False)
        if value == 'expired':
            return current.filter(revoked_at__isnull=True, effective_until__lte=now)
        if value == 'pending':
            return current.filter(revoked_at__isnull=True, effective_from__gt=now)
        if value == 'active':
            return current.filter(
                revoked_at__isnull=True,
                effective_from__lte=now,
                effective_until__gt=now,
            )
        return queryset


class AccessAuditLogFilter(django_filters.FilterSet):
    """访问审计记录过滤器"""
    user = django_filters.NumberFilter(field_name='user_id')
    username = django_filters.CharFilter(field_name='username', lookup_expr='icontains')
    goods = django_filters.NumberFilter(field_name='goods_id')
    grant_no = django_filters.CharFilter(field_name='grant_no', lookup_expr='exact')
    decision = django_filters.CharFilter(field_name='decision', lookup_expr='exact')
    start_date = django_filters.DateFilter(field_name='accessed_at', lookup_expr='date__gte')
    end_date = django_filters.DateFilter(field_name='accessed_at', lookup_expr='date__lte')

    class Meta:
        model = AccessAuditLog
        fields = ['user', 'username', 'goods', 'grant_no', 'decision', 'start_date', 'end_date']
