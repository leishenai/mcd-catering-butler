#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦团餐管家 - 团餐方案规划与预算分摊引擎。

职责单一：把「多少人、多少钱、在哪、什么场合、谁忌口」翻译成一份可直接下单的
三档团餐方案。菜单数据由麦当劳 MCP（query-meals / list-nutrition-foods）提供，
本模块只做组合优化、预算控制与分摊计算，不联网、不持有任何凭证。

用法：
    python scripts/catering_planner.py --people 50 --budget 1500 --occasion 部门聚餐
    python scripts/catering_planner.py --people 20 --budget 600 --diet vegetarian,no_spicy --split aa
    python scripts/catering_planner.py --people 30 --budget 900 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

CATEGORY_LABEL = {"main": "主食", "side": "小食", "drink": "饮品"}
CATEGORY_SHARE = {"main": 0.70, "side": 0.20, "drink": 0.10}
QTY_RATIO = {"main": 1.0, "side": 0.25, "drink": 0.6}
SIDE_FLOOR_RATIO = 0.06
DRINK_FLOOR_RATIO = 0.40

# 超预算时的牺牲顺序：先砍饮品，再砍小食，主食尽量保住
DOWNGRADE_WEIGHT = {"main": 0.5, "side": 0.8, "drink": 3.0}
BUDGET_HEADROOM = 0.97

DIET_BLOCK = {
    "vegetarian": {"beef", "chicken", "fish"},
    "no_spicy": {"spicy"},
    "no_beef": {"beef"},
    "no_fried": {"fried"},
    "no_chicken": {"chicken"},
}

TIER_SPEC = [
    ("控本档", 0.85, "贴合预算下沿，适合内部日常会议餐"),
    ("标准档", 1.00, "按申报预算执行，推荐方案"),
    ("升级档", 1.15, "超出申报预算，需额外审批"),
]


@dataclass(frozen=True)
class MenuItem:
    name: str
    category: str
    price: float
    calories: int
    tags: tuple[str, ...]
    sku: str


def load_menu(path: Path) -> list[MenuItem]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = [
        MenuItem(
            name=row["name"],
            category=row["category"],
            price=float(row["price"]),
            calories=int(row.get("calories", 0)),
            tags=tuple(row.get("tags", [])),
            sku=row.get("sku", ""),
        )
        for row in raw["items"]
    ]
    if not items:
        raise ValueError(f"菜单为空：{path}")
    return items


def filter_by_diet(menu: list[MenuItem], diets: list[str]) -> tuple[list[MenuItem], list[str]]:
    blocked: set[str] = set()
    for diet in diets:
        blocked |= DIET_BLOCK.get(diet, set())
    if not blocked:
        return menu, []
    kept = [item for item in menu if not (set(item.tags) & blocked)]
    removed = sorted({item.name for item in menu if set(item.tags) & blocked})
    return kept, removed


def _pools(menu: list[MenuItem]) -> tuple[dict[str, list[MenuItem]], bool]:
    """按类别分组。麦当劳纯素食主食确实不存在，此时用小食顶上主食位并标记降级。"""
    pools = {c: [m for m in menu if m.category == c] for c in CATEGORY_SHARE}
    degraded = False
    if not pools["main"]:
        fallback = pools["side"] or list(menu)
        if not fallback:
            raise ValueError("当前忌口条件下没有任何可用餐品，请放宽条件。")
        pools["main"] = fallback
        degraded = True
    for c in pools:
        if not pools[c]:
            pools[c] = pools["main"]
    return pools, degraded


def initial_selection(menu: list[MenuItem], people: int, target: float) -> tuple[list[dict], bool]:
    """按类别预算选基准 SKU；主食按 7:3 拆两个价位相近的 SKU，保留菜单变化。"""
    pools, degraded = _pools(menu)
    state: list[dict] = []
    for cat, share in CATEGORY_SHARE.items():
        pool = pools[cat]
        n_total = max(1, round(people * QTY_RATIO[cat]))
        cap = target * share / n_total
        affordable = [p for p in pool if p.price <= cap]
        primary = max(affordable, key=lambda p: p.price) if affordable else min(pool, key=lambda p: p.price)

        n1 = n_total if n_total <= 1 else max(1, round(n_total * 0.7))
        n2 = n_total - n1
        state.append({"item": primary, "qty": n1})
        if n2 <= 0:
            continue
        others = [p for p in pool if p.name != primary.name]
        similar = [p for p in others if primary.price * 0.75 <= p.price <= primary.price * 1.25]
        cap2 = target * share / n2 * 0.9
        affordable2 = [p for p in similar if p.price <= cap2]
        if affordable2:
            state.append({"item": max(affordable2, key=lambda p: p.price), "qty": n2})
        else:
            state[-1]["qty"] = n_total
    return state, degraded


def converge_to_budget(state: list[dict], menu: list[MenuItem], target: float) -> list[dict]:
    """第一步收敛：把最贵的一类换成同类更便宜的 SKU。"""
    pools, _ = _pools(menu)
    ceiling = target * BUDGET_HEADROOM
    for _ in range(200):
        if sum(s["item"].price * s["qty"] for s in state) <= ceiling:
            break
        best = None
        for slot in state:
            cheaper = [p for p in pools[slot["item"].category] if p.price < slot["item"].price]
            if not cheaper:
                continue
            nxt = max(cheaper, key=lambda p: p.price)
            gain = (slot["item"].price - nxt.price) * slot["qty"] * DOWNGRADE_WEIGHT.get(
                slot["item"].category, 1.0
            )
            if best is None or gain > best[0]:
                best = (gain, slot, nxt)
        if best is None:
            break
        best[1]["item"] = best[2]
    return state


def trim_quantities(state: list[dict], people: int, target: float) -> list[dict]:
    """第二步收敛：换无可换时减份数。饮品先减，小食次之，主食不动。"""
    ceiling = target * BUDGET_HEADROOM
    floors = {
        "drink": max(1, round(people * DRINK_FLOOR_RATIO)),
        "side": max(1, round(people * SIDE_FLOOR_RATIO)),
    }
    for _ in range(500):
        if sum(s["item"].price * s["qty"] for s in state) <= ceiling:
            break
        movable = [
            s for s in state
            if s["item"].category in floors and s["qty"] > floors[s["item"].category]
        ]
        if not movable:
            break
        movable.sort(key=lambda s: (0 if s["item"].category == "drink" else 1, -s["item"].price * s["qty"]))
        movable[0]["qty"] -= 1
    return state


def merge_and_consume(state: list[dict], menu: list[MenuItem], target: float) -> list[dict]:
    """合并同 SKU；结余较多时补一份像样的小食，而不是堆最便宜的水果。"""
    merged: dict[str, dict] = {}
    for slot in state:
        key = slot["item"].sku or slot["item"].name
        if key in merged:
            merged[key]["qty"] += slot["qty"]
        else:
            merged[key] = {"item": slot["item"], "qty": slot["qty"]}
    result = list(merged.values())

    total = sum(s["item"].price * s["qty"] for s in result)
    room = target * BUDGET_HEADROOM - total
    sides = [m for m in menu if m.category == "side"]
    if sides and room > 0:
        affordable = [s for s in sides if s.price <= room * 0.5]
        if affordable:
            add = max(affordable, key=lambda s: s.price)
            count = min(3, int(room * 0.5 // add.price))
            if count > 0:
                hit = next((s for s in result if s["item"].sku == add.sku), None)
                if hit:
                    hit["qty"] += count
                else:
                    result.append({"item": add, "qty": count})
    return result


def build_plan(menu: list[MenuItem], people: int, target: float, label: str, note: str) -> dict:
    state, degraded = initial_selection(menu, people, target)
    state = converge_to_budget(state, menu, target)
    state = trim_quantities(state, people, target)
    state = merge_and_consume(state, menu, target)

    lines, total, calories = [], 0.0, 0
    for slot in state:
        item = slot["item"]
        subtotal = round(item.price * slot["qty"], 2)
        lines.append(
            {
                "name": item.name,
                "category": item.category,
                "qty": slot["qty"],
                "unit_price": item.price,
                "subtotal": subtotal,
                "calories": item.calories * slot["qty"],
                "sku": item.sku,
            }
        )
        total += subtotal
        calories += item.calories * slot["qty"]

    total = round(total, 2)
    warnings = []
    if degraded:
        warnings.append(
            "麦当劳菜单下没有符合该忌口条件的主食，方案已用小食替代主食位；"
            "建议为特殊饮食需求者单独到店选购，或改用可定制餐品的门店。"
        )
    if total > target:
        warnings.append(
            f"当前菜单无法压进该档预算，仍超 ¥{round(total - target, 2)}，建议下调餐标、减人数或改拼单。"
        )
    mains_count = sum(line["qty"] for line in lines if line["category"] == "main")
    if mains_count < people and not degraded:
        warnings.append(f"主食仅 {mains_count} 份，少于 {people} 人，需补足或按共享餐设计。")

    return {
        "tier": label,
        "note": note,
        "target_budget": round(target, 2),
        "total": total,
        "per_capita": round(total / people, 2),
        "balance": round(target - total, 2),
        "calories_total": calories,
        "calories_per_capita": round(calories / people),
        "lines": lines,
        "warnings": warnings,
    }


def render(plans: list[dict], meta: dict) -> str:
    people = meta["people"]
    out: list[str] = [f"# 团餐方案：{meta['occasion']}", ""]
    out.append(
        f"**人数** {people} 人 ｜ **申报预算** ¥{meta['budget']:.2f} ｜ "
        f"**城市** {meta['city']} ｜ **分摊方式** {meta['split_label']}"
    )
    if meta["removed"]:
        out.extend(["", f"> 已按忌口条件剔除：{'、'.join(meta['removed'])}"])
    out.extend(["", "## 三档对比", "", "| 档位 | 总价 | 人均 | 结余 | 人均热量 | 说明 |", "|---|---:|---:|---:|---:|---|"])
    for p in plans:
        out.append(
            f"| {p['tier']} | ¥{p['total']:.2f} | ¥{p['per_capita']:.2f} | "
            f"¥{p['balance']:.2f} | {p['calories_per_capita']} kcal | {p['note']} |"
        )

    recommended = plans[1] if len(plans) > 1 else plans[0]
    out.extend(["", f"## 推荐方案明细（{recommended['tier']}）", "",
                "| 类别 | 餐品 | 数量 | 单价 | 小计 | 餐品编码 |", "|---|---|---:|---:|---:|---|"])
    for line in recommended["lines"]:
        out.append(
            f"| {CATEGORY_LABEL.get(line['category'], line['category'])} | {line['name']} | "
            f"{line['qty']} | ¥{line['unit_price']:.2f} | ¥{line['subtotal']:.2f} | `{line['sku']}` |"
        )
    out.append(f"| **合计** |  |  |  | **¥{recommended['total']:.2f}** |  |")

    out.extend(["", "## 预算分摊", ""])
    if meta["split"] == "aa":
        out.append(f"- 全员 AA：**每人 ¥{recommended['total'] / people:.2f}**，收齐后由组织者统一支付。")
    elif meta["split"] == "company":
        out.append(f"- 公司全额报销：走一笔报销单，金额 **¥{recommended['total']:.2f}**，附本表与订单截图。")
    else:
        company = recommended["total"] * 0.7
        per = company / 0.7 * 0.3 / people
        out.append(f"- 公司承担 70%：**¥{company:.2f}**；个人均摊 30%：每人 **¥{per:.2f}**。")

    warns = [w for p in plans for w in p["warnings"]]
    if warns:
        out.extend(["", "## 风险提示", ""])
        for w in dict.fromkeys(warns):
            out.append(f"- {w}")

    out.extend(["", "## 下单前必做校验", "",
                "1. 用 `query-meals` 核对餐品编码在当前门店是否在售、价格是否变动。",
                "2. 用 `calculate-price` 叠加可用优惠券重算总价，本表价格未含优惠。",
                "3. 用 `query-meal-assistance` 确认门店是否支持团餐助餐服务与送达时间。", "",
                "> 本方案为参赛作品输出，非麦当劳官方产品；餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准。"])
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser(description="麦麦团餐管家 - 团餐方案规划与预算分摊")
    parser.add_argument("--people", type=int, required=True, help="用餐人数")
    parser.add_argument("--budget", type=float, required=True, help="申报总预算（元）")
    parser.add_argument("--occasion", default="部门聚餐", help="场合：部门聚餐 / 加班餐 / 团建派对 / 入职欢迎")
    parser.add_argument("--city", default="杭州", help="城市")
    parser.add_argument("--diet", default="", help="忌口，逗号分隔：vegetarian,no_spicy,no_beef,no_fried,no_chicken")
    parser.add_argument("--split", default="company", choices=["company", "aa", "mixed"], help="分摊方式")
    parser.add_argument("--menu", default=None, help="菜单 JSON 路径，默认 assets/sample_menu.json")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    if args.people <= 0 or args.budget <= 0:
        print("人数与预算必须大于 0", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parent.parent
    menu_path = Path(args.menu) if args.menu else root / "assets" / "sample_menu.json"
    menu = load_menu(menu_path)

    diets = [d.strip() for d in args.diet.split(",") if d.strip()]
    menu, removed = filter_by_diet(menu, diets)
    if not menu:
        print("忌口条件过严，菜单已被全部剔除。", file=sys.stderr)
        return 3

    plans = [
        build_plan(menu, args.people, args.budget * ratio, label, note)
        for label, ratio, note in TIER_SPEC
    ]
    split_label = {"aa": "全员 AA", "company": "公司报销", "mixed": "公司 70% + 个人 30%"}[args.split]
    meta = {
        "people": args.people,
        "budget": args.budget,
        "occasion": args.occasion,
        "city": args.city,
        "split": args.split,
        "split_label": split_label,
        "removed": removed,
        "menu_source": menu_path.name,
    }

    if args.json:
        print(json.dumps({"meta": meta, "plans": plans}, ensure_ascii=False, indent=2))
    else:
        print(render(plans, meta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
