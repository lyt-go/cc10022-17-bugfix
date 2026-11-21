"""参考数据导入与字段清洗消费链路测试。

覆盖题述场景：混用类别的同一份字典批量导入时，分类去重、同码重送幂等、
异内容同码标记冲突并保留已确认数据；导入后字段标准化清洗按类别取用
分组参考数据，把省份别名（沪）写回标准编码。
"""
from __future__ import annotations

import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.field_cleanse import FieldCleanseService
from app.services.mdm_reference import MdmReferenceService
from app.store import store

# 同一批混类字典：省份 01 本类从未录过；付款 01 与存量名称冲突。
MIXED_BATCH = [
    {"数据类别": "省份", "标准编码": "01", "标准名称": "上海市",
     "别名列表": "沪,上海", "映射关系": "CN-31", "有效日期": "2026-12-31"},
    {"数据类别": "付款", "标准编码": "01", "标准名称": "货到付款",
     "别名列表": "COD", "映射关系": "PAY-COD", "有效日期": "2026-12-31"},
]


def _snapshot(module: str) -> list[dict]:
    return [dict(row) for row in store.rows(module)]


def _restore(module: str, rows: list[dict]) -> None:
    store.rows(module)[:] = rows


class TestReferenceImportCleanse:
    """参考数据分类去重导入 -> 导入报告核对 -> 清洗按类别消费。"""

    def setup_method(self):
        self._ref_backup = _snapshot("mdm_reference")
        self._cleanse_backup = _snapshot("field_cleanse")

    def teardown_method(self):
        _restore("mdm_reference", self._ref_backup)
        _restore("field_cleanse", self._cleanse_backup)

    def test_seed_grouped_reference_data(self):
        """分组参考导入数据已写入：付款 01 在册，省份 01 缺失，沪暂不可匹配。"""
        ref_svc = MdmReferenceService()
        payment = ref_svc.lookup_reference_value("付款", "01")
        assert payment is not None and payment["standard_name"] == "现金支付"
        province_codes = [r["标准编码"] for r in ref_svc.query_reference_by_category("省份")]
        assert "01" not in province_codes, "省份 01 在导入前应缺失"
        assert ref_svc.lookup_reference_value("省份", "沪") is None

    def test_mixed_batch_import_report(self):
        """同一批两种问题：省份 01 首次录入，付款 01 与存量名称冲突并保留已确认数据。"""
        ref_svc = MdmReferenceService()
        report = ref_svc.import_reference_data("默认类别", MIXED_BATCH)
        assert report["total"] == 2
        assert report["imported"] == 1, f"省份 01 应导入成功: {report}"
        assert report["skipped"] == 0
        assert len(report["conflicts"]) == 1, f"付款 01 应标记冲突: {report}"

        by_code = {(r["数据类别"], r["标准编码"]): r for r in report["results"]}
        assert by_code[("省份", "01")]["result"] == "imported"
        conflict = by_code[("付款", "01")]
        assert conflict["result"] == "conflict"
        assert conflict["存量名称"] == "现金支付"
        assert conflict["提交名称"] == "货到付款"

        # 冲突行不覆盖已确认数据
        payment = ref_svc.lookup_reference_value("付款", "01")
        assert payment["standard_name"] == "现金支付"
        # 跨类别同码不再误判重复：省份 01 已写入
        province = ref_svc.lookup_reference_value("省份", "01")
        assert province is not None and province["standard_name"] == "上海市"
        assert "01" in [r["标准编码"] for r in ref_svc.query_reference_by_category("省份")]

    def test_same_category_resend_is_idempotent(self):
        """同一类别下同码重送幂等：内容一致跳过，不重复写入。"""
        ref_svc = MdmReferenceService()
        first = ref_svc.import_reference_data("省份", [MIXED_BATCH[0]])
        assert first["imported"] == 1
        again = ref_svc.import_reference_data("省份", [MIXED_BATCH[0]])
        assert again["imported"] == 0
        assert again["skipped"] == 1
        assert again["results"][0]["result"] == "skipped"
        province_rows = [r for r in store.rows("mdm_reference")
                         if r.get("数据类别") == "省份" and r.get("标准编码") == "01"]
        assert len(province_rows) == 1, "幂等重送不应产生重复行"

    def test_cross_category_same_code_not_duplicate(self):
        """不同类别共用同一标准编码不判重复，各类别独立成组。"""
        ref_svc = MdmReferenceService()
        result = ref_svc.import_reference_data("省份", [MIXED_BATCH[0]])
        assert result["imported"] == 1, "付款 01 已存在不应拦截省份 01"
        uniqueness = ref_svc.validate_reference_uniqueness("省份")
        assert uniqueness["duplicate_codes"] == 0
        payment = ref_svc.lookup_reference_value("付款", "01")
        province = ref_svc.lookup_reference_value("省份", "01")
        assert payment["standard_code"] == province["standard_code"] == "01"
        assert payment["standard_name"] != province["standard_name"]

    def test_cleanse_consumes_reference_by_category(self):
        """清洗入口按类别取用参考数据：省份别名沪写回标准编码 01。"""
        ref_svc = MdmReferenceService()
        cleanse_svc = FieldCleanseService()
        # 导入前不可匹配
        assert cleanse_svc.cleanse_with_reference("省份", "沪")["ok"] is False
        ref_svc.import_reference_data("省份", [MIXED_BATCH[0]])

        direct = cleanse_svc.cleanse_with_reference("省份", "沪")
        assert direct["ok"] is True
        assert direct["cleansed"] == "01"
        assert direct["standard_name"] == "上海市"

        # 清洗规则通过 修正字典 ref:<类别> 消费分组参考数据
        rule = {
            "id": 900001,
            "status": "已生效",
            "pending": False,
            "abnormal": False,
            "规则编号": "FIEL-9001",
            "实体模型": "customer",
            "目标字段": "省份",
            "标准化函数": "trim",
            "修正字典": "ref:省份",
            "异常处理": "保留原值",
            "先后顺序": "1",
            "规则版本": "1.0",
        }
        store.rows("field_cleanse").append(rule)
        via_rule = cleanse_svc.cleanse_field_value(rule["id"], " 沪 ")
        assert via_rule["ok"] is True
        assert via_rule["cleansed"] == "01"

        records = [{"省份": "沪"}, {"省份": "粤"}, {"省份": "未知"}]
        cleaned = cleanse_svc.batch_cleanse_records(rule["id"], records, "省份")
        assert [r["省份"] for r in cleaned] == ["01", "03", "未知"]


if __name__ == "__main__":
    runner = TestReferenceImportCleanse()
    tests = [
        ("分组参考数据就绪", runner.test_seed_grouped_reference_data),
        ("混类批次导入报告", runner.test_mixed_batch_import_report),
        ("同类同码重送幂等", runner.test_same_category_resend_is_idempotent),
        ("跨类别同码不判重", runner.test_cross_category_same_code_not_duplicate),
        ("清洗按类别消费参考数据", runner.test_cleanse_consumes_reference_by_category),
    ]
    passed = failed = 0
    for name, test_fn in tests:
        runner.setup_method()
        try:
            test_fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {name} -> {exc}")
            failed += 1
        finally:
            runner.teardown_method()
    print(f"\n{'=' * 60}")
    print(f"结果: {passed}/{len(tests)} 通过, {failed} 失败")
    assert failed == 0, f"{failed} 条参考数据链路测试失败！"
