# AfterM 高德增强层参考（map-integration）

> 增强层：高德 Web 服务（官方 MCP）。属「探测可用即用」，缺失时通勤降级静态估算并标注「估算」，不阻断主流程。
> 本文件**不含任何 key 或含 key 的完整 URL** —— key 只存本机 MCP 配置。

## 一、为什么需要它

麦当劳 MCP 的 `query_nearby_stores` 返回的门店**经纬度实测全为 0**，无法直接用于路径规划。
因此通勤链路必须是：

```text
门店/场馆「地址文本」 → maps_geo 地理编码（得坐标） → 路径规划（步行/骑行/驾车/公交）→ 通勤对比表
```

## 二、接入方式（本机）

- 服务器别名：`amap-mcp`；传输：streamable HTTP。
- 配置只存本机 `~/.workbuddy/mcp.json`；**key 内嵌在 URL 中，视同密钥**。
- 纪律：**完整 URL（含 key）不得写入任何技能文件、回执、对话展示或参赛材料**；
  引用时只说服务器别名 `amap-mcp`。
- 启用：WorkBuddy 左侧【专家·技能·连接器】→【连接器】→ 找到 `amap-mcp` → 点【信任/启用】。

## 三、本技能使用的工具（常用 5 + 增值 2）

| 工具 | 用途 | 关键入参 |
|---|---|---|
| `maps_geo` | 地址/地名 → 坐标 | `address`（或 `city`+`address`） |
| `maps_direction_walking` | 步行路径 | `origin`(lng,lat) `destination`(lng,lat) |
| `maps_direction_bicycling` | 骑行路径 | 同上 |
| `maps_direction_driving` | 驾车路径（含过路费） | 同上 |
| `maps_transit_integrated` | 公交/地铁整合（**MCP 返回体仅线路/耗时/上下车站**；票价与末班车需走原始服务补取，见第七节） | 同上 + `city`、可选 `cityd` |
| `maps_schema_take_taxi` | 生成打车唤起链接（增值） | 起点终点 |
| `maps_schema_navi` | 生成导航唤起链接（增值） | 终点等 |

> 其余 `maps_around_search` / `maps_text_search` / `maps_weather` 等本技能不使用。

## 四、冒烟判据

- `maps_geo("杭州市奥体中心")` → 应返回 `120.232974,30.224071`。
- 实测（REST 直连复核）：奥体中心 `120.232974,30.224071`（公交地铁站点级）；
  汉庭（市心北路×××号）`120.261422,30.232897`（门址级）。二者与设计案实测值一致。

## 五、通勤实测基线（奥体中心 → 汉庭，演示场景）

| 方式 | 距离 | 耗时 | 花费 | 末班车 |
|---|---|---|---|---|
| 步行 | 2.9 km | ≈49 分钟 | ¥0 | — |
| 骑行 | 3.76 km | ≈15 分钟 | ≈¥1.5–3（估算） | — |
| 驾车 | 3.66 km | ≈882 秒 | 过路费 ¥0；**打车估价 ¥12**（原始服务 `taxi_cost`，标注「以叫车平台为准」） | — |
| 地铁 | 6号线 → 2号线 | ≈27 分钟（步行 486m） | **¥3.0**（原始服务 `cost`） | 奥体中心站 **23:32**；换乘 2 号线钱江世纪城站 **23:54**（`station_end_time`） |

> **未赶末班判据**：`missed=1` 直接排除该方案；`散场 + 步行到站 + 等车 > 该站末班` → 标「风险高/赶不上」。

## 六、⚠️ 关键实测：高德 MCP 会裁剪返回字段（2026-10-16 发现）

**问题**：`amap-mcp` 的 `maps_transit_integrated` 返回体是**服务端裁剪过的瘦身版**，
只剩 `name / distance / duration / departure_stop / arrival_stop / via_stops`，
把下面这些**有用的字段全删了**：

| 被裁掉的字段 | 含义 | 实测值（奥体中心 → 汉庭） |
|---|---|---|
| `cost` | **票价** | **3.0**（¥3） |
| `start_time` / `end_time` | **线路首末班** | 0601 / 2230 |
| `station_start_time` / `station_end_time` | **该站首末班（末班车）** | 奥体中心站末班 **2332**；钱江世纪城站（换2号线）末班 **2354** |
| `missed` | **是否赶不上末班** | 0（未赶不上） |
| `nightflag` | 是否夜班车 | 0 |
| `route.taxi_cost` | **打车估价** | **12**（¥12） |

**结论**：这些数据**不是高德没有，是 MCP 封装丢了**。
→ **需要票价 / 末班车 / 打车估价时**，按同一路径改走**高德原始 Web 服务**：

```text
GET https://restapi.amap.com/v3/direction/transit/integrated
     ?origin=<lng,lat>&destination=<lng,lat>&city=<city>&cityd=<city>&key=<KEY>&strategy=0&nightflag=1
```

- 返回 `route.taxi_cost` + `route.transits[].cost / missed / nightflag` +
  `segments[].bus.buslines[].start_time / end_time / station_start_time / station_end_time`。
- **key 只从本机配置读取**（`amap-mcp` URL 内嵌），**禁止写入任何技能文件 / 回执 / 对话 / 参赛材料**。
- 若受限不能直连原始服务 → 降级：票价「以实际为准」、末班车提示人工确认，**绝不编造**。

### 6.1 ⚠️ 打车估价的正确取法（2026-10-17 更正）

此前一度误判「泰州不支持打车估价」，实为**自身调用参数/层级用错**，已 12 城复核推翻：

```text
GET https://restapi.amap.com/v3/direction/driving
     ?origin=<lng,lat>&destination=<lng,lat>&extensions=all     # ← 必须 all
```

| 坑 | 症状 | 正解 |
|---|---|---|
| 用 `extensions=base` | `route.taxi_cost` = `null`（连杭州也 null） | 改 **`extensions=all`** |
| 读 `paths[0].taxi_cost` | 该层级**本就没有**此字段 → `null` | 读 **`route.taxi_cost`**（路由层） |

- 12 城实测（`extensions=all`）：杭州 **17** / 泰州 **8、23** / 南京 **30** / 扬州 **30** / 镇江 **9** /
  盐城 **20** / 苏州 **12** / 无锡 **17** / 常州 **20** / 南通 **17** / 徐州 **30** / 合肥 **18** —— **全部可取**。
- 交叉校验：`transit/integrated` 的 `route.taxi_cost` 与驾车值一致。
- ⇒ 打车费按**真值**呈现，**不要**再一律标「≈估算」。

## 七、降级规则（异常分支 H）

高德调用失败 / key 缺失 / 超时 →
- 通勤改为**静态估算**：距离 ÷ 经验速度（步行 ~1.2m/s、骑行 ~4m/s、驾车 ~城市均速）+ 费率表；
- 结果**必须标注「估算」**；
- **不阻断主流程**，方案与接头卡照常输出。
