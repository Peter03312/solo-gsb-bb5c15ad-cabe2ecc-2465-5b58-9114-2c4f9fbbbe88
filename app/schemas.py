"""HTTP 请求体的 Pydantic 模型与按事件类型的必填字段校验。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EventType = Literal["transfer", "receive", "seal_check", "storage"]
CorrectionAction = Literal["void", "replace"]

# 各事件类型的必填字段（事件上报与纠错替换共用）
REQUIRED_FIELDS: dict[str, list[str]] = {
    "transfer": ["transfer_no", "from_custodian", "to_custodian"],
    "receive": ["transfer_no", "receiver", "seal_no"],
    "seal_check": ["checked_seal_no"],
    "storage": [],
}


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="样本编号，业务唯一键")
    collected_at: datetime = Field(..., description="采集时间 ISO8601")
    collector: str = Field(..., min_length=1, description="采集人，即初始保管人")
    seal_no: str = Field(..., min_length=1, description="封签号")


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="全局唯一事件号，同号重试幂等")
    event_type: EventType
    sample_code: str = Field(..., min_length=1, description="样本编号")
    occurred_at: datetime = Field(..., description="事件发生时间 ISO8601")

    # transfer（移交）
    transfer_no: str | None = Field(None, description="移交单号（transfer 必填）")
    from_custodian: str | None = Field(None, description="移交发起/交出方（transfer 必填，须为当前保管人）")
    to_custodian: str | None = Field(None, description="接收方（transfer 必填）")

    # receive（签收）
    receiver: str | None = Field(None, description="实际签收人（receive 必填，须等于移交的接收方）")
    seal_no: str | None = Field(None, description="签收核对封签号（receive 必填，须与登记一致）")

    # seal_check（封签检查）
    checked_seal_no: str | None = Field(None, description="检查时读取的封签号（seal_check 必填）")
    seal_intact: bool | None = Field(None, description="封签是否完好，默认 true；false 判无效")

    # storage（实验室入库）
    storage_location: str | None = Field(None, description="入库库位（storage 建议填写）")
    custodian: str | None = Field(None, description="入库后保管人，默认实验室")

    note: str | None = Field(None, description="备注，参与幂等指纹")

    @model_validator(mode="after")
    def _check_required_fields(self) -> "EventIn":
        missing = [f for f in REQUIRED_FIELDS[self.event_type] if not getattr(self, f)]
        if missing:
            raise ValueError(f"事件类型 {self.event_type} 缺少必填字段: {', '.join(missing)}")
        return self


class NewEventIn(BaseModel):
    """纠错替换时提交的完整新事件内容（不含 sample_code —— 纠错不得跨样本）。

    字段集与校验规则与 EventIn 一致；新事件号必须全局唯一且未被占用。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="新事件号，必须未被占用（全局唯一）")
    event_type: EventType
    occurred_at: datetime = Field(..., description="事件发生时间 ISO8601")

    # transfer（移交）
    transfer_no: str | None = Field(None, description="移交单号（transfer 必填）")
    from_custodian: str | None = Field(None, description="移交发起/交出方（transfer 必填，须为当前保管人）")
    to_custodian: str | None = Field(None, description="接收方（transfer 必填）")

    # receive（签收）
    receiver: str | None = Field(None, description="实际签收人（receive 必填，须等于移交的接收方）")
    seal_no: str | None = Field(None, description="签收核对封签号（receive 必填，须与登记一致）")

    # seal_check（封签检查）
    checked_seal_no: str | None = Field(None, description="检查时读取的封签号（seal_check 必填）")
    seal_intact: bool | None = Field(None, description="封签是否完好，默认 true；false 判无效")

    # storage（实验室入库）
    storage_location: str | None = Field(None, description="入库库位（storage 建议填写）")
    custodian: str | None = Field(None, description="入库后保管人，默认实验室")

    note: str | None = Field(None, description="备注，参与幂等指纹")

    @model_validator(mode="after")
    def _check_required_fields(self) -> "NewEventIn":
        missing = [f for f in REQUIRED_FIELDS[self.event_type] if not getattr(self, f)]
        if missing:
            raise ValueError(f"事件类型 {self.event_type} 缺少必填字段: {', '.join(missing)}")
        return self


class CorrectionIn(BaseModel):
    """核定前事件纠错：作废（void）或替换（replace）。

    * 仅核定入库前可纠错，纠错不得跨样本；
    * 同一纠错号同内容重试幂等（200），异内容冲突（409）；
    * replace 必须给出完整新事件内容与未占用的新事件号；void 不得携带新事件。
    """

    model_config = ConfigDict(extra="forbid")

    correction_id: str = Field(..., min_length=1, description="全局唯一纠错号，同号重试幂等")
    sample_code: str = Field(..., min_length=1, description="样本编号（纠错不得跨样本）")
    target_event_id: str = Field(..., min_length=1, description="被作废/替换的当前有效事件号")
    action: CorrectionAction = Field(..., description="void=作废，replace=替换")
    operator: str = Field(..., min_length=1, description="纠错操作人")
    reason: str = Field(..., min_length=1, description="纠错原因")
    new_event: NewEventIn | None = Field(None, description="替换时的完整新事件内容；作废时必须为空")

    @model_validator(mode="after")
    def _check_action_payload(self) -> "CorrectionIn":
        if self.action == "replace" and self.new_event is None:
            raise ValueError("action=replace 必须提供 new_event（完整新事件内容与未占用的新事件号）")
        if self.action == "void" and self.new_event is not None:
            raise ValueError("action=void 作废不得携带 new_event")
        return self


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(..., min_length=1, description="核定操作人")
    note: str | None = Field(None, description="核定备注")


class BatchTransferItemIn(BaseModel):
    """批量移交中的一份样本：样本编号、该份移交单号、该份既有 transfer 事件号。"""

    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="样本编号")
    transfer_no: str = Field(..., min_length=1, description="该份样本的移交单号")
    event_id: str = Field(..., min_length=1, description="该份样本对应的（既有）transfer 事件号，全局唯一")


class TransferBatchIn(BaseModel):
    """同一交接车次多份样本的一次批量移交提交。

    * 批次号全局唯一；统一移交时间、交出人、接收人对批内各份样本生效；
    * 各份移交均作为既有 transfer 事件（event_id 全局唯一）落库；
    * 批内样本编号、事件号重复（在服务层按统一错误格式拒绝）；
    * 样本不存在/已核定、任一事件号已占用 → 整批失败，不留事件与批次。
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(..., min_length=1, description="全局唯一批次号，同号重试幂等")
    occurred_at: datetime = Field(..., description="统一移交时间 ISO8601")
    from_custodian: str = Field(..., min_length=1, description="交出人")
    to_custodian: str = Field(..., min_length=1, description="接收人")
    items: list[BatchTransferItemIn] = Field(..., min_length=1, description="批内各份样本（至少一份）")


class DossierItemIn(BaseModel):
    """案卷中的一份样本：样本编号 + 可选的非负历史版本号（缺省取封存事务中的最新版本）。"""

    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="样本编号")
    version: int | None = Field(None, ge=0, description="指定封存的非负历史版本号；缺省取最新版本")


class DossierIn(BaseModel):
    """同一抽检的多份样本保管链一次封存为案卷。

    * 案卷号全局唯一；至少两份不重复样本（重复/不存在/版本不存在 → 整卷失败不留案卷）；
    * 可逐份指定非负历史版本，未指定者取封存事务中的最新版本（同一数据库一致视图）；
    * 同案卷号同内容重试幂等（即使最新版本已变化也返回原卷），异内容冲突。
    """

    model_config = ConfigDict(extra="forbid")

    dossier_id: str = Field(..., min_length=1, description="全局唯一案卷号，同号重试幂等")
    items: list[DossierItemIn] = Field(..., min_length=2, description="卷内样本（至少两份，编号不重复）")


class ReviewItemIn(BaseModel):
    """案卷复核清单中的一份意见：卷内样本编号 + 通过/不通过 + 不通过原因。

    通过（passed=true）不得携带原因；不通过（passed=false）必须给出非空原因。
    """

    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="卷内样本编号，须命中案卷封存清单")
    passed: bool = Field(..., description="该份复核结论：true=通过，false=不通过")
    reason: str | None = Field(None, description="不通过原因；通过时必须为空，不通过时必填")

    @model_validator(mode="after")
    def _check_reason(self) -> "ReviewItemIn":
        if self.passed and self.reason is not None:
            raise ValueError("通过的份项不得填写不通过原因（reason 必须为空）")
        if not self.passed and not (self.reason and self.reason.strip()):
            raise ValueError("不通过的份项必须填写非空不通过原因（reason）")
        return self


class ReviewIn(BaseModel):
    """案卷复核意见提交（前两次为两名不同复核人的完整清单；第三次为第三人裁决）。

    * expected_digest 必须与案卷封存摘要一致，否则拒绝写入（复核只依据封存版本快照）；
    * 前两份意见由两名不同复核人各提交一次完整清单（覆盖卷内每份样本，不缺项/不重复份项）；
    * 前两份都到齐后，仅对有分歧的份项开放第三人裁决：裁决人不得是前两次复核人之一，
      裁决清单必须恰好覆盖全部分歧份项；全部份项终局后形成案卷结论；
    * 意见提交后不可改；同复核单号同内容重试幂等返回原结果，异内容冲突；
    * 越阶段提交（案卷终局后再提交、第二份未到就裁决、前两份缺漏时的裁决清单不全等）拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    review_no: str = Field(..., min_length=1, description="全局唯一复核单号，同号重试幂等")
    reviewer: str = Field(..., min_length=1, description="复核人标识（工号/姓名等）")
    expected_digest: str = Field(
        ..., min_length=1,
        description="提交方持有的案卷封存摘要（SHA-256，64 位十六进制）；与库存不一致则拒绝写入",
    )
    items: list[ReviewItemIn] = Field(..., min_length=1, description="逐份样本意见（前两次须完整覆盖案卷清单）")
