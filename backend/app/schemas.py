"""通用接口契约；模块字段由领域服务持有，不再为每个模块复制一份模型类。"""
from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PageResult(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int = 1
    size: int = 20


class ActionResult(BaseModel):
    ok: bool
    message: str
    entry: dict[str, Any] | None = None


class EntryPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    remark: str | None = None


class ActionPayload(BaseModel):
    action: str
    remark: str | None = None


class ChannelScopePayload(BaseModel):
    """渠道共享字段范围登记/更新请求。"""
    channel: str
    golden_model: str
    allowed_fields: list[str] | str
    default_publish_fields: list[str] | str | None = None
    remark: str | None = None


class SharedFieldDeliveryPayload(BaseModel):
    """按渠道授权范围读取黄金记录的交付请求。"""
    channel: str
    golden_model: str
    requested_fields: list[str] | str | None = None
    filters: dict[str, str] | None = None
    query_field: str | None = None
    api_id: int | None = None


class FieldRevokePayload(BaseModel):
    """渠道字段授权撤回请求。"""
    channel: str
    golden_model: str
    revoked_fields: list[str] | str


class LedgerMergePayload(BaseModel):
    """交付凭证可合并性核对请求。"""
    ledger_id_a: int
    ledger_id_b: int
