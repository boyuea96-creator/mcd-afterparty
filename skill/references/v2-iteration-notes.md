# AfterM v2 迭代笔记（WorkBuddy 落地实测，2026-10-14）

> 由 WorkBuddy 用本机已接好的 mcd-mcp 真实只读跑通 8 步流程后，发现的「设计文档与实际 API 行为不符」之处。
> 供用户转 ZCode 调整 `SKILL-draft.md` / `workbuddy-draft.md` 设计。所有价格均来自真实 `calculate_price` 返回（单位分，÷100=元）。

## 一、必须修正的 API 行为偏差

### 1. query_meals（配送）reservationDate 格式
- 设计原文：「reservationDate=送达时刻」（仅写时点，未给格式）。
- 实测：传 `2026-10-14` 报 `code=600052`「预约时间格式错误，格式为yyyy-MM-dd HH:mm」。
- 修正：配送/到店 `query_meals` 的 reservationDate 必须传 `yyyy-MM-dd HH:mm`（如 `2026-10-14 22:30`）。

### 2. query_store_coupons（外送）必传 beCode
- 设计原文：第 5 步写 `query_store_coupons`（storeCode，orderType，beType，reservationDate），**漏了 beCode**。
- 实测：外送（beType=2）不传 beCode 报 `code=600048`「外送场景下，beType=2/5/6时必传beCode」。
- 修正：外送券查询必须带 beCode（与 `delivery_query_stores` 返回的 beCode 一致）。

### 3. calculate_price 总价取数规则
- 实测：顶层 `data.price` 才是含「商品+运费+打包」的真实总价；但 `productList` 里会插入一条「打包服务费」行且其 `subtotal=0`，而顶层 `packingPrice` 非空。
- 风险：若按 `productList` 求和会得到错误总价（漏算打包费）。
- 修正：技能一律以 `data.price` 为总价，运费看 `deliveryPrice`、打包看 `packingPrice`，**不要**对 `productList` 求和。

## 二、隐私红线与工具强制展示的冲突（需 ZCode 拍板）
- `delivery_query_addresses` 的返回格式说明明确要求**强制展示** `contactName` / `phone` / `fullAddress`（「禁止模型丢弃」）。
- 但技能红线 #6 写「联系人姓名、电话、详细地址不写入任何文件」。
- 冲突点：工具强制在对话中展示 PII，而红线要求不暴露/不落盘。
- 建议：红线维持「不写入任何文件 / 只用 addressId」；对话展示遵循工具强制格式但仅在对用户输出时出现，不进入任何文件或 `workbuddy.md` 导出。请在设计中明确这句边界，避免评审时被误判泄露。

## 三、实测验证通过、可保留的设计断言
- 大莲花 22:00 散场场景：到店 5 家门店全部 22:00 打烊、今夜预约窗口最晚 21:45 → 22:30 到店窗口整体 miss，**自动转麦乐送**，与设计并发。
- 京港国际（3330199 / beCode 333019902）确为 24h 店，今夜夜市 17:30–23:59、明日跨零点 00:00–05:29 可送，是深夜主推支线。
- 配送菜单独立编码体系（巨无霸 901100 vs 到店 1100）属实；多人分享餐可直接按人数映射。
- 券后价机制属实：中薯条买一送一券（券商品码 9900012387 + couponId + couponCode）传入 `calculate_price` → 原价 3000 分、券后 1350 分。

## 四、真实价例（大莲花 4 人 / 人均预算 ¥50 / 京港国际麦乐送 / 2026-10-14 22:30 送达）
| 方案 | 组合 | 真实到手价 | 人均 |
|---|---|---|---|
| A 图快直送（不凑券） | 快乐吃鸡双人餐 ×2（无券） | ¥120.80（商品111.8+运费6+打包3） | ¥30.20 |
| B 性价比之选 | 吃鸡盛宴四人餐 ×1 | ¥115.80（商品107+运费6+打包2.8） | ¥28.95（比A省¥1.25/人） |
| C 深夜分享 | 吃鸡盛宴四人餐 + 奥利奥麦旋风 ×2 + 可口可乐 ×2 | ¥161.25（商品152.25+运费6+打包3） | ¥40.31 |

> 设计修正（用户评审）：A=速度优先（不凑券的常见组合，允许略贵）；B=性价比优先（取人均最小值且 ≤A、价格靠近，凸显省钱）。
> 本菜单可用券仅「中薯条买一送一」，凑券无法低于四人餐量贩价，故 B 以套餐实现性价比；技能在券真能降价时仍叠加。A/B 人均差仅 ¥1.25，符合「价格靠近、B 凸显性价比」要求。

## 五、技能加载事项（非设计问题）
- `AfterM/SKILL.md` 已正确放到项目级 `Mcdonald Skill/.workbuddy/skills/AfterM/`，格式含 frontmatter（name/description/agent_created）。
- 但本会话中新创建的技能未被 Skill 选择器识别（报「Can not find skill: AfterM」）。需在 WorkBuddy 重新扫描技能（重开 App 或刷新连接器/技能页）后才会出现在技能列表并可被触发。文件本身无误。
