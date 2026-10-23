#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从麦当劳 MCP 拉真实菜单，转换为 catering_planner.py 可消费的菜单格式。

这是项目里唯一直接联网的脚本，用于把 MCP 返回的真实数据结构映射成本地方案引擎的
输入格式。字段映射依据 2026-10-23 的真实接口返回实测结果：

- query-nearby-stores 必须同时传 city 与 keyword，否则报 600058
- query-meals 返回 data.meals 是「商品编码 -> 商品」的字典，不是数组
- 商品价格字段为 currentPrice，字符串，单位为元
- list-nutrition-foods 返回的是类 CSV 文本，按 productName 匹配，不按编码
- calculate-price 的商品字段名是 productCode（不是 code），价格单位为分

用法：
    export MCD_TOKEN=你的Token
    python scripts/fetch_menu.py --city 杭州 --keyword 杭州 --out assets/real_menu.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

ENDPOINT = "https://mcp.mcd.cn"

# 引擎类别判定。门店分类本身是混装的（「小食拼盘/多人餐」里也可能有堡），
# 所以先看商品名，名称判不出来才退回分类名。
CATEGORY_RULES = [
    ("drink", ("饮品", "咖啡", "饮料", "可乐", "雪碧", "黑巧", "奶", "茶", "雪冰", "汁", "水")),
    ("side", ("小食", "炸鸡", "甜品", "拼盘", "薯条", "冰淇淋", "翅", "块", "派")),
    ("main", ("汉堡", "堡", "卷", "单人餐", "套餐", "饭")),
]

TAG_RULES = {
    "spicy": ("辣", "椒", "川"),
    "beef": ("牛", "吉士", "安格斯"),
    "chicken": ("鸡",),
    "fish": ("鱼",),
    "fried": ("炸", "薯", "鸡块", "鸡翅"),
}


def requires_token() -> str:
    token = os.environ.get("MCD_TOKEN", "").strip()
    if not token:
        raise SystemExit("请先设置环境变量 MCD_TOKEN，例如：export MCD_TOKEN=你的Token")
    return token


def call_tool(token: str, name: str, args: dict) -> str:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}},
        ensure_ascii=False,
    )
    req = urllib.request.Request(
        ENDPOINT,
        data=body.encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SystemExit("Token 无效或已过期，请到 open.mcd.cn/mcp 重新激活") from exc
        if exc.code == 429:
            raise SystemExit("触发限流（600 次/分钟），请稍后重试") from exc
        raise
    if "error" in payload:
        raise SystemExit(f"MCP 调用失败 {name}: {json.dumps(payload['error'], ensure_ascii=False)[:300]}")
    return "".join(
        c.get("text", "") for c in payload.get("result", {}).get("content", []) if c.get("type") == "text"
    )


def extract_json(text: str):
    """MCP 返回是「说明文字 + JSON」混排，取第一个能完整解析的 JSON 对象。"""
    start = text.find("{")
    while start != -1:
        try:
            return json.JSONDecoder().raw_decode(text[start:])[0]
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    return None


def map_category(raw_name: str, product_name: str = "") -> str:
    """商品名优先，判不出来再用门店分类名兜底。"""
    for source in (product_name or "", (raw_name or "").replace("\n", "")):
        for category, keys in CATEGORY_RULES:
            if any(k in source for k in keys):
                return category
    return "side"


def infer_tags(name: str) -> list[str]:
    tags = [tag for tag, keys in TAG_RULES.items() if any(k in name for k in keys)]
    if not tags:
        tags.append("vegetarian")
    return tags


def parse_nutrition(text: str) -> dict[str, int]:
    """list-nutrition-foods 的 data 是含转义换行的类 CSV 字符串：名称,null,energyKj,energyKcal,..."""
    text = text.replace("\\n", "\n")
    table: dict[str, int] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("[") or line.startswith("{"):
            continue
        cells = [c.strip() for c in line.split(",")]
        if len(cells) < 4:
            continue
        try:
            table[cells[0]] = int(float(cells[3]))
        except ValueError:
            continue
    return table


def match_calories(name: str, table: dict[str, int]) -> int:
    if name in table:
        return table[name]
    for key, kcal in table.items():
        if key and (key in name or name in key):
            return kcal
    return 0


def fetch(token: str, city: str, keyword: str, store_code: str | None) -> dict:
    if store_code:
        code = store_code
    else:
        stores = extract_json(
            call_tool(token, "query-nearby-stores", {"beType": 1, "searchType": 2, "city": city, "keyword": keyword})
        )
        rows = (stores or {}).get("data") or []
        if not rows:
            raise SystemExit(f"未找到门店：city={city} keyword={keyword}")
        code = rows[0]["storeCode"]
        print(f"选定门店：{rows[0].get('storeName')} ({code})")

    meals = extract_json(call_tool(token, "query-meals", {"storeCode": code, "orderType": 1, "beType": 1}))
    data = (meals or {}).get("data") or {}
    food_map = data.get("meals") or {}
    if not isinstance(food_map, dict) or not food_map:
        raise SystemExit("query-meals 未返回可用菜单")

    nutrition_payload = extract_json(call_tool(token, "list-nutrition-foods", {})) or {}
    raw_nutrition = nutrition_payload.get("data")
    nutrition = parse_nutrition(raw_nutrition if isinstance(raw_nutrition, str) else json.dumps(raw_nutrition, ensure_ascii=False))
    print(f"套餐/商品数：{len(food_map)}  营养表记录数：{len(nutrition)}")

    # 记录商品编码所属的门店分类名，具体归类留到拿到商品名后再判
    code_to_catname: dict[str, str] = {}
    for cat in data.get("categories") or []:
        for item in cat.get("meals") or []:
            code_to_catname.setdefault(item.get("code"), cat.get("name", ""))

    items = []
    for code, info in food_map.items():
        name = info.get("name") or ""
        if not name:
            continue
        try:
            price = float(info.get("currentPrice") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if price <= 0:
            continue
        items.append(
            {
                "name": name,
                "category": map_category(code_to_catname.get(code, ""), name),
                "price": price,
                "calories": match_calories(name, nutrition),
                "tags": infer_tags(name),
                "sku": code,
            }
        )

    if not items:
        raise SystemExit("菜单转换后为空，请检查接口返回结构是否变化")

    items.sort(key=lambda x: (x["category"], -x["price"]))
    return {
        "_meta": {
            "说明": "由 fetch_menu.py 从麦当劳 MCP 实时拉取并转换，价格单位为元",
            "currency": "CNY",
            "source": "mcd-mcp",
            "storeCode": code,
            "city": city,
            "fetched_from": "https://mcp.mcd.cn",
        },
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="从麦当劳 MCP 拉取真实菜单并转换为方案引擎格式")
    parser.add_argument("--city", default="杭州", help="城市名，如 杭州")
    parser.add_argument("--keyword", default=None, help="关键词，默认与城市名相同（接口要求两者都非空）")
    parser.add_argument("--store", default=None, help="直接指定 storeCode，跳过门店查询")
    parser.add_argument("--out", default=None, help="输出路径，默认 assets/real_menu.json")
    args = parser.parse_args()

    token = requires_token()
    keyword = args.keyword or args.city
    menu = fetch(token, args.city, keyword, args.store)

    out = Path(args.out) if args.out else Path(__file__).resolve().parent.parent / "assets" / "real_menu.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(menu, ensure_ascii=False, indent=2), encoding="utf-8")

    by_cat: dict[str, int] = {}
    for item in menu["items"]:
        by_cat[item["category"]] = by_cat.get(item["category"], 0) + 1
    print(f"已写入：{out}")
    print("分类分布：" + "、".join(f"{k} {v} 项" for k, v in sorted(by_cat.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
