"""共享字段交付链路测试。

场景：同一黄金模型在两个渠道登记共享定义；一轮名单查询进行中客服渠道的
证件字段权限被撤回；数据负责人核对该次与之后的实际交付，验证字段授权
口径的完整性、可追溯性、渠道独立性与合并约束。
"""
from __future__ import annotations

import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.field_delivery import LEDGER_MODULE, FieldDeliveryService
from app.services.golden_record import GoldenRecordService
from app.services.mdm_api import MdmApiService
from app.store import store

GOLDEN_MODEL = "share_customer"
CHANNEL_SERVICE = "客服渠道-交付测试"
CHANNEL_MARKETING = "营销渠道-交付测试"
# 每个测试方法使用独立渠道后缀，避免内存仓库在方法间相互污染
_SUFFIX = {"n": 0}


def _unique(prefix: str) -> str:
    _SUFFIX["n"] += 1
    return f"{prefix}-{_SUFFIX['n']}"


def _prepare_golden_records() -> None:
    """准备同一黄金模型下带证件字段的两条生效黄金记录（幂等，只播种一次）。"""
    existing = GoldenRecordService().search_golden_records(entity_model=GOLDEN_MODEL)
    if existing:
        return
    svc = GoldenRecordService()
    svc.create_golden_record(
        GOLDEN_MODEL,
        {"name": "张三", "phone": "13800138000", "id_no": "110101199001011234",
         "email": "zhangsan@example.com", "secret_level": "S3"},
        ["SRC-CRM-1"],
    )
    svc.create_golden_record(
        GOLDEN_MODEL,
        {"name": "李四", "phone": "13900139000", "id_no": "310101199203034567",
         "email": "lisi@example.com", "secret_level": "S2"},
        ["SRC-CRM-2"],
    )


def _setup_two_channels() -> tuple[str, str, FieldDeliveryService]:
    """同一黄金模型登记两个渠道的共享定义，范围互相独立。"""
    svc = FieldDeliveryService()
    service_channel = _unique(CHANNEL_SERVICE)
    marketing_channel = _unique(CHANNEL_MARKETING)
    r1 = svc.upsert_channel_scope(
        service_channel, GOLDEN_MODEL,
        ["name", "phone", "id_no", "email"],
        default_publish_fields=["name", "phone", "id_no", "email"],
    )
    r2 = svc.upsert_channel_scope(
        marketing_channel, GOLDEN_MODEL,
        ["name", "phone"],
        default_publish_fields=["name", "phone"],
    )
    assert r1["ok"] and r2["ok"]
    return service_channel, marketing_channel, svc


class TestFieldDeliveryChain:
    """渠道授权范围 → 共享字段交付 → 凭证复核 → 合并口径核对。"""

    def test_channel_scopes_are_independent(self):
        """渠道之间的授权范围互相独立，互不覆盖。"""
        service_channel, marketing_channel, svc = _setup_two_channels()
        service_scope = svc.query_channel_scopes(service_channel, GOLDEN_MODEL)[0]
        marketing_scope = svc.query_channel_scopes(marketing_channel, GOLDEN_MODEL)[0]
        assert service_scope["允许字段"] == "name,phone,id_no,email"
        assert marketing_scope["允许字段"] == "name,phone"

        # 重新登记营销渠道不应影响客服渠道的范围
        svc.upsert_channel_scope(marketing_channel, GOLDEN_MODEL, ["name"])
        service_scope_again = svc.query_channel_scopes(service_channel, GOLDEN_MODEL)[0]
        assert service_scope_again["允许字段"] == "name,phone,id_no,email"
        marketing_again = svc.query_channel_scopes(marketing_channel, GOLDEN_MODEL)[0]
        assert marketing_again["允许字段"] == "name"
        assert marketing_again["范围版本"] == "v2"

    def test_explicit_fields_intersect_channel_permissions(self):
        """显式选择两个允许字段时，响应只含这两个字段，越权字段被拦截。"""
        _prepare_golden_records()
        service_channel, _, svc = _setup_two_channels()
        result = svc.deliver_shared_fields(
            service_channel, GOLDEN_MODEL, requested_fields=["name", "id_no"],
        )
        assert result["ok"] is True
        assert result["count"] == 2
        assert set(result["records"][0].keys()) == {"name", "id_no"}
        assert result["field_basis"] == ["name", "id_no"]
        assert result["blocked_fields"] == []

        # 显式请求未授权字段：不交付并留痕拦截
        blocked = svc.deliver_shared_fields(
            service_channel, GOLDEN_MODEL,
            requested_fields=["name", "secret_level"],
        )
        assert blocked["ok"] is True
        assert set(blocked["records"][0].keys()) == {"name"}
        assert blocked["blocked_fields"] == ["secret_level"]

    def test_empty_config_falls_back_to_default_publish_scope(self):
        """空配置重查必须受默认发布范围控制，且能从允许字段集合解释。"""
        service_channel, _, svc = _setup_two_channels()
        result = svc.deliver_shared_fields(service_channel, GOLDEN_MODEL)
        assert result["ok"] is True
        assert result["scope_source"] == "默认发布范围"
        allowed = set(result["allowed_fields"])
        # 默认结果中的每个字段都必须来自该渠道允许字段集合
        assert set(result["field_basis"]).issubset(allowed)
        assert result["field_basis"] == ["name", "phone", "id_no", "email"]
        assert all(set(row.keys()).issubset(allowed) for row in result["records"])

        # 登记一个收窄的默认发布范围：空配置只交付默认字段
        narrow_channel = _unique(CHANNEL_SERVICE)
        svc.upsert_channel_scope(
            narrow_channel, GOLDEN_MODEL,
            ["name", "phone", "id_no", "email"],
            default_publish_fields=["name", "phone"],
        )
        narrow_result = svc.deliver_shared_fields(narrow_channel, GOLDEN_MODEL)
        assert narrow_result["field_basis"] == ["name", "phone"]
        assert narrow_result["scope_source"] == "默认发布范围"

    def test_revocation_during_query_and_ledger_traceability(self):
        """一轮名单查询中撤回证件字段：当次凭证保留旧口径，之后交付不再含证件。"""
        _prepare_golden_records()
        service_channel, _, svc = _setup_two_channels()

        # 当次名单查询（撤回前）：证件字段正常交付
        before = svc.deliver_shared_fields(service_channel, GOLDEN_MODEL)
        assert before["ok"] is True
        before_ledger_id = before["ledger_id"]
        assert "id_no" in before["field_basis"]
        assert "id_no" in before["records"][0]

        # 撤回进行中：客服渠道证件字段权限被撤回
        revoke = svc.revoke_channel_fields(
            service_channel, GOLDEN_MODEL, ["id_no"],
        )
        assert revoke["ok"] is True
        assert "id_no" not in revoke["remaining_fields"]

        # 撤回后的实际交付：不再含证件字段
        after = svc.deliver_shared_fields(service_channel, GOLDEN_MODEL)
        assert after["ok"] is True
        assert "id_no" not in after["field_basis"]
        assert all("id_no" not in row for row in after["records"])

        # 数据负责人核对凭证：当次凭证仍保留撤回前的完整字段口径
        ledger_rows = svc.query_delivery_ledger(service_channel, GOLDEN_MODEL)
        ledger_ids = {row["id"] for row in ledger_rows}
        assert before_ledger_id in ledger_ids
        before_ledger = store.find(LEDGER_MODULE, before_ledger_id)
        assert "id_no" in before_ledger["授权字段口径"]
        assert before_ledger["范围版本"] == "v1"
        after_ledger = store.find(LEDGER_MODULE, after["ledger_id"])
        assert "id_no" not in after_ledger["授权字段口径"]
        assert after_ledger["范围版本"] == "v2"

    def test_only_same_basis_rows_can_merge(self):
        """受同一字段授权口径约束的交付行才可合并，撤回前后不可合并。"""
        _prepare_golden_records()
        service_channel, _, svc = _setup_two_channels()
        before = svc.deliver_shared_fields(service_channel, GOLDEN_MODEL)
        svc.revoke_channel_fields(service_channel, GOLDEN_MODEL, ["id_no"])
        after = svc.deliver_shared_fields(service_channel, GOLDEN_MODEL)

        # 撤回前后口径不同：禁止合并
        check = svc.can_merge_delivery_rows(before["ledger_id"], after["ledger_id"])
        assert check["ok"] is True and check["mergeable"] is False
        assert set(check["basis_a"]) != set(check["basis_b"])

        # 同口径的两次交付：允许合并
        again = svc.deliver_shared_fields(service_channel, GOLDEN_MODEL)
        same_check = svc.can_merge_delivery_rows(after["ledger_id"], again["ledger_id"])
        assert same_check["mergeable"] is True
        assert set(same_check["shared_basis"]) == {"name", "phone", "email"}

        # 不同黄金模型的凭证不可合并
        other = svc.upsert_channel_scope(
            _unique(CHANNEL_SERVICE), "other_model", ["name"],
        )
        other_delivery = svc.deliver_shared_fields(
            other["渠道"], "other_model", requested_fields=["name"],
        )
        cross_model = svc.can_merge_delivery_rows(
            again["ledger_id"], other_delivery["ledger_id"],
        )
        assert cross_model["ok"] is False

    def test_mdm_api_channel_query_and_empty_config_security(self):
        """既有发布接口：显式字段对照响应，空配置重查由渠道允许字段集合解释。"""
        _prepare_golden_records()
        service_channel, _, svc = _setup_two_channels()
        api_svc = MdmApiService()
        api_id = self._create_published_api(api_svc, return_fields="name,phone")

        # 显式选择两个允许字段：响应数据与选择一致
        explicit = api_svc.query_golden_records_by_api(
            api_id, channel=service_channel,
        )
        assert explicit["ok"] is True
        assert explicit["count"] == 2
        assert set(explicit["records"][0].keys()) == {"name", "phone"}

        # 运营清空返回字段后重查：默认结果必须能从渠道允许字段集合解释
        api_def = api_svc.get_entry(api_id)
        api_def["返回字段"] = ""
        empty_config = api_svc.query_golden_records_by_api(
            api_id, channel=service_channel,
        )
        assert empty_config["ok"] is True
        assert empty_config["scope_source"] == "默认发布范围"
        allowed = set(empty_config["allowed_fields"])
        assert set(empty_config["field_basis"]).issubset(allowed)
        assert all(set(row.keys()).issubset(allowed) for row in empty_config["records"])
        # 整份实体模型未被泄露：未授权/未登记字段不出现在响应中
        assert all("secret_level" not in row for row in empty_config["records"])

        # 未登记渠道不能取数
        unknown = api_svc.query_golden_records_by_api(
            api_id, channel="未登记渠道",
        )
        assert unknown["ok"] is False

    def test_legacy_call_without_channel_never_leaks_full_entity(self):
        """历史无渠道调用：空返回字段不再返回整份实体模型。"""
        _prepare_golden_records()
        api_svc = MdmApiService()
        api_id = self._create_published_api(api_svc, return_fields="")
        result = api_svc.query_golden_records_by_api(api_id)
        assert result["ok"] is True
        allowed_model_fields = set(GoldenRecordService.LIST_FIELDS)
        for row in result["records"]:
            assert set(row.keys()).issubset(allowed_model_fields)
            assert "secret_level" not in row
        exported = api_svc.export_api_response(api_id)
        for row in exported:
            assert set(row.keys()).issubset(allowed_model_fields)

    @staticmethod
    def _create_published_api(api_svc: MdmApiService, return_fields: str) -> int:
        rows = store.rows(api_svc.MODULE)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "接口编号": f"FD-API-{len(rows) + 1:04d}",
            "实体模型": GOLDEN_MODEL,
            "查询字段": "name",
            "返回字段": return_fields,
            "缓存时长": "60",
            "限流阈值": "1000",
            "调用量统计": "0",
            "接口版本": "1.0",
            "status": "已发布",
            "pending": False,
            "abnormal": False,
        }
        rows.append(entry)
        return entry["id"]


if __name__ == "__main__":
    runner = TestFieldDeliveryChain()
    tests = [
        ("渠道范围互相独立", runner.test_channel_scopes_are_independent),
        ("显式字段与渠道权限取交集", runner.test_explicit_fields_intersect_channel_permissions),
        ("空配置回落默认发布范围", runner.test_empty_config_falls_back_to_default_publish_scope),
        ("查询中撤回证件字段且凭证可追溯", runner.test_revocation_during_query_and_ledger_traceability),
        ("同口径行才可合并", runner.test_only_same_basis_rows_can_merge),
        ("发布接口渠道查询与空配置安全", runner.test_mdm_api_channel_query_and_empty_config_security),
        ("无渠道历史调用不泄露整份实体", runner.test_legacy_call_without_channel_never_leaks_full_entity),
    ]
    passed = failed = 0
    for name, test_fn in tests:
        try:
            test_fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {name} -> {exc}")
            failed += 1
    print(f"\n{'=' * 60}")
    print(f"结果: {passed}/{len(tests)} 通过, {failed} 失败")
    assert failed == 0, f"{failed} 条共享字段交付测试失败！"
