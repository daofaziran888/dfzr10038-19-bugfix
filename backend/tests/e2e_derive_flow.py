"""端到端核验脚本（仅标准库）：特征注册审核 → 衍生规则启效 → 时间窗口联动。

覆盖验收点：
1. 审核 spend_7d 时，只依赖 spend_7d_refund 的规则不会被前缀串号启效；
2. 规则输入依赖按特征身份（特征编号）/精确词条关联，多输入规则需整套输入可核对；
3. 同名跨业务域的老规则启效前挂起、登记核实待办，且仍可被检索；
4. 启效规则的输出精确匹配窗口特征列表，已启动窗口的触发依据不被替换；
5. 链路回执与衍生清单使用同一规则编号；
6. 历史规则补齐身份后可启效并闭环待办；前缀相似规则始终草稿；
7. 精确单名老记录与历史多输入规则兼容启效。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/api"
failures: list[str] = []


def call(method: str, path: str, payload: dict | None = None) -> dict:
    if "?" in path:
        head, query = path.split("?", 1)
        path = head + "?" + urllib.parse.quote(query, safe="=&?")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise AssertionError(f"HTTP {exc.code} {path}: {exc.read().decode()}") from exc


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


def create(module: str, values: dict) -> dict:
    result = call("POST", f"/{module}", {"values": values})
    assert result["ok"], result["message"]
    return result["entry"]


def audit(entry_id: int) -> dict:
    return call("POST", f"/feature_reg/{entry_id}/actions", {"action": "审核注册"})


def find_rule(rule_no: str) -> dict:
    rows = call("GET", "/derive_rule?size=200")["items"]
    matches = [r for r in rows if r.get("规则编号") == rule_no]
    assert matches, f"规则 {rule_no} 检索不到（待核实规则也应可搜索）"
    return matches[0]


def find_window(template_no: str) -> dict:
    rows = call("GET", "/time_window?size=200")["items"]
    return next(r for r in rows if r.get("模板编号") == template_no)


# 场景脚本依赖干净的内存仓库：若已存在本脚本数据则直接拒绝，避免重复运行互相污染
existing = call("GET", "/derive_rule?size=200")["items"]
if any(r.get("规则编号") == "DERI-T-A" for r in existing):
    print("检测到上一轮场景数据，请重启后端获得干净内存仓库后再运行")
    sys.exit(2)


# ---------------------------------------------------------------- 登记特征
f_spend = create("feature_reg", {
    "特征编号": "FEAT-2001", "特征名称": "spend_7d", "数据类型": "DOUBLE",
    "业务域": "交易域", "数据来源": "Hive/dwd_trade", "更新频率": "T+1",
    "负责人": "甲", "注册时间": "2026-11-20",
})
f_refund = create("feature_reg", {
    "特征编号": "FEAT-2002", "特征名称": "spend_7d_refund", "数据类型": "DOUBLE",
    "业务域": "交易域", "数据来源": "Hive/dwd_refund", "更新频率": "T+1",
    "负责人": "甲", "注册时间": "2026-11-20",
})
# 同名特征出现在第二个业务域：spend_7d 重名（仍为草稿，重名同样让显示名无法定责）
create("feature_reg", {
    "特征编号": "FEAT-2003", "特征名称": "spend_7d", "数据类型": "DOUBLE",
    "业务域": "营销域", "数据来源": "Hive/mkt_campaign", "更新频率": "T+1",
    "负责人": "乙", "注册时间": "2026-11-20",
})
f_other = create("feature_reg", {
    "特征编号": "FEAT-2004", "特征名称": "coupon_cnt_7d", "数据类型": "INT64",
    "业务域": "交易域", "数据来源": "Hive/dwd_trade", "更新频率": "T+1",
    "负责人": "甲", "注册时间": "2026-11-20",
})

# ---------------------------------------------------------------- 登记规则
# R-A：只依赖 spend_7d_refund 的老记录（仅显示名）
create("derive_rule", {
    "规则编号": "DERI-T-A", "输入特征": "spend_7d_refund", "衍生函数": "refund_rate",
    "输出特征": "out_refund_rate", "计算粒度": "user", "依赖表": "dwd_refund",
    "刷新策略": "T+1", "最近产出": "-",
})
# R-B：身份明确的多输入规则
create("derive_rule", {
    "规则编号": "DERI-T-B", "输入特征": "spend_7d;spend_7d_refund",
    "输入特征标识": "FEAT-2001;FEAT-2002", "衍生函数": "net_spend",
    "输出特征": "out_net_spend", "计算粒度": "user", "依赖表": "dwd_trade",
    "刷新策略": "T+1", "最近产出": "-",
})
# R-C：引用历史同名输入 spend_7d（交易/营销两域重名），身份无法定责
create("derive_rule", {
    "规则编号": "DERI-T-C", "输入特征": "spend_7d", "衍生函数": "spend_bucket",
    "输出特征": "out_spend_bucket", "计算粒度": "user", "依赖表": "dwd_trade",
    "刷新策略": "T+1", "最近产出": "-",
})
# R-D：前缀相似但未注册的输入，永远不具备依赖
create("derive_rule", {
    "规则编号": "DERI-T-D", "输入特征": "spend_7", "衍生函数": "prefix_only",
    "输出特征": "out_prefix", "计算粒度": "user", "依赖表": "dwd_trade",
    "刷新策略": "T+1", "最近产出": "-",
})
# R-E：历史多输入规则（两个显示名都唯一可解析）
create("derive_rule", {
    "规则编号": "DERI-T-E", "输入特征": "spend_7d_refund;coupon_cnt_7d",
    "衍生函数": "refund_per_coupon", "输出特征": "out_net_spend",
    "计算粒度": "user", "依赖表": "dwd_trade", "刷新策略": "T+1", "最近产出": "-",
})

# ---------------------------------------------------------------- 登记窗口
create("time_window", {
    "模板编号": "TIME-T-1", "窗口类型": "滚动窗口", "窗口宽度": "7d",
    "滑动步长": "1d", "对齐基准": "自然日", "缺值填充": "0",
    "特征列表": "out_refund_rate", "最近计算": "-",
})
create("time_window", {
    "模板编号": "TIME-T-2", "窗口类型": "滚动窗口", "窗口宽度": "7d",
    "滑动步长": "1d", "对齐基准": "自然日", "缺值填充": "0",
    "特征列表": "out_net_spend", "最近计算": "-",
})
create("time_window", {
    "模板编号": "TIME-T-3", "窗口类型": "滚动窗口", "窗口宽度": "30d",
    "滑动步长": "1d", "对齐基准": "自然日", "缺值填充": "0",
    "特征列表": "unrelated_feature", "最近计算": "-",
})
create("time_window", {
    "模板编号": "TIME-T-4", "窗口类型": "滚动窗口", "窗口宽度": "7d",
    "滑动步长": "1d", "对齐基准": "自然日", "缺值填充": "0",
    "特征列表": "out_spend_bucket", "最近计算": "-",
})

# -------------------------------- 审核 spend_7d（交易域 FEAT-2001）
receipt = audit(f_spend["id"])
flow = receipt["flow"]
assert flow and flow["hits"] > 0
effect_refs = {e["ref_no"] for e in flow["effects"] if e.get("ref_no")}

rule_a = find_rule("DERI-T-A")
rule_b = find_rule("DERI-T-B")
rule_c = find_rule("DERI-T-C")
rule_d = find_rule("DERI-T-D")
rule_e = find_rule("DERI-T-E")

check("退款专属规则不被 spend_7d 前缀串号启效", rule_a["status"] == "草稿", rule_a["status"])
# 另一输入 FEAT-2002 尚未审核：整套输入未齐，身份明确的多输入规则也必须等待
check("多输入规则输入未齐时等待核实", rule_b["status"] == "草稿"
      and rule_b.get("输入核实状态") == "待核实", f"{rule_b['status']}/{rule_b.get('输入核实状态')}")
check("重名历史规则维持草稿并标记待核实",
      rule_c["status"] == "草稿" and rule_c.get("输入核实状态") == "待核实",
      f"{rule_c['status']}/{rule_c.get('输入核实状态')}")
check("前缀相似未注册规则始终草稿", rule_d["status"] == "草稿", rule_d["status"])
# coupon_cnt_7d 尚未审核注册，R-E 整套输入未齐
check("历史多输入规则缺输入时等待", rule_e["status"] == "草稿", rule_e["status"])

# 此时任何窗口都不应被启动
for tpl in ("TIME-T-1", "TIME-T-2", "TIME-T-3", "TIME-T-4"):
    check(f"无规则启效时窗口 {tpl} 保持草稿", find_window(tpl)["status"] == "草稿")

# 待核实规则仍可搜索（按规则编号关键字与输入特征精确过滤都应可见）
search_pending = call("GET", "/derive_rule?keyword=DERI-T-C")["items"]
check("待核实规则可被关键字搜索", any(r["规则编号"] == "DERI-T-C" for r in search_pending))
search_input = call("GET", "/derive_rule?输入特征=spend_7d")["items"]
hit_nos = {r["规则编号"] for r in search_input}
check("输入特征精确检索命中依赖方", {"DERI-T-B", "DERI-T-C"} <= hit_nos, str(hit_nos))
check("精确检索不被前缀重叠污染（不含退款专属/前缀规则）",
      "DERI-T-A" not in hit_nos and "DERI-T-D" not in hit_nos, str(hit_nos))

# 治理待办已登记（挂起规则各一条）
audits = call("GET", "/govern_audit?size=200")["items"]
followup_c = [a for a in audits if a.get("source_rule") == "DERI-T-C"]
check("重名规则登记了身份核实待办",
      len(followup_c) == 1 and followup_c[0]["整改状态"] == "待补齐身份")

# 注册回执的规则编号与衍生清单一致（挂起回执同样带规则编号）
check("注册回执与衍生详情共用同一规则编号", "DERI-T-B" in effect_refs, str(effect_refs))

# -------------------------------- 审核 spend_7d_refund
audit(f_refund["id"])
rule_a2 = find_rule("DERI-T-A")
rule_b2 = find_rule("DERI-T-B")
rule_e2 = find_rule("DERI-T-E")
check("退款特征审核后退款专属规则启效", rule_a2["status"] == "生效中", rule_a2["status"])
check("退款规则登记正确输入身份", rule_a2.get("输入特征标识") == ["FEAT-2002"],
      str(rule_a2.get("输入特征标识")))
check("多输入规则在输入齐全后启效", rule_b2["status"] == "生效中", rule_b2["status"])
check("启效规则写入输入身份", rule_b2.get("输入特征标识") == ["FEAT-2001", "FEAT-2002"],
      str(rule_b2.get("输入特征标识")))
check("历史多输入规则仍等待未注册输入", rule_e2["status"] == "草稿", rule_e2["status"])

win1, win2, win3 = find_window("TIME-T-1"), find_window("TIME-T-2"), find_window("TIME-T-3")
check("退款规则输出窗口启动", win1["status"] == "计算中" and win1.get("trigger_rule") == "DERI-T-A",
      f"{win1['status']}/{win1.get('trigger_rule')}")
check("多输入规则输出窗口启动并记录规则编号",
      win2["status"] == "计算中" and win2.get("trigger_rule") == "DERI-T-B",
      f"{win2['status']}/{win2.get('trigger_rule')}")
check("窗口触发依据指向审核特征", win2.get("awakened_by") == "feature_reg/FEAT-2002",
      str(win2.get("awakened_by")))
check("无规则输出的窗口保持草稿", win3["status"] == "草稿")
check("衍生清单回写窗口登记", "TIME-T-2" in rule_b2.get("窗口登记", []),
      str(rule_b2.get("窗口登记")))
# 待办随启效闭环
followup_b = [a for a in call("GET", "/govern_audit?size=200")["items"]
              if a.get("source_rule") == "DERI-T-B"]
check("多输入规则启效后待办闭环", all(a["整改状态"] == "已闭环" for a in followup_b))

# -------------------------------- 审核 coupon_cnt_7d：R-E 启效，窗口触发依据不被替换
audit(f_other["id"])
rule_e3 = find_rule("DERI-T-E")
check("历史多输入规则在输入齐全后启效（兼容老数据）",
      rule_e3["status"] == "生效中", rule_e3["status"])
win2b = find_window("TIME-T-2")
check("已启动窗口的触发依据不被替换", win2b.get("awakened_by") == "feature_reg/FEAT-2002",
      str(win2b.get("awakened_by")))
check("窗口保留原触发规则编号", win2b.get("trigger_rule") == "DERI-T-B")
check("窗口追加匹配规则而不重启", "DERI-T-E" in (win2b.get("matched_rules") or []),
      str(win2b.get("matched_rules")))
check("后启效规则的衍生清单同步窗口匹配", "TIME-T-2" in rule_e3.get("窗口登记", []),
      str(rule_e3.get("窗口登记")))

# -------------------------------- 同名规则补齐身份
rule_c_id = find_rule("DERI-T-C")["id"]
bad = call("POST", f"/derive_rule/{rule_c_id}/identity-backfill",
           {"identifiers": ["FEAT-9999"]})
check("未知特征编号不能补齐身份", "未在特征注册库登记" in bad["message"], bad["message"])
check("补齐失败规则维持草稿", find_rule("DERI-T-C")["status"] == "草稿")

ok = call("POST", f"/derive_rule/{rule_c_id}/identity-backfill",
          {"identifiers": ["FEAT-2001"]})
check("身份补齐接口受理", ok["ok"] and ok["entry"]["status"] == "生效中", ok["message"])
rule_c2 = find_rule("DERI-T-C")
check("补齐身份后规则启效", rule_c2["status"] == "生效中"
      and rule_c2.get("输入核实状态") == "已核实")
followups2 = [a for a in call("GET", "/govern_audit?size=200")["items"]
              if a.get("source_rule") == "DERI-T-C"]
check("启效后核实待办闭环", all(a["整改状态"] == "已闭环" for a in followups2))
win4 = find_window("TIME-T-4")
check("补齐启效规则的输出窗口启动", win4["status"] == "计算中"
      and win4.get("trigger_rule") == "DERI-T-C", f"{win4['status']}/{win4.get('trigger_rule')}")

# 前缀规则最终仍为草稿
check("前缀相似规则最终仍维持草稿", find_rule("DERI-T-D")["status"] == "草稿")

# -------------------------------- 手动启效与联动链路同口径
# 新建一条输入未注册的规则，手动启效也必须被来源核对拦截
r_manual = create("derive_rule", {
    "规则编号": "DERI-T-M", "输入特征": "not_registered_feat", "衍生函数": "manual_guard",
    "输出特征": "out_manual", "计算粒度": "user", "依赖表": "dwd_trade",
    "刷新策略": "T+1", "最近产出": "-",
})
manual_block = call("POST", f"/derive_rule/{r_manual['id']}/actions", {"action": "启效规则"})
check("手动启效未核实规则被拦截并说明原因",
      "无法核对" in manual_block["message"]
      and manual_block["entry"]["status"] == "草稿",
      manual_block["message"])
check("被拦截规则维持草稿且标记待核实",
      find_rule("DERI-T-M")["status"] == "草稿"
      and find_rule("DERI-T-M").get("输入核实状态") == "待核实")

# 已核实身份的规则手动启效：成功并联动输出匹配窗口
create("time_window", {
    "模板编号": "TIME-T-5", "窗口类型": "滚动窗口", "窗口宽度": "7d",
    "滑动步长": "1d", "对齐基准": "自然日", "缺值填充": "0",
    "特征列表": "out_manual_ok", "最近计算": "-",
})
r_manual_ok = create("derive_rule", {
    "规则编号": "DERI-T-M2", "输入特征": "spend_7d_refund",
    "输入特征标识": "FEAT-2002", "衍生函数": "manual_ok",
    "输出特征": "out_manual_ok", "计算粒度": "user", "依赖表": "dwd_trade",
    "刷新策略": "T+1", "最近产出": "-",
})
manual_ok = call("POST", f"/derive_rule/{r_manual_ok['id']}/actions", {"action": "启效规则"})
check("已核实规则手动启效成功", manual_ok["ok"] and manual_ok["entry"]["status"] == "生效中",
      manual_ok["message"])
check("手动启效同样联动输出匹配窗口",
      find_window("TIME-T-5")["status"] == "计算中"
      and find_window("TIME-T-5").get("trigger_rule") == "DERI-T-M2")

# -------------------------------- 兼容：种子里的精确单名老规则
seed_rule_before = next(
    r for r in call("GET", "/derive_rule?size=200")["items"] if r["规则编号"] == "DERI-0001")
check("种子精确单名规则初始为草稿", seed_rule_before["status"] == "草稿")
seed_feat_row = next(r for r in call("GET", "/feature_reg?size=200")["items"]
                     if r["特征编号"] == "FEAT-0002")
audit(seed_feat_row["id"])
seed_rule_after = next(
    r for r in call("GET", "/derive_rule?size=200")["items"] if r["规则编号"] == "DERI-0001")
check("精确单名老记录审核后正常启效", seed_rule_after["status"] == "生效中",
      seed_rule_after["status"])
check("老记录启效补登身份", seed_rule_after.get("输入特征标识") == ["FEAT-0002"],
      str(seed_rule_after.get("输入特征标识")))

print()
if failures:
    print(f"共 {len(failures)} 项失败：{failures}")
    sys.exit(1)
print("全部核验通过")
