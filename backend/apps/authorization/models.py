"""
临时授权模型

设计约定:
- 生效区间为左闭右开 [effective_from, effective_until): 到达失效时刻即不可访问,
  相邻区间(一个的失效时刻等于另一个的生效时刻)不相交; 跨午夜时段只是普通区间。
- 授权按版本管理: 同一授权组的各版本共享 grant_no, version 从 1 开始递增;
  变更(amend)产生新版本并将旧版本标记 superseded_at, 撤销(revoke)作用于整个授权组。
- 访问审计记录一经形成不可修改、不可删除(ORM 层强制)。
"""
from django.db import models
from django.utils import timezone
from apps.authentication.models import User
from apps.warehouse.models import Goods


class ImmutableAuditLogError(Exception):
    """审计记录不可变异常 - 审计记录一经形成不允许修改或删除"""
    pass


class TemporaryGrant(models.Model):
    """临时授权模型 - 每一行代表授权的一个版本"""

    STATUS_CHOICES = [
        ('pending', '未生效'),
        ('active', '生效中'),
        ('expired', '已过期'),
        ('revoked', '已撤销'),
        ('superseded', '已被取代'),
    ]
    STATUS_DISPLAY_MAP = dict(STATUS_CHOICES)

    grant_no = models.CharField('授权编号', max_length=30, db_index=True)
    version = models.PositiveIntegerField('版本号', default=1)
    grantee = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='temporary_grants', verbose_name='被授权人'
    )
    granted_by = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='issued_temporary_grants', verbose_name='授权人'
    )
    reason = models.TextField('授权依据')
    goods = models.ManyToManyField(
        Goods, through='TemporaryGrantScope',
        related_name='temporary_grants', verbose_name='授权物资范围'
    )
    effective_from = models.DateTimeField('生效时间')
    effective_until = models.DateTimeField('失效时间')
    superseded_at = models.DateTimeField('被取代时间', null=True, blank=True)
    revoked_at = models.DateTimeField('撤销时间', null=True, blank=True)
    revoked_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True,
        related_name='revoked_temporary_grants', verbose_name='撤销人'
    )
    revoke_reason = models.CharField('撤销原因', max_length=500, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'authz_temporary_grant'
        verbose_name = '临时授权'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['grant_no', 'version']

    def __str__(self):
        return f"{self.grant_no} v{self.version} ({self.grantee})"

    @property
    def is_current(self):
        """是否为当前版本(未被新版本取代)"""
        return self.superseded_at is None

    @property
    def status(self):
        """授权状态: 撤销 > 被取代 > 已过期 > 未生效 > 生效中"""
        now = timezone.now()
        if self.revoked_at is not None:
            return 'revoked'
        if self.superseded_at is not None:
            return 'superseded'
        if now >= self.effective_until:
            return 'expired'
        if now < self.effective_from:
            return 'pending'
        return 'active'

    @property
    def status_display(self):
        return self.STATUS_DISPLAY_MAP.get(self.status, self.status)

    def contains_moment(self, at):
        """判断指定时刻是否落在生效区间内(左闭右开)"""
        return self.effective_from <= at < self.effective_until


class TemporaryGrantScope(models.Model):
    """授权资源范围 - 授权版本与物资的关联"""
    grant = models.ForeignKey(
        TemporaryGrant, on_delete=models.CASCADE,
        related_name='scope_items', verbose_name='授权版本'
    )
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='temporary_grant_scopes', verbose_name='物资'
    )

    class Meta:
        db_table = 'authz_grant_scope'
        verbose_name = '授权资源范围'
        verbose_name_plural = verbose_name
        unique_together = ['grant', 'goods']

    def __str__(self):
        return f"{self.grant.grant_no} v{self.grant.version} - {self.goods.name}"


class AccessAuditLogQuerySet(models.QuerySet):
    """审计记录查询集 - 禁止批量修改与删除"""

    def update(self, **kwargs):
        raise ImmutableAuditLogError('审计记录不允许修改')

    def delete(self):
        raise ImmutableAuditLogError('审计记录不允许删除')


class AccessAuditLog(models.Model):
    """访问审计记录 - 放行与拒绝均留痕, 记录后不可删改"""

    DECISION_CHOICES = [
        ('allowed', '放行'),
        ('denied', '拒绝'),
    ]
    BASIS_CHOICES = [
        ('role', '角色权限'),
        ('grant', '临时授权'),
    ]

    user = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='access_audit_logs', verbose_name='访问用户'
    )
    username = models.CharField('访问账号', max_length=50)
    goods = models.ForeignKey(
        Goods, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='access_audit_logs', verbose_name='访问物资'
    )
    goods_code = models.CharField('物资编码', max_length=50)
    goods_name = models.CharField('物资名称', max_length=200)
    grant = models.ForeignKey(
        TemporaryGrant, on_delete=models.PROTECT, null=True, blank=True,
        related_name='access_audit_logs', verbose_name='命中授权版本'
    )
    grant_no = models.CharField('命中授权编号', max_length=30, blank=True, db_index=True)
    grant_version = models.PositiveIntegerField('命中授权版本号', null=True, blank=True)
    decision = models.CharField('访问结论', max_length=10, choices=DECISION_CHOICES)
    basis = models.CharField('放行依据', max_length=10, choices=BASIS_CHOICES, blank=True)
    deny_reason = models.CharField('拒绝原因', max_length=30, blank=True)
    deny_detail = models.CharField('拒绝说明', max_length=200, blank=True)
    path = models.CharField('访问路径', max_length=200, blank=True)
    ip_address = models.GenericIPAddressField('IP地址', null=True, blank=True)
    accessed_at = models.DateTimeField('访问时间', auto_now_add=True)

    objects = AccessAuditLogQuerySet.as_manager()

    class Meta:
        db_table = 'authz_access_audit_log'
        verbose_name = '访问审计记录'
        verbose_name_plural = verbose_name
        ordering = ['-accessed_at']

    def __str__(self):
        return f"{self.username} - {self.goods_name} - {self.get_decision_display()}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableAuditLogError('审计记录不允许修改')
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableAuditLogError('审计记录不允许删除')
