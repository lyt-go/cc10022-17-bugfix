"""参考数据领域服务：标准编码入库、字典映射校验与参考版本管理。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 参与幂等/冲突判定的内容字段：同类别同编码下这些字段完全一致才算同一条数据。
CONTENT_FIELDS = ["标准名称", "别名列表", "映射关系", "有效日期"]


def _split_terms(raw: Any) -> list[str]:
    """把逗号（含中文逗号）分隔的字典项拆成去空白列表。"""
    return [
        term.strip()
        for term in str(raw or "").replace("，", ",").split(",")
        if term.strip()
    ]


class MdmReferenceService(BaseModuleService):
    MODULE = "mdm_reference"
    ENTITY = "参考数据"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["数据编号", "数据类别", "标准编码", "标准名称", "别名列表", "映射关系", "有效日期", "数据版本"]
    REQUIRED_FIELDS = ["数据编号", "数据类别", "标准编码"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    @staticmethod
    def _find_in_category(rows: list[dict[str, Any]], category: str, code: str) -> dict[str, Any] | None:
        """按 (数据类别, 标准编码) 定位存量参考数据，类别之间互不影响。"""
        for row in rows:
            if row.get("数据类别") == category and str(row.get("标准编码", "")).strip() == code:
                return row
        return None

    @staticmethod
    def _same_content(existing: dict[str, Any], entry_data: dict[str, Any]) -> bool:
        """比对同码行内容：全部内容字段一致才视为同一条数据（幂等重送）。"""
        return all(
            str(existing.get(field, "")).strip() == str(entry_data.get(field, "")).strip()
            for field in CONTENT_FIELDS
        )

    @staticmethod
    def _next_ref_id(rows: list[dict[str, Any]], category: str) -> str:
        """生成类别内唯一的数据编号，避免与存量编号冲突。"""
        used = {row.get("数据编号") for row in rows}
        seq = sum(1 for row in rows if row.get("数据类别") == category) + 1
        ref_id = f"REF-{category}-{seq:04d}"
        while ref_id in used:
            seq += 1
            ref_id = f"REF-{category}-{seq:04d}"
        return ref_id

    def import_reference_data(self, category: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """导入参考数据：按数据类别分组去重，逐条写入并输出导入报告。

        同一份参考字典可能混用多个数据类别，每行以自身「数据类别」为准
        （缺省回退到批次类别）。唯一性按 (数据类别, 标准编码) 判定：

        - 本类别从未录过该编码 -> 写入分组参考数据；
        - 本类别已有同码且内容一致 -> 幂等跳过，不重复写入；
        - 本类别已有同码但内容不同 -> 明确标记冲突，保留已确认的存量数据。

        返回导入报告：imported/skipped/conflicts/total 汇总与逐条 results，
        供清洗人员核对后决定能否开始转换含别名的行。
        """
        rows = store.rows(self.MODULE)
        imported, skipped = 0, 0
        conflicts: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for entry_data in entries:
            entry_category = str(entry_data.get("数据类别") or category).strip()
            code = str(entry_data.get("标准编码", "")).strip()
            if not code:
                # 导入参考数据：缺标准编码的行直接拒绝并写入报告
                results.append({
                    "数据类别": entry_category,
                    "标准编码": code,
                    "result": "invalid",
                    "message": "缺少标准编码，已拒绝导入",
                })
                continue
            existing = self._find_in_category(rows, entry_category, code)
            if existing is not None:
                if self._same_content(existing, entry_data):
                    skipped += 1
                    # 同类别同码重送：内容一致，幂等跳过
                    results.append({
                        "数据类别": entry_category,
                        "标准编码": code,
                        "result": "skipped",
                        "数据编号": existing.get("数据编号"),
                        "message": "同类别同编码且内容一致，幂等跳过",
                    })
                else:
                    # 同类别同码但内容不同：标记冲突，保留已确认数据
                    conflict = {
                        "数据类别": entry_category,
                        "标准编码": code,
                        "result": "conflict",
                        "数据编号": existing.get("数据编号"),
                        "存量名称": existing.get("标准名称"),
                        "提交名称": entry_data.get("标准名称", ""),
                        "message": (
                            f"{entry_category}/{code} 与存量名称"
                            f"「{existing.get('标准名称')}」冲突，已保留已确认数据"
                        ),
                    }
                    conflicts.append(conflict)
                    results.append(conflict)
                continue
            ref_id = self._next_ref_id(rows, entry_category)
            ref_entry = {
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
                "数据编号": ref_id,
                "数据类别": entry_category,
                "标准编码": code,
                "标准名称": entry_data.get("标准名称", ""),
                "别名列表": entry_data.get("别名列表", ""),
                "映射关系": entry_data.get("映射关系", ""),
                "有效日期": entry_data.get("有效日期", ""),
                "数据版本": entry_data.get("数据版本", "") or "1.0",
                "status": "已生效",
                "pending": False,
                "abnormal": False,
            }
            rows.append(ref_entry)
            imported += 1
            # 导入参考数据：按类别分组写入mdm_reference供字段清洗使用
            results.append({
                "数据类别": entry_category,
                "标准编码": code,
                "result": "imported",
                "数据编号": ref_id,
                "message": f"{entry_category}/{code} 本类别首次录入，已写入参考数据",
            })
        return {
            "imported": imported,
            "skipped": skipped,
            "conflicts": conflicts,
            "total": len(entries),
            "results": results,
        }

    def lookup_reference_value(self, category: str, code: str) -> dict[str, Any] | None:
        """查询参考数据：按数据类别和标准编码查找标准名称。

        只取用该类别的已生效参考数据，按标准编码、标准名称或别名
        检索对应的标准名称、别名和映射关系，用于字段标准化转换。
        """
        rows = store.rows(self.MODULE)
        key = str(code).strip()
        for row in rows:
            if row.get("数据类别") != category or row.get("status") != "已生效":
                continue
            aliases = _split_terms(row.get("别名列表", ""))
            if key in (
                str(row.get("标准编码", "")).strip(),
                str(row.get("标准名称", "")).strip(),
                *aliases,
            ):
                # 查询参考数据：按编码查找标准名称和映射关系
                return {
                    "standard_code": row.get("标准编码"),
                    "standard_name": row.get("标准名称"),
                    "aliases": aliases,
                    "mappings": _split_terms(row.get("映射关系", "")),
                    "version": row.get("数据版本"),
                }
        return None

    def validate_reference_uniqueness(self, category: str) -> dict[str, Any]:
        """校验参考数据的编码唯一性：检查同类别下是否有重复标准编码。

        读取mdm_reference存储，按数据类别分组检查标准编码
        是否唯一，输出重复编码列表和校验报告。
        """
        rows = store.rows(self.MODULE)
        cat_rows = [r for r in rows if r.get("数据类别") == category]
        seen: dict[str, list[str]] = {}
        for row in cat_rows:
            code = row.get("标准编码", "")
            if code not in seen:
                seen[code] = []
            seen[code].append(row.get("数据编号", ""))
        duplicates = {k: v for k, v in seen.items() if len(v) > 1}
        # 校验参考数据唯一性：检测同类别下重复编码
        return {
            "category": category,
            "total": len(cat_rows),
            "unique_codes": len(seen),
            "duplicate_codes": len(duplicates),
            "duplicates": duplicates,
            "unique_pct": round((len(seen) - len(duplicates)) / max(len(seen), 1) * 100, 2),
        }

    def query_reference_by_category(self, category: str) -> list[dict[str, Any]]:
        """查询指定数据类别的所有已生效参考数据。

        遍历参考数据存储，筛选已生效且匹配数据类别的记录，
        按标准编码排序返回参考数据列表。
        """
        rows = store.rows(self.MODULE)
        refs = [
            row for row in rows
            if row.get("数据类别") == category and row.get("status") == "已生效"
        ]
        # 查询参考数据：按数据类别和编码排序
        refs.sort(key=lambda r: r.get("标准编码", ""))
        return refs

    def merge_reference_versions(self, category: str, old_version: str,
                                   new_version: str) -> dict[str, Any]:
        """归并参考数据版本：将旧版本参考数据迁移到新版本。

        读取参考数据存储，将指定类别的旧版本数据归并到
        新版本，更新数据版本号并归档旧版本记录。
        """
        rows = store.rows(self.MODULE)
        migrated = 0
        for row in rows:
            if row.get("数据类别") == category and row.get("数据版本") == old_version:
                row["数据版本"] = new_version
                row["version_migrated_at"] = datetime.datetime.utcnow().isoformat()
                migrated += 1
        # 归并参考数据版本：将旧版本归档并迁移到新版本
        return {"category": category, "migrated_count": migrated, "from": old_version, "to": new_version}
