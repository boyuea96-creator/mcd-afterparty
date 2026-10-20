# MCP 集成说明

本文说明 AfterM 实际使用了哪些麦当劳 MCP Server 与 Tool、怎么调、以及这些调用换来了什么。

---

## 一、接入概况

| 项 | 值 |
|---|---|
| Server | 麦当劳中国 MCP — `https://mcp.mcd.cn` |
| 协议 | MCP Streamable HTTP（JSON-RPC 2.0，响应为 SSE `data:` 帧） |
| 协议版本 | `2025-06-18` |
| 鉴权 | 请求头 `Authorization`，Token 从 <https://open.mcd.cn/mcp> 领取 |
| 客户端 | 自研极简客户端 `app/mcpc.py`，**纯 Python 标准库**，无第三方依赖 |
| 会话 | 一次 `initialize` 拿到 `Mcp-Session-Id`，后续全部复用；线程内串行、调用间并发 |
| 只读约束 | **35 个工具中只接 9 个，全部为查询类；7 个写入类工具一律不接** |

客户端不依赖任何 MCP SDK 的原因很简单：MCP 就是 JSON-RPC 2.0 over HTTP，标准库 `urllib` 足够。这让整个项目 `git clone` 后**不需要 `pip install` 任何东西**就能跑。

---

## 二、实际使用的 Tool（9 个，全部只读）

| # | Tool | 用来干什么 | 参数要点 |
|---|---|---|---|
| 1 | `now-time-info` | 校准"现在几点/周几"，所有券的可用时段都靠它 | 无参 |
| 2 | `query-nearby-stores` | 找候选门店 | **只吃 `city` + `keyword`，不吃经纬度**；`keyword` 用街道名比"麦当劳(XX店)"稳 |
| 3 | `query-meals` | 拉门店菜单（价、图、是否可售） | 必带 `storeCode`；散场时间在未来时带 `reservationDate` |
| 4 | `calculate-price` | **真实试价**——本项目唯一的价格来源 | `items` 用 `productCode`（不是 `code`）；支持 `roundList` 指定选配 |
| 5 | `available-coupons` | 麦麦省可领券（"哪里有券"） | 返回 **markdown**，不是 JSON |
| 6 | `query-my-coupons` | 我的卡包（"我手里有什么券"） | 返回 **markdown**，不是 JSON |
| 7 | `query-store-coupons` | 已领 ∩ 该店 ∩ 该时段（"这单真能用哪张"） | 三源里**唯一能参与试价**的一源 |
| 8 | `delivery-query-stores` | 外送范围与配送门店 | 需落脚点地址 |
| 9 | `delivery-query-addresses` | 读取用户 App 里已存的外送地址 | 无参；**只读，不建地址** |

### 明确不接的写入类工具

| Tool | 为什么不接 |
|---|---|
| `create-order` / `party-order-create` / `mall-create-order` | 下单是用户的事，工具不替人付钱 |
| `cancel-order` | 同上 |
| `auto-bind-coupons` | 领券会让用户卡包发生变化，属于状态写入 |
| `delivery-create-address` | 建地址是写入。没有的地址就老实说"先去 App 加一个" |
| `draw-lottery` | 抽奖属于营销互动，与本场景无关 |

> **这条边界是产品决策，不是技术妥协**：一个替你算账的助手，一旦能下单，用户就得先信任它。只读意味着**最坏情况也只是算错，不会造成任何实际损失**。

---

## 三、调用流程

```
用户填完 → POST /api/plan
  │
  ├─ 0. now-time-info ────────────── 校准时间；散场时间已过就按现在算
  │
  ├─ 1. 地理（高德，非 MCP）
  │      geocode/geo → 校验 level，粗精度（市/区县/公交站）转 place/text 重查
  │      三处地址：当前地址、落脚点、门店反查
  │
  ├─ 2. 找店
  │      query-nearby-stores(city, 街道名) → 候选池
  │      按真实步行耗时排序 → 逐个 query-meals 验菜单（并行）
  │      最近的店不一定点得出餐：商场 B1、甜品站、已过营业时段都会失败 → 自动降级下一家
  │
  ├─ 3. 交通（高德，非 MCP）
  │      步行 / 骑行 / 公交地铁 / 打车 四路并行，两段路（散场点→店，店→落脚点）
  │
  ├─ 4. 券（三源并联）
  │      available-coupons ＋ query-my-coupons → 事实陈述
  │      query-store-coupons → 只有这一源的结果能进试价
  │
  ├─ 5. 试价（调用量最大的一段）
  │      calculate-price 逐个候选组合：套餐 / 单品 / 随心配 / 券后
  │      不传 roundList → 按 isDefault=1 求默认价；指定选配 → 再试一次
  │      ⇒ 结果取 data.price 最低者；划线价只作展示，绝不拿来算省额
  │
  └─ 6. 组文案 → 一句话结论 ＋ 三档买法 ＋ 收口 ＋ ≤6 行行动卡
```

### 调用量

典型一次完整计算（含堂食+外卖两套，6 人场景）：

| 阶段 | 麦当劳 MCP | 高德 |
|---|---|---|
| 时间/门店/菜单 | ~6–10 | ~10 |
| 试价 | ~20–25 | — |
| 券 | ~3 | — |
| 交通 | — | ~12 |
| **合计** | **约 36 次** | **约 22 次** |

全部并发，端到端 **7–10 秒**。

---

## 四、工程上踩过的坑（都写在 `skill/references/verified-facts.md` 里）

这几个不看文档根本发现不了，写错还**不报错**：

1. **`calculate-price` 的 items 用 `productCode`，不是 `code`。** 写错不报错，静默返回 `price=0`
2. **价格单位是分。** `4470` = ¥44.70
3. **`query-my-coupons` / `available-coupons` 返回 markdown**，不是 JSON，得单独解析
4. **`reservationDate` 必须是未来时间**，且 `query-meals` / `calculate-price` / `query-store-coupons` 都认它
5. **门店接口返回的经纬度是 `0.0`**，地址常写成"市心北路一层834…"，要清洗成路名再反查
6. **门店接口里的 `distance` 不能用来排序**——它的参照系不是用户填的地址（实测：真实 6.24km 的店报 `distance=687`）。用它排序会让 6 公里的店赢过 500 米的店。**排序只认自己算的步行耗时**
7. **响应是 SSE 帧，JSON 前面还有一段字段说明文字**，要从第一个 `{` 开始 `raw_decode` 逐个试

---

## 五、业务价值

### 对消费者

散场这个场景，痛点不是"不知道麦当劳有什么"，而是**在一堆实时变化的约束里快速找到一个说得通的做法**。MCP 提供的三件事刚好对上：

| MCP 能力 | 换来的用户价值 |
|---|---|
| `query-meals` + `calculate-price` | **真价**。不是"大概 40 块"，是"3 份随心配，¥41.70，人均 ¥13.90"，到店照着点就是这个数 |
| `available-coupons` + `query-my-coupons` + `query-store-coupons` | **券不白领**。三源分清"哪有券 / 我有什么券 / 这单真能用券"，避免"以为省了其实用不上" |
| `query-nearby-stores` + `delivery-query-stores` | **找得到、点得着**。最近的店点不出餐就自动降级，不会给出一个到店才发现关门的方案 |

### 对平台

- **把"点餐"往前挪了一步**：用户还没打开 App 就已经知道要点什么、花多少，决策成本降下来，打开 App 就是执行
- **券的使用率**：券的价值在"被用掉"，而这个工具的每一份方案都会告诉你这单能不能用券、能省多少
- **只读不写入**：没有任何越权风险，也不会产生脏订单

### 产品判断（三条硬规则）

1. **决策权归用户**：堂食和外卖两套账都算，摆出来让用户选。价差小于 ¥5/人 就直接说"差不多"
2. **"能选"不等于"能吃到"**：外卖能点热饮，但半小时到手也就温了。凡被时间改变的品质（温度、脆度、出水），外卖一律打问号。**说清折损，但不替用户否决**
3. **错了要认**：用户点名要的东西，菜单里没有就当场说没有，还要区分"这个点不供应"和"这家店就没有"——探不出来就不编原因。**一个假的优点能毁掉整份方案的可信度**

---

## 六、复现

```bash
git clone https://github.com/boyuea96-creator/mcd-afterparty.git
cd mcd-afterparty/app
export MCD_MCP_TOKEN=你的token      # 从 https://open.mcd.cn/mcp 领取
python3 server.py                   # 打开 http://127.0.0.1:8765
```

配置文件示例见 [`mcp-config.example.json`](./mcp-config.example.json)，只含环境变量占位符。
