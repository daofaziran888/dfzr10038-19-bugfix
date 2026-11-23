"""衍生工程领域服务。

在通用骨架之上承载真实业务差异：
- 衍生规则的输入依赖以「特征身份（特征编号）」为准，显示名仅作展示；
- 输入特征检索按精确词条匹配，前缀重叠（如 spend_7d 与 spend_7d_refund）不构成依赖；
- 只有显示名的历史规则在启效时解析身份，重名无法定责的规则保留草稿并登记核实待办，
  待核实规则仍可被正常检索。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 输入特征/特征列表允许的多值分隔符：分号、逗号（中英文）、空白
_TOKEN_SPLIT_RE = re.compile(r"[;,，、\s]+")

# 规则行上承载输入身份的结构化字段（历史数据没有这些字段，按缺省值兼容）
IDENTITY_FIELD = "输入特征标识"
PENDING_FIELD = "待核实输入"
VERIFY_FIELD = "输入核实状态"
WINDOW_FIELD = "窗口登记"


def split_tokens(text: Any) -> list[str]:
    """把多值文本拆成精确词条；空词条自动丢弃。"""
    if text is None:
        return []
    if isinstance(text, (list, tuple, set)):
        tokens: list[str] = []
        for part in text:
            tokens.extend(token for token in _TOKEN_SPLIT_RE.split(str(part).strip()) if token)
        return tokens
    return [token for token in _TOKEN_SPLIT_RE.split(str(text).strip()) if token]


def feature_id_index() -> dict[str, dict[str, Any]]:
    """全部注册记录按特征编号建索引（含草稿，编号即身份，存在即可核对来源）。"""
    return {
        str(row.get("特征编号", "")).strip(): row
        for row in store.rows("feature_reg")
        if str(row.get("特征编号", "")).strip()
    }


def feature_name_index() -> dict[str, list[dict[str, Any]]]:
    """注册库全部特征按显示名分组（含草稿）。

    同名跨业务域时会出现多条：只要登记了两条同名词条，仅凭显示名就无法定责，
    与其中一条是否已审核通过无关；是否可启效再单独看身份记录的状态。
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for row in store.rows("feature_reg"):
        name = str(row.get("特征名称", "")).strip()
        if name:
            index.setdefault(name, []).append(row)
    return index


def rule_input_token_set(rule_row: dict[str, Any]) -> set[str]:
    """规则当前可用于精确匹配的全部输入词条：显示名词条 + 已登记身份编号。"""
    tokens = set(split_tokens(rule_row.get("输入特征", "")))
    tokens.update(str(ident) for ident in rule_row.get(IDENTITY_FIELD, []) if ident)
    return tokens


def normalize_identifiers(raw: Any) -> list[str]:
    """兼容列表/分隔字符串形式的身份编号入参。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [str(value).strip() for value in raw.values() if str(value).strip()]
    return split_tokens(raw)


class DeriveRuleService(BaseModuleService):
    MODULE = "derive_rule"
    ENTITY = "衍生规则"
    OPERATION_TERM = "计算"
    LIST_FIELDS = ["规则编号", "输入特征", "衍生函数", "输出特征", "计算粒度", "依赖表", "刷新策略", "最近产出"]
    REQUIRED_FIELDS = ["规则编号", "输入特征", "衍生函数"]
    STATUS_ORDER = ["草稿", "生效中", "计算异常", "已停用"]
    ACTION_RULES = {"启效规则": "生效中", "标记异常": "计算异常", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if missing:
            return entry, missing
        assert entry is not None
        explicit = normalize_identifiers(values.get(IDENTITY_FIELD) or values.get("输入特征编号"))
        tokens = split_tokens(values.get("输入特征", ""))
        id_index = feature_id_index()
        # 只有指向「已注册」特征的编号才算已核实；草稿身份可登记但仍待核实
        verified = [ident for ident in explicit
                    if (row := id_index.get(ident)) is not None and row.get("status") == "已注册"]
        pending_tokens: list[str] = []
        if verified:
            # 身份明确的规则：显示名只是标签，未被编号覆盖的词条仍需核实
            covered_names = {
                str(id_index[ident].get("特征名称", "")).strip()
                for ident in verified
            }
            pending_tokens = [token for token in tokens if token not in covered_names]
        else:
            # 历史登记方式：只有显示名，留待启效时按注册名解析
            pending_tokens = list(tokens)
        # 指向草稿/不存在身份的编号同样列入待核实
        pending_tokens.extend(ident for ident in explicit if ident not in verified)
        entry[IDENTITY_FIELD] = list(dict.fromkeys(explicit))
        entry[PENDING_FIELD] = list(dict.fromkeys(pending_tokens))
        entry[VERIFY_FIELD] = "已核实" if verified and not pending_tokens else "待核实"
        entry[WINDOW_FIELD] = []
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "启效规则":
            return self._activate_with_verification(entry_id)
        return super().run_action(entry_id, action)

    def _activate_with_verification(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """手动启效与注册联动共用一套来源核对：不可核对则维持草稿。"""
        # 延迟导入避免 data_flow ↔ derive_rule 模块级循环依赖
        from app.services.data_flow import (
            _ensure_identity_followup,
            _verify_rule_inputs,
            _window_compute_for_derived,
            _close_identity_followup,
        )

        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        feat_id = str(entry.get(IDENTITY_FIELD, [""])[0]) if entry.get(IDENTITY_FIELD) else ""
        verification = _verify_rule_inputs(
            entry, triggered_feat_no=feat_id,
            triggered_feat_name=(split_tokens(entry.get("输入特征", "")) or [""])[0],
        )
        rule_no = str(entry.get("规则编号", ""))
        if not verification["ready"]:
            entry[PENDING_FIELD] = verification["pending_tokens"]
            entry[VERIFY_FIELD] = "待核实"
            _ensure_identity_followup(rule_no, verification["reasons"], [])
            return entry, (
                f"规则 {rule_no} 输入来源无法核对，维持草稿："
                f"{'；'.join(verification['reasons'])}"
            )

        result, message = super().run_action(entry_id, "启效规则")
        if result is not None:
            result[IDENTITY_FIELD] = verification["identifiers"]
            result[PENDING_FIELD] = []
            result[VERIFY_FIELD] = "已核实"
            _close_identity_followup(rule_no)
            _window_compute_for_derived(result, verification["identifiers"][0], [])
        return result, message

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = dict(filters or {})
        # 输入特征列按精确词条匹配，避免前缀重叠串号（spend_7d 不命中 spend_7d_refund 专属规则）
        input_filter = filters.pop("输入特征", None)
        rows, _ = super().list_entries(
            filters=filters, keyword=None, status=status, page=1, size=1_000_000)
        if input_filter:
            wanted = split_tokens(input_filter) or [input_filter.strip()]
            rows = [
                row for row in rows
                if any(token in rule_input_token_set(row) for token in wanted if token)
            ]
        if keyword:
            # 关键字沿用基类语义，只匹配首列规则编号；待核实/草稿规则同样可见
            rows = [
                row for row in rows
                if keyword.casefold() in str(row.get("规则编号", "")).casefold()
            ]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total
