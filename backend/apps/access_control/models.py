"""
临时授权访问控制模型

设计要点：
- 外部鉴定人员只能凭「预约 + 当前有效的授权版本 + 资源范围」三重条件只读访问在案物资；
- 授权一经签发即按版本（GrantVersion）快照依据、生效区间与资源范围，
  换发产生新版本并令旧版本失效（superseded），撤销立即生效（revoked）；
- 生效区间统一使用半开区间 [valid_from, valid_to)，跨午夜只是普通的绝对时间区间；
- AccessAudit 为只追加台账：任何 update / delete / save 改写都会被拒绝，
  且通过 PROTECT 外键阻止授权/版本/案件被物理删除。
"""
from django.conf import settings
from django.db import models


class Case(models.Model):
    """案件"""

    case_no = models.CharField('案件编号', max_length=64, unique=True)
    name = models.CharField('案件名称', max_length=200)
    is_active = models.BooleanField('是否在办', default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='created_cases', verbose_name='创建人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ac_case'
        verbose_name = '案件'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.case_no} {self.name}"


class CaseEvidence(models.Model):
    """在案物资（案件与物资的关联记录）"""

    case = models.ForeignKey(
        Case, on_delete=models.PROTECT,
        related_name='evidences', verbose_name='所属案件'
    )
    goods = models.ForeignKey(
        'warehouse.Goods', on_delete=models.PROTECT,
        related_name='case_evidences', verbose_name='物资'
    )
    registered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='registered_evidences', verbose_name='登记人'
    )
    remark = models.CharField('备注', max_length=200, blank=True)
    created_at = models.DateTimeField('登记时间', auto_now_add=True)

    class Meta:
        db_table = 'ac_case_evidence'
        verbose_name = '在案物资'
        verbose_name_plural = verbose_name
        unique_together = ['case', 'goods']

    def __str__(self):
        return f"{self.case.case_no}-{self.goods.code}"


class ExternalAppraiser(models.Model):
    """外部鉴定人员档案（与登录账号一对一）"""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name='appraiser_profile', verbose_name='登录账号',
        limit_choices_to={'role': 'external'}
    )
    name = models.CharField('姓名', max_length=50)
    id_card = models.CharField('身份证号', max_length=18, unique=True)
    organization = models.CharField('鉴定机构', max_length=200)
    phone = models.CharField('联系电话', max_length=20, blank=True)
    is_active = models.BooleanField('是否在册', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ac_external_appraiser'
        verbose_name = '外部鉴定人员'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name}（{self.organization}）"


class Appointment(models.Model):
    """外部鉴定人员查看在案物资的预约（半开时间区间 [start_time, end_time)）"""

    STATUS_CHOICES = [
        ('confirmed', '已确认'),
        ('cancelled', '已取消'),
        ('completed', '已完成'),
    ]

    appraiser = models.ForeignKey(
        ExternalAppraiser, on_delete=models.PROTECT,
        related_name='appointments', verbose_name='鉴定人员'
    )
    case = models.ForeignKey(
        Case, on_delete=models.PROTECT,
        related_name='appointments', verbose_name='预约案件'
    )
    evidences = models.ManyToManyField(
        CaseEvidence, related_name='appointments',
        verbose_name='预约查看物资'
    )
    start_time = models.DateTimeField('预约开始时间')
    end_time = models.DateTimeField('预约结束时间')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='confirmed')
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='created_appointments', verbose_name='预约确认人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'ac_appointment'
        verbose_name = '预约'
        verbose_name_plural = verbose_name
        ordering = ['-start_time']

    def __str__(self):
        return f"{self.appraiser.name}-{self.case.case_no}-{self.start_time:%Y-%m-%d %H:%M}"

    def is_within_window(self, at):
        """时刻 at 是否落在预约时段内（含跨午夜）"""
        return self.start_time <= at < self.end_time


class Grant(models.Model):
    """
    临时授权（逻辑主体）。

    一次授权的所有要素（依据、区间、范围、签发人）都固化在 GrantVersion 上；
    续期/调整范围须换发新版本，撤销作用于具体版本。
    """

    appraiser = models.ForeignKey(
        ExternalAppraiser, on_delete=models.PROTECT,
        related_name='grants', verbose_name='被授权人'
    )
    appointment = models.ForeignKey(
        Appointment, on_delete=models.PROTECT,
        related_name='grants', verbose_name='关联预约'
    )
    case = models.ForeignKey(
        Case, on_delete=models.PROTECT,
        related_name='grants', verbose_name='授权案件'
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='issued_grants', verbose_name='授权人'
    )
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'ac_grant'
        verbose_name = '临时授权'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"授权#{self.pk}-{self.appraiser.name}-{self.case.case_no}"

    @property
    def current_version(self):
        return self.versions.order_by('-version_no').first()


class GrantVersion(models.Model):
    """授权版本：依据、生效区间与资源范围在此不可变快照留存"""

    STATUS_CHOICES = [
        ('active', '生效中'),
        ('superseded', '已换发'),
        ('revoked', '已撤销'),
    ]

    grant = models.ForeignKey(
        Grant, on_delete=models.PROTECT,
        related_name='versions', verbose_name='所属授权'
    )
    version_no = models.PositiveIntegerField('版本号')
    # 授权依据必须说明（法律文书 / 委托函 / 审批单等）
    basis_type = models.CharField('依据类型', max_length=50)
    basis_ref = models.CharField('依据文号', max_length=200)
    basis_reason = models.TextField('授权依据说明')
    # 半开生效区间 [valid_from, valid_to)
    valid_from = models.DateTimeField('生效时间')
    valid_to = models.DateTimeField('失效时间')
    # 资源范围快照（CaseEvidence 主键列表），版本形成后不再随关联物资变化
    scope_evidence_ids = models.JSONField('资源范围快照', default=list)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='active')
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
        related_name='issued_grant_versions', verbose_name='签发人'
    )
    issued_at = models.DateTimeField('签发时间', auto_now_add=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='revoked_grant_versions', verbose_name='撤销人'
    )
    revoked_at = models.DateTimeField('撤销时间', null=True, blank=True)
    revoke_reason = models.TextField('撤销原因', blank=True)

    class Meta:
        db_table = 'ac_grant_version'
        verbose_name = '授权版本'
        verbose_name_plural = verbose_name
        unique_together = ['grant', 'version_no']
        ordering = ['grant_id', '-version_no']

    def __str__(self):
        return f"授权#{self.grant_id} v{self.version_no}（{self.get_status_display()}）"

    def is_effective_at(self, at):
        """给定时刻是否处于生效区间内（撤销/换发状态不算有效）"""
        if self.status != 'active':
            return False
        return self.valid_from <= at < self.valid_to

    def covers_evidence(self, evidence_id):
        """资源范围是否覆盖某项在案物资"""
        return evidence_id in (self.scope_evidence_ids or [])


class _AppendOnlyQuerySet(models.QuerySet):
    """只追加台账的 QuerySet：禁止批量更新与删除"""

    def update(self, **kwargs):
        raise PermissionError('审计台账为只追加记录，禁止修改')

    def delete(self):
        raise PermissionError('审计台账为只追加记录，禁止删除')


class AccessAudit(models.Model):
    """
    授权访问审计台账（只追加）。

    每次访问尝试都记录一条，包含放行/拒绝结论、判定理由以及
    「由哪个授权版本放行」（grant_id + grant_version_no + version 主键）。
    """

    RESULT_CHOICES = [
        ('allowed', '放行'),
        ('denied', '拒绝'),
    ]

    DENY_REASON_CHOICES = [
        ('not_appraiser', '账号非外部鉴定人员'),
        ('appraiser_inactive', '鉴定人员档案已停用'),
        ('no_appointment', '无有效预约'),
        ('appointment_outside_window', '不在预约时段内'),
        ('appointment_cancelled', '预约已取消'),
        ('no_active_grant', '无生效授权'),
        ('grant_outside_window', '授权不在生效区间内'),
        ('grant_revoked', '授权已被提前撤销'),
        ('out_of_scope', '资源不在授权范围内'),
        ('evidence_not_in_case', '物资不属于授权案件'),
    ]

    appraiser = models.ForeignKey(
        ExternalAppraiser, on_delete=models.PROTECT, null=True, blank=True,
        related_name='access_audits', verbose_name='鉴定人员'
    )
    # 人员/物资名称冗余快照，即便档案后续变更也能还原当时事实
    appraiser_name = models.CharField('鉴定人员姓名快照', max_length=50)
    case = models.ForeignKey(
        Case, on_delete=models.PROTECT,
        related_name='access_audits', verbose_name='案件'
    )
    evidence = models.ForeignKey(
        CaseEvidence, on_delete=models.PROTECT,
        related_name='access_audits', verbose_name='访问物资'
    )
    appointment = models.ForeignKey(
        Appointment, on_delete=models.PROTECT, null=True,
        related_name='access_audits', verbose_name='匹配预约'
    )
    grant = models.ForeignKey(
        Grant, on_delete=models.PROTECT, null=True,
        related_name='access_audits', verbose_name='匹配授权'
    )
    grant_version = models.ForeignKey(
        GrantVersion, on_delete=models.PROTECT, null=True,
        related_name='access_audits', verbose_name='放行授权版本'
    )
    grant_version_no = models.PositiveIntegerField('放行授权版本号', null=True)
    result = models.CharField('判定结论', max_length=10, choices=RESULT_CHOICES)
    deny_reason = models.CharField('拒绝原因', max_length=40, choices=DENY_REASON_CHOICES, blank=True)
    accessed_at = models.DateTimeField('访问时刻', default=None)
    ip_address = models.GenericIPAddressField('IP地址', null=True, blank=True)
    user_agent = models.CharField('用户代理', max_length=500, blank=True)

    objects = _AppendOnlyQuerySet.as_manager()

    class Meta:
        db_table = 'ac_access_audit'
        verbose_name = '授权访问审计'
        verbose_name_plural = verbose_name
        ordering = ['-accessed_at']

    def __str__(self):
        return f"{self.appraiser_name} {self.get_result_display()} {self.evidence_id} @ {self.accessed_at:%Y-%m-%d %H:%M:%S}"

    def save(self, *args, **kwargs):
        # 只允许新增，不允许改写既有审计记录
        if self.pk is not None:
            raise PermissionError('审计台账为只追加记录，禁止修改')
        if self.accessed_at is None:
            from django.utils import timezone
            self.accessed_at = timezone.now()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise PermissionError('审计台账为只追加记录，禁止删除')
