# MCP 集成说明

本文件说明「麦麦团餐管家」实际调用了哪些麦当劳 MCP 工具、按什么顺序调用、为什么这样调，以及**实测发现的接口真实行为**。

> 本文档所有参数与字段描述均来自 2026-10-23 的**真实接口调用验证**，不是照抄官方文档。凡与官方文档不一致处，以本文实测结果为准，并已标注差异。

- **MCP Server**：麦当劳中国官方 MCP 服务
- **接入地址**：`https://mcp.mcd.cn`
- **传输协议**：Streamable HTTP（实测返回纯 JSON，无 SSE，无 `Mcp-Session-Id`，属无状态模式）
- **鉴权**：请求头 `Authorization: Bearer <YOUR_MCP_TOKEN>`
- **Token 获取**：[open.mcd.cn/mcp](https://open.mcd.cn/mcp) 手机号登录 → 控制台 → 激活
- **限流**：每 Token 每分钟 600 次，超出返回 429

配置示例见仓库根目录的 `mcp-config.example.json`，Token 使用 `${MCD_MCP_TOKEN}` 环境变量占位符，**仓库内不包含任何真实凭证**。

---

## 一、实测确认：35 个工具，不是文档写的 34 个

`tools/list` 实际返回 **35 个工具**。与官方 README 的差异：

| 差异类型 | 具体 |
|---|---|
| **命名不同** | 文档写 `query-partystore-date` / `query-partystore-session`，实际是 **`query-party-store-date`** / **`query-party-store-session`**（多一个连字符）。按文档写会报工具不存在 |
| **文档未列** | `query-promotions`（企业团餐满减/满折规则）、`query-survey-coupon`（按订单号查 CSAT 满意度答卷） |
| **数量** | 文档 34 个，实际 35 个 |

`query-promotions` 对团餐场景价值很高——它返回门店当前的企业团餐促销规则（`promotionType` 31-满额减、33-订单折扣），团餐金额大，一条满减规则可能就是一百多块。

---

## 二、核心工具的真实参数

以下是被本项目实际调用、且参数结构经实测确认的关键工具。

### 1. `query-nearby-stores`（查门店）

```json
{"beType": 1, "searchType": 2, "city": "杭州", "keyword": "杭州"}
```

**实测坑一：`city` 和 `keyword` 必须同时传。** 只传 `city` 或只传 `keyword` 都会报 `600058 城市名或者关键词不能为空`。这个错误码的字面意思会误导人以为参数没传到，实际是**两个都要给**。

**实测坑二：`beType` 只支持 1（到店自取）和 5（得来速）。** 查门店时传 `beType=6`（企业团餐）不会返回团餐门店，团餐门店要走 `delivery-query-stores`。

### 2. `query-meals`（拉菜单）

```json
{"storeCode": "3330521", "orderType": 1, "beType": 1}
```

**实测：返回结构是「分类 + 商品字典」双层，不是平铺数组。**

```
data.categories[]  →  {name, meals: [{code, tags}]}     ← 只有编码和标签，没有名称价格
data.meals{}       →  {code: {name, image, currentPrice, originalPrice, canWithOrder}}
```

关键点：

- `data.meals` 是**字典**（编码 → 商品），不是数组。按 `data["meals"][0]` 取会直接 `KeyError`。
- 商品价格字段是 **`currentPrice`**，值是**字符串**（如 `"47"`），单位**元**。
- 商品在 `categories` 里只有 `{code, tags}`，名称和价格要到 `data.meals[code]` 里取。**这是最容易写错的地方**。
- 返回里**没有热量**，热量要另调 `list-nutrition-foods`。

实测样例（杭州东站某门店，11 个分类 79 个商品）：

```json
"9900005456": {"name": "麦辣鸡腿汉堡三件套", "currentPrice": "47", "originalPrice": "47", "canWithOrder": false}
```

### 3. `list-nutrition-foods`（营养数据）

```json
{}
```

**实测：`data` 是字符串，不是数组**，内容是含转义换行的类 CSV 文本：

```
[158]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  中薯条,null,1210,289,4,12,38,165,18
```

三个必须注意的点：

1. `data` 是**字符串**，里面的换行是 `\n` 转义的，直接 `splitlines()` 只会得到一行。要先 `text.replace("\\n", "\n")`。
2. 匹配键是 **`productName`（中文名）**，不是商品编码。所以热量只能按名称模糊匹配，**匹配率做不到 100%**，匹配不上按 0 计。
3. 表头声明 `[158]` 表示 158 条记录，实测确实返回 158 条。

### 4. `calculate-price`（算价）

```json
{"storeCode": "3330521", "orderType": 1, "beType": 1,
 "items": [{"productCode": "1440", "quantity": 1}]}
```

**实测坑：商品字段名是 `productCode`，不是 `code`。** 写成 `[{"code": "1440", "quantity": 1}]` 不会报错，而是**静默返回 `price: 0`、`productList: []`**——这是最危险的一类失败：看起来成功了，实际算了个空。

**实测：返回金额单位是分。** `productPrice: 2550` 表示 ¥25.50。

### 5. `query-promotions`（团餐促销，文档未列）

```json
{"storeCode": "3330521", "orderType": 2, "beType": 6, "beCode": "..."}
```

**团餐场景固定 `beType=6`、`orderType=2`（外送）。** 返回 `promotionType`（31-满额减 / 33-订单折扣）、`ruleCategory`（30-满折 / 40-满减）等规则。

### 关于企业团餐的特殊性

实测参数说明确认：**企业团餐是独立业务类型 `beType=6`，走外送链路 `orderType=2`**，与到店自取（1）、麦乐送（2）、得来速（5）并列。团餐门店要用 `delivery-query-stores` 查，不能用 `query-nearby-stores`。团餐还有专属的 `gmServiceCode`（助餐服务码）参数。

---

## 三、实际调用流程

本项目把流程拆成「拉数据」和「算方案」两段，前者联网，后者纯本地。

```
query-nearby-stores(city+keyword, beType=1)      ← 必须两个参数同时给
      │  storeCode
      ▼
query-meals(storeCode, orderType=1, beType=1)
      │  data.categories[] + data.meals{}       ← 名称价格在 meals 字典里
      ├──────────────┐
      ▼              ▼
list-nutrition-foods   query-store-coupons       ← 热量 + 优惠券
      │  （按名称匹配）
      ▼
scripts/fetch_menu.py  →  assets/real_menu.json  ← 转成引擎格式
      │
      ▼
scripts/catering_planner.py（纯本地，不联网）    ← 三档方案 + 分摊 + 预警
      │
      ▼
calculate-price(items=[{productCode, quantity}]) ← 用真实编码复核，单位分
      │
      ▼
  输出方案 → 用户确认 → 用户自行 create-order
```

团建/派对场景追加场次筛选（注意工具名）：

```
query-party-city → query-party-store → query-party-store-date → query-party-store-session
```

---

## 四、错误处理

| 错误码 | 含义 | 本项目处理 |
|---|---|---|
| `401` | Token 无效、过期或未提供 | 提示重新到 open.mcd.cn/mcp 激活，不重试 |
| `429` | 超过 600 次/分钟 | 退避重试，间隔 2 秒起，最多 3 次 |
| `600058` | **不是参数缺失**，是 `city` 与 `keyword` 未同时提供 | 两个都传上 |

**静默失败（无错误码，最危险）：**

| 现象 | 真实原因 |
|---|---|
| `calculate-price` 返回 `price: 0`、`productList: []`，但 `success: true` | `items` 字段名写成了 `code`，必须是 `productCode` |
| 营养表解析出 0 条 | `data` 是含 `\n` 转义的字符串，没先还原换行 |
| `KeyError: 0` 取不到商品 | `data.meals` 是字典不是数组 |

这三条都是实测踩出来的，不是理论推演。

---

## 五、字段映射清单

`scripts/fetch_menu.py` 完成的映射，供后续维护参考：

| MCP 原始字段 | 引擎字段 | 转换规则 |
|---|---|---|
| `data.meals[code].name` | `name` | 直接取 |
| `data.meals[code].currentPrice` | `price` | 字符串转 float，单位元，`<=0` 的丢弃 |
| `data.meals` 的键 `code` | `sku` | 直接作为商品编码 |
| `categories[].name` + 商品名 | `category` | **商品名优先**判断 main/side/drink，判不出来才用分类名兜底 |
| `list-nutrition-foods` 的 `energyKcal` | `calories` | 按 `productName` 模糊匹配，匹配不上记 0 |
| 商品名关键词 | `tags` | 辣→spicy、牛→beef、鸡→chicken、鱼→fish、炸/薯→fried，都没有则 vegetarian |

**分类判定为什么不能只看分类名**：门店分类是混装的。实测「小食拼盘/多人餐」分类里包含「龙焰芝士棒鸡腿堡三件套」，按分类名归类会把这个汉堡算成小食。所以必须商品名优先。

---

## 六、业务价值

一句话：**把「组织者要打十个电话才能问清楚的事」，压缩成一次对话。**

1. 有没有门店能送、多少份起送 —— `query-nearby-stores` / `delivery-query-stores`
2. 菜单和价格（不再是过期截图）—— `query-meals` 实时拉
3. 总价算三遍三遍不一样 —— `calculate-price` 复核，单位是分
4. 忘掉某人不吃辣 —— 忌口过滤 + 替代方案
5. 财务问「为什么超了 200」—— 三档方案 + 结余明细 + 超标预警
6. 团餐有没有满减 —— `query-promotions`（文档没写的那个）
7. 派对活动挨个问场次 —— 四级场次筛选

---

> 本文件所述集成方式遵循麦当劳 MCP 服务规则。项目为参赛作品，非麦当劳官方产品。
> 餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准。
