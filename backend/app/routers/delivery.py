"""共享字段交付路由：渠道授权范围登记、字段交付、凭证复核与合并核对。

与通用资源路由并列注册，路径使用 field-delivery / mdm-apis 固定前缀，
避免被 /api/{module}/{entry_id} 动态路由抢占。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from app.schemas import (
    ChannelScopePayload,
    FieldRevokePayload,
    LedgerMergePayload,
    SharedFieldDeliveryPayload,
)
from app.services.field_delivery import FieldDeliveryService
from app.services.mdm_api import MdmApiService

router = APIRouter(prefix="/api", tags=["共享字段交付"])

_delivery_svc = FieldDeliveryService()


@router.post("/field-delivery/scopes")
def upsert_channel_scope(payload: ChannelScopePayload) -> dict[str, Any]:
    """登记/更新渠道在某黄金模型上的共享字段授权范围。"""
    return _delivery_svc.upsert_channel_scope(
        channel=payload.channel,
        golden_model=payload.golden_model,
        allowed_fields=payload.allowed_fields,
        default_publish_fields=payload.default_publish_fields,
        remark=payload.remark,
    )


@router.get("/field-delivery/scopes")
def list_channel_scopes(
    channel: str | None = Query(default=None),
    golden_model: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """查询渠道共享字段范围，渠道之间的范围互相独立。"""
    return _delivery_svc.query_channel_scopes(channel=channel, golden_model=golden_model)


@router.post("/field-delivery/scopes/revoke")
def revoke_channel_fields(payload: FieldRevokePayload) -> dict[str, Any]:
    """撤回渠道对指定字段的授权。"""
    return _delivery_svc.revoke_channel_fields(
        channel=payload.channel,
        golden_model=payload.golden_model,
        revoked_fields=payload.revoked_fields,
    )


@router.post("/field-delivery/deliver")
def deliver_shared_fields(payload: SharedFieldDeliveryPayload) -> dict[str, Any]:
    """按渠道权限生成共享字段交付数据，并保存本次字段口径凭证。"""
    return _delivery_svc.deliver_shared_fields(
        channel=payload.channel,
        golden_model=payload.golden_model,
        requested_fields=payload.requested_fields,
        filters=payload.filters,
        query_field=payload.query_field,
        api_id=payload.api_id,
    )


@router.get("/field-delivery/ledger")
def list_delivery_ledger(
    channel: str | None = Query(default=None),
    golden_model: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """查询交付凭证：每次交付的字段授权口径快照。"""
    return _delivery_svc.query_delivery_ledger(channel=channel, golden_model=golden_model)


@router.post("/field-delivery/ledger/merge-check")
def check_ledger_merge(payload: LedgerMergePayload) -> dict[str, Any]:
    """核对两份交付行是否受同一字段授权口径约束、可否合并使用。"""
    return _delivery_svc.can_merge_delivery_rows(payload.ledger_id_a, payload.ledger_id_b)


@router.get("/mdm-apis/{api_id}/query")
def query_golden_via_api(api_id: int,
                         channel: str = Query(..., description="调用渠道编码"),
                         return_fields: str | None = Query(
                             default=None,
                             description="逗号分隔的显式返回字段；不传或留空走渠道默认发布范围")) -> dict[str, Any]:
    """既有发布接口按渠道读取黄金记录，响应字段受渠道授权范围约束。

    每次调用独立解析字段口径，不修改接口登记的返回字段；空配置回落
    渠道默认发布范围，显式字段与渠道允许字段取交集。
    """
    service = MdmApiService()
    api_def = service.get_entry(api_id)
    if not api_def:
        return {"ok": False, "reason": "查询接口不可用"}
    requested = [f.strip() for f in (return_fields or "").split(",") if f.strip()]
    # 用一次性视图承载本次请求的字段选择，避免污染接口持久化配置
    api_def = dict(api_def)
    api_def["返回字段"] = ",".join(requested)
    return service.query_golden_records_by_api(api_id, channel=channel, _api_override=api_def)
