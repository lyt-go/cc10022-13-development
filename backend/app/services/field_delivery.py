"""共享字段交付领域服务：按渠道授权范围管控黄金记录字段的读取与交付。

能力边界：
- 黄金模型登记可对外共享的字段，渠道与黄金模型构成独立的授权范围，
  渠道之间互不影响；
- 查询接口读取黄金记录时关联渠道权限，只交付该渠道被授权的字段，
  空配置回落到该渠道的默认发布范围，绝不返回整份实体模型；
- 每次交付生成一条交付凭证，快照本次字段口径，供数据负责人复核；
- 只有授权字段口径一致（同一黄金模型、同一字段集合）的交付行才允许合并使用。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

SCOPE_MODULE = "field_delivery"
LEDGER_MODULE = "field_delivery_ledger"


def _split_fields(raw: Any) -> list[str]:
    """把逗号分隔的字段配置解析成去重后的有序字段列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        seen: list[str] = []
        for item in raw:
            name = str(item).strip()
            if name and name not in seen:
                seen.append(name)
        return seen
    result: list[str] = []
    for part in str(raw).split(","):
        name = part.strip()
        if name and name not in result:
            result.append(name)
    return result


class FieldDeliveryService(BaseModuleService):
    MODULE = SCOPE_MODULE
    ENTITY = "渠道共享字段范围"
    OPERATION_TERM = "登记"
    LIST_FIELDS = ["定义编号", "渠道", "黄金模型", "允许字段", "发布范围", "范围版本", "生效时间"]
    REQUIRED_FIELDS = ["渠道", "黄金模型", "允许字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已撤回"]
    ACTION_RULES = {"提交校验": "待校验", "发布范围": "已生效", "撤回授权": "已撤回"}
    NEGATIVE_ACTIONS = ["撤回授权"]

    # ── 渠道共享字段范围登记 ──────────────────────────────────────────
    def upsert_channel_scope(self, channel: str, golden_model: str,
                             allowed_fields: list[str] | str,
                             default_publish_fields: list[str] | str | None = None,
                             remark: str | None = None) -> dict[str, Any]:
        """登记或更新某渠道在某黄金模型上的可共享字段授权范围。

        同一渠道+黄金模型只有一份生效范围，重复登记按新版本覆盖；
        不同渠道、不同黄金模型的范围互相独立。
        """
        channel = str(channel).strip()
        golden_model = str(golden_model).strip()
        allowed = _split_fields(allowed_fields)
        if not channel or not golden_model:
            return {"ok": False, "reason": "渠道与黄金模型不能为空"}
        if not allowed:
            return {"ok": False, "reason": "允许字段不能为空，空授权范围必须显式登记"}

        defaults = _split_fields(default_publish_fields) if default_publish_fields is not None else list(allowed)
        # 默认发布范围只能是允许字段的子集，未登记的字段自动剔除
        defaults = [f for f in defaults if f in allowed]
        if not defaults:
            defaults = list(allowed)

        rows = store.rows(SCOPE_MODULE)
        existing = self._find_scope_row(channel, golden_model, include_inactive=True)
        now = datetime.datetime.utcnow().isoformat()
        if existing is not None:
            version = self._bump_version(existing.get("范围版本", "v0"))
            existing.update({
                "允许字段": ",".join(allowed),
                "发布范围": ",".join(defaults),
                "范围版本": version,
                "生效时间": now,
                "status": "已生效",
                "pending": False,
                "abnormal": False,
                "备注": remark or existing.get("备注", ""),
            })
            entry = existing
        else:
            entry = {
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
                "定义编号": f"FDS-{len(rows) + 1:04d}",
                "渠道": channel,
                "黄金模型": golden_model,
                "允许字段": ",".join(allowed),
                "发布范围": ",".join(defaults),
                "范围版本": "v1",
                "生效时间": now,
                "备注": remark or "",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            rows.append(entry)
        return {"ok": True, "scope": dict(entry), "渠道": channel, "黄金模型": golden_model}

    def revoke_channel_fields(self, channel: str, golden_model: str,
                              revoked_fields: list[str] | str) -> dict[str, Any]:
        """撤回渠道对若干字段的授权，撤回后新的交付不再输出这些字段。

        直接收敛当前生效范围（模拟权限实时撤回），历史交付凭证保留撤回前
        的字段口径快照，不受影响。
        """
        scope = self._find_scope_row(channel, golden_model)
        if scope is None:
            return {"ok": False, "reason": "渠道未登记该黄金模型的生效共享字段范围"}
        revoked = _split_fields(revoked_fields)
        allowed = _split_fields(scope.get("允许字段"))
        remaining = [f for f in allowed if f not in revoked]
        defaults = [f for f in _split_fields(scope.get("发布范围")) if f in remaining]
        scope["允许字段"] = ",".join(remaining)
        scope["发布范围"] = ",".join(defaults)
        scope["范围版本"] = self._bump_version(scope.get("范围版本", "v0"))
        scope["生效时间"] = datetime.datetime.utcnow().isoformat()
        return {
            "ok": True,
            "scope": dict(scope),
            "revoked_fields": [f for f in revoked if f in allowed],
            "remaining_fields": remaining,
        }

    def query_channel_scopes(self, channel: str | None = None,
                             golden_model: str | None = None) -> list[dict[str, Any]]:
        """查询渠道共享字段范围，可按渠道、黄金模型过滤。"""
        rows = store.rows(SCOPE_MODULE)
        result = list(rows)
        if channel:
            result = [r for r in result if r.get("渠道") == channel]
        if golden_model:
            result = [r for r in result if r.get("黄金模型") == golden_model]
        return [dict(r) for r in result]

    # ── 渠道授权范围解析 ──────────────────────────────────────────────
    def resolve_delivery_scope(self, channel: str, golden_model: str,
                               requested_fields: list[str] | str | None = None) -> dict[str, Any]:
        """解析一次读取的实际授权字段口径。

        显式选择字段时，取请求字段与渠道允许字段的交集，越权字段记入拦截；
        空配置时回落到渠道默认发布范围（允许字段的子集）。
        """
        scope = self._find_scope_row(channel, golden_model)
        if scope is None:
            return {"ok": False, "reason": "渠道未登记该黄金模型的生效共享字段范围"}
        allowed = _split_fields(scope.get("允许字段"))
        requested = _split_fields(requested_fields)
        default_scope = _split_fields(scope.get("发布范围")) or list(allowed)

        if requested:
            authorized = [f for f in requested if f in allowed]
            blocked = [f for f in requested if f not in allowed]
            source = "接口显式选择"
        else:
            # 空配置受默认发布范围控制，且默认范围必须能从允许字段集合解释
            authorized = [f for f in default_scope if f in allowed]
            blocked = []
            source = "默认发布范围"
        return {
            "ok": True,
            "渠道": scope.get("渠道"),
            "黄金模型": scope.get("黄金模型"),
            "允许字段": allowed,
            "请求字段": requested,
            "授权字段口径": authorized,
            "拦截字段": blocked,
            "口径来源": source,
            "范围版本": scope.get("范围版本"),
            "定义编号": scope.get("定义编号"),
        }

    # ── 共享字段交付 ──────────────────────────────────────────────────
    def deliver_shared_fields(self, channel: str, golden_model: str,
                              requested_fields: list[str] | str | None = None,
                              filters: dict[str, str] | None = None,
                              query_field: str | None = None,
                              api_id: int | None = None) -> dict[str, Any]:
        """按渠道权限读取黄金记录并生成共享字段交付数据。

        交付数据只包含授权字段口径内的字段；每次交付保存一条凭证，
        快照本次字段口径、拦截字段与范围版本，保证交付可追溯、可复核。
        """
        resolution = self.resolve_delivery_scope(channel, golden_model, requested_fields)
        if not resolution.get("ok"):
            return resolution

        from app.services.golden_record import GoldenRecordService
        records = GoldenRecordService().search_golden_records(entity_model=golden_model)
        if filters and query_field:
            needle = str(filters.get(query_field, "")).casefold()
            if needle:
                records = [
                    r for r in records
                    if needle in str(r.get(query_field, "")).casefold()
                ]

        authorized = resolution["授权字段口径"]
        delivery_data = [
            {field: record.get(field) for field in authorized if field in record}
            for record in records
        ]
        ledger = self._save_delivery_ledger(
            resolution=resolution,
            requested=resolution["请求字段"],
            delivery_row_count=len(delivery_data),
            api_id=api_id,
        )
        return {
            "ok": True,
            "channel": resolution["渠道"],
            "golden_model": resolution["黄金模型"],
            "records": delivery_data,
            "count": len(delivery_data),
            "field_basis": authorized,
            "allowed_fields": resolution["允许字段"],
            "blocked_fields": resolution["拦截字段"],
            "scope_source": resolution["口径来源"],
            "scope_version": resolution["范围版本"],
            "ledger_id": ledger["id"],
            "ledger_no": ledger["凭证编号"],
        }

    def _save_delivery_ledger(self, resolution: dict[str, Any], requested: list[str],
                              delivery_row_count: int, api_id: int | None) -> dict[str, Any]:
        """保存交付凭证：快照本次字段口径，便于事后复核与合并判定。"""
        rows = store.rows(LEDGER_MODULE)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "凭证编号": f"FDV-{len(rows) + 1:04d}",
            "渠道": resolution["渠道"],
            "黄金模型": resolution["黄金模型"],
            "查询接口": api_id or "",
            "定义编号": resolution["定义编号"],
            "范围版本": resolution["范围版本"],
            "请求字段": ",".join(requested),
            "授权字段口径": ",".join(resolution["授权字段口径"]),
            "拦截字段": ",".join(resolution["拦截字段"]),
            "口径来源": resolution["口径来源"],
            "交付行数": delivery_row_count,
            "交付时间": datetime.datetime.utcnow().isoformat(),
            "status": "已交付",
            "pending": False,
            "abnormal": False,
        }
        rows.append(entry)
        return entry

    def query_delivery_ledger(self, channel: str | None = None,
                              golden_model: str | None = None) -> list[dict[str, Any]]:
        """查询交付凭证，供数据负责人核对每次实际交付的字段口径。"""
        rows = store.rows(LEDGER_MODULE)
        result = list(rows)
        if channel:
            result = [r for r in result if r.get("渠道") == channel]
        if golden_model:
            result = [r for r in result if r.get("黄金模型") == golden_model]
        result.sort(key=lambda r: str(r.get("交付时间", "")))
        return [dict(r) for r in result]

    def can_merge_delivery_rows(self, ledger_id_a: int, ledger_id_b: int) -> dict[str, Any]:
        """判定两份交付行是否可合并：同一黄金模型且字段授权口径完全一致。"""
        row_a = store.find(LEDGER_MODULE, ledger_id_a)
        row_b = store.find(LEDGER_MODULE, ledger_id_b)
        if row_a is None or row_b is None:
            return {"ok": False, "reason": "交付凭证不存在，无法核对合并口径"}
        if row_a.get("黄金模型") != row_b.get("黄金模型"):
            return {"ok": False, "reason": "黄金模型不同，交付行不可合并", "mergeable": False}
        basis_a = set(_split_fields(row_a.get("授权字段口径")))
        basis_b = set(_split_fields(row_b.get("授权字段口径")))
        if basis_a != basis_b:
            return {
                "ok": True,
                "mergeable": False,
                "reason": "字段授权口径不一致，受同一权限口径约束的行才可合并使用",
                "basis_a": sorted(basis_a),
                "basis_b": sorted(basis_b),
            }
        return {
            "ok": True,
            "mergeable": True,
            "reason": "字段授权口径一致，交付行可合并使用",
            "shared_basis": sorted(basis_a),
        }

    # ── 内部工具 ──────────────────────────────────────────────────────
    def _find_scope_row(self, channel: str, golden_model: str,
                        include_inactive: bool = False) -> dict[str, Any] | None:
        rows = store.rows(SCOPE_MODULE)
        candidates = [
            row for row in rows
            if row.get("渠道") == channel and row.get("黄金模型") == golden_model
        ]
        if not include_inactive:
            candidates = [row for row in candidates if row.get("status") == "已生效"]
        if not candidates:
            return None
        return max(candidates, key=lambda row: int(row.get("id", 0)))

    @staticmethod
    def _bump_version(version: str) -> str:
        try:
            current = int(str(version).lower().removeprefix("v"))
        except ValueError:
            current = 0
        return f"v{current + 1}"
