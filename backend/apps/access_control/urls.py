"""
访问控制URL配置
"""
from django.urls import path
from .views import (
    AccessAuditDetailView, AccessAuditListView,
    AppointmentCancelView, AppointmentListView,
    CaseDetailView, CaseEvidenceListView, CaseListView,
    ExternalAccessibleListView, ExternalAppraiserDetailView,
    ExternalAppraiserListView, ExternalEvidenceAccessView,
    GrantDetailView, GrantListView, GrantReissueView, GrantVersionRevokeView,
)

urlpatterns = [
    # 案件与在案物资
    path('cases/', CaseListView.as_view(), name='case-list'),
    path('cases/<int:pk>/', CaseDetailView.as_view(), name='case-detail'),
    path('cases/<int:case_pk>/evidences/', CaseEvidenceListView.as_view(), name='case-evidence-list'),

    # 外部鉴定人员档案
    path('appraisers/', ExternalAppraiserListView.as_view(), name='appraiser-list'),
    path('appraisers/<int:pk>/', ExternalAppraiserDetailView.as_view(), name='appraiser-detail'),

    # 预约
    path('appointments/', AppointmentListView.as_view(), name='appointment-list'),
    path('appointments/<int:pk>/cancel/', AppointmentCancelView.as_view(), name='appointment-cancel'),

    # 临时授权
    path('grants/', GrantListView.as_view(), name='grant-list'),
    path('grants/<int:pk>/', GrantDetailView.as_view(), name='grant-detail'),
    path('grants/<int:pk>/reissue/', GrantReissueView.as_view(), name='grant-reissue'),
    path('grants/<int:grant_pk>/versions/<int:version_no>/revoke/',
         GrantVersionRevokeView.as_view(), name='grant-version-revoke'),

    # 审计台账（只读）
    path('access-audits/', AccessAuditListView.as_view(), name='access-audit-list'),
    path('access-audits/<int:pk>/', AccessAuditDetailView.as_view(), name='access-audit-detail'),

    # 外部鉴定人员入口
    path('external/evidences/<int:evidence_pk>/',
         ExternalEvidenceAccessView.as_view(), name='external-evidence-access'),
    path('external/accessible/',
         ExternalAccessibleListView.as_view(), name='external-accessible-list'),
]
