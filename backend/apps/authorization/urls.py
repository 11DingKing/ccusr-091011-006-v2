"""
临时授权URL配置
"""
from django.urls import path
from .views import (
    AccessAuditLogDetailView,
    AccessAuditLogListView,
    AccessCheckView,
    CaseGoodsAccessView,
    TemporaryGrantAmendView,
    TemporaryGrantDetailView,
    TemporaryGrantListView,
    TemporaryGrantRevokeView,
)

urlpatterns = [
    path('grants/', TemporaryGrantListView.as_view(), name='grant-list'),
    path('grants/<int:pk>/', TemporaryGrantDetailView.as_view(), name='grant-detail'),
    path('grants/<int:pk>/amend/', TemporaryGrantAmendView.as_view(), name='grant-amend'),
    path('grants/<int:pk>/revoke/', TemporaryGrantRevokeView.as_view(), name='grant-revoke'),
    path('audit-logs/', AccessAuditLogListView.as_view(), name='audit-log-list'),
    path('audit-logs/<int:pk>/', AccessAuditLogDetailView.as_view(), name='audit-log-detail'),
    path('check/', AccessCheckView.as_view(), name='access-check'),
    path('case-goods/<int:goods_id>/', CaseGoodsAccessView.as_view(), name='case-goods-access'),
]
