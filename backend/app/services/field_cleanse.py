"""字段清洗领域服务：标准化函数执行、修正字典匹配与字段完整性校验。"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class FieldCleanseService(BaseModuleService):
    MODULE = "field_cleanse"
    ENTITY = "清洗规则"
    OPERATION_TERM = "清洗"
    LIST_FIELDS = ["规则编号", "实体模型", "目标字段", "标准化函数", "修正字典", "异常处理", "先后顺序", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "实体模型", "目标字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    STANDARD_FUNCTIONS: dict[str, Any] = {
        "trim": lambda v: v.strip() if isinstance(v, str) else v,
        "lower": lambda v: v.lower() if isinstance(v, str) else v,
        "upper": lambda v: v.upper() if isinstance(v, str) else v,
        "remove_spaces": lambda v: re.sub(r"\s+", "", v) if isinstance(v, str) else v,
        "format_phone": lambda v: re.sub(r"[^\d+]", "", v) if isinstance(v, str) else v,
        "format_id": lambda v: v.replace("-", "").replace(" ", "").upper() if isinstance(v, str) else v,
    }

    def cleanse_with_reference(self, category: str, raw_value: str) -> dict[str, Any]:
        """按数据类别取用分组参考数据，把别名/非标值写回标准编码。

        查询参考数据管理中该类别的已生效字典，命中标准编码、标准名称
        或别名（如省份别名「沪」）时，返回对应的标准编码作为清洗结果。
        """
        from app.services.mdm_reference import MdmReferenceService

        ref = MdmReferenceService().lookup_reference_value(category, raw_value)
        if ref is None:
            return {
                "ok": False,
                "reason": f"参考数据未匹配：{category}/{raw_value}",
                "category": category,
                "raw": raw_value,
                "cleansed": raw_value,
            }
        # 按类别取用参考数据：别名写回该字段对应的标准编码
        return {
            "ok": True,
            "category": category,
            "raw": raw_value,
            "cleansed": ref["standard_code"],
            "standard_code": ref["standard_code"],
            "standard_name": ref["standard_name"],
            "applied_functions": [f"reference:{category}"],
        }

    def cleanse_field_value(self, rule_id: int, raw_value: str) -> dict[str, Any]:
        """清洗字段值：按清洗规则执行标准化函数和修正字典映射。

        读取字段清洗规则，对原始字段值依次应用标准化函数和修正字典，
        校验清洗结果并返回清洗前值、清洗后值和采用的标准化函数。
        """
        rule = self.get_entry(rule_id)
        if not rule or rule.get("status") != "已生效":
            return {"ok": False, "reason": "清洗规则不可用", "raw": raw_value, "cleansed": raw_value}
        value = raw_value
        applied = []
        func_names = [f.strip() for f in rule.get("标准化函数", "").split(",") if f.strip()]
        for func_name in func_names:
            if func_name in self.STANDARD_FUNCTIONS:
                value = self.STANDARD_FUNCTIONS[func_name](value)
                applied.append(func_name)
        # 清洗字段值：应用标准化函数和修正字典完成字段转换
        dict_entries = [d.strip() for d in rule.get("修正字典", "").split(",") if ":" in d]
        for entry in dict_entries:
            wrong, correct = entry.split(":", 1)
            if wrong.strip() == "ref":
                # 修正字典声明 ref:<数据类别> 时，按类别取用参考数据写回标准编码
                ref_result = self.cleanse_with_reference(correct.strip(), str(value))
                if ref_result["ok"]:
                    value = ref_result["cleansed"]
                    applied.append(f"ref:{correct.strip()}->{value}")
                continue
            if isinstance(value, str) and wrong.strip() in value:
                value = value.replace(wrong.strip(), correct.strip())
                applied.append(f"dict:{wrong.strip()}->{correct.strip()}")
        return {"ok": True, "raw": raw_value, "cleansed": value, "applied_functions": applied}

    def batch_cleanse_records(self, rule_id: int, records: list[dict[str, Any]],
                               target_field: str) -> list[dict[str, Any]]:
        """批量清洗记录指定字段：遍历记录列表对目标字段执行清洗规则。

        读取清洗规则，对传入记录列表逐条清洗目标字段值，
        校验字段完整性并标准化字段内容，返回清洗后的记录列表。
        """
        rule = self.get_entry(rule_id)
        if not rule:
            return records
        for record in records:
            if target_field in record:
                result = self.cleanse_field_value(rule_id, str(record[target_field]))
                if result["ok"]:
                    # 清洗目标字段：标准化字段值并更新记录
                    record[target_field] = result["cleansed"]
                    record["_cleansed_fields"] = record.get("_cleansed_fields", [])
                    record["_cleansed_fields"].append(target_field)
        return records

    def validate_field_completeness(self, entity_model: str, records: list[dict[str, Any]],
                                     required_fields: list[str]) -> dict[str, Any]:
        """验证字段完整性：检查清洗后每条记录的必填字段是否均已填充。

        遍历清洗后的记录列表，逐一验证必填字段完整性，
        统计完备率、缺失字段分布和异常记录列表。
        """
        total = len(records)
        complete = 0
        field_missing: dict[str, int] = {}
        incomplete_records: list[dict[str, Any]] = []
        for i, record in enumerate(records):
            missing = [f for f in required_fields if not str(record.get(f, "")).strip()]
            if missing:
                incomplete_records.append({"index": i, "missing_fields": missing})
                for f in missing:
                    field_missing[f] = field_missing.get(f, 0) + 1
            else:
                complete += 1
        # 验证字段完整性：统计完备率和缺失字段分布
        completeness = round(complete / max(total, 1) * 100, 2)
        return {
            "entity_model": entity_model,
            "total_records": total,
            "complete_records": complete,
            "completeness_pct": completeness,
            "field_missing_distribution": field_missing,
            "incomplete_detail": incomplete_records,
        }

    def query_cleanse_rules_for_model(self, entity_model: str) -> list[dict[str, Any]]:
        """查询指定实体模型的所有已生效清洗规则，按先后顺序排列。

        遍历清洗规则存储，筛选已生效且匹配实体模型的规则，
        按先后顺序字段排序返回清洗规则执行序列。
        """
        rows = store.rows(self.MODULE)
        rules = [
            row for row in rows
            if row.get("实体模型") == entity_model and row.get("status") == "已生效"
        ]
        # 查询清洗规则并按先后顺序排序
        rules.sort(key=lambda r: int(r.get("先后顺序", "0") or "0"))
        return rules

    def standardize_field_values(self, records: list[dict[str, Any]],
                                  entity_model: str) -> list[dict[str, Any]]:
        """标准化字段值：对实体模型的所有记录执行完整清洗流水线。

        查询该实体模型的所有清洗规则，按先后顺序依次对每条记录的
        各目标字段执行标准化函数和修正字典转换，完成全链路清洗。
        """
        rules = self.query_cleanse_rules_for_model(entity_model)
        for rule in rules:
            target_field = rule.get("目标字段", "")
            if not target_field:
                continue
            for record in records:
                if target_field in record:
                    result = self.cleanse_field_value(rule["id"], str(record[target_field]))
                    if result["ok"]:
                        # 标准化字段值：执行完整清洗流水线并更新记录
                        record[target_field] = result["cleansed"]
        return records