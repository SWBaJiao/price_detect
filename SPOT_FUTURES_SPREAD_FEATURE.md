# 现货-合约价差监控功能说明

## 功能概述

新增现货与合约价格差异监控功能，当现货价格与合约价格的差异超过设定阈值时，系统会推送告警通知，帮助识别潜在的套利机会。

## 核心特性

### 1. 价差计算

价差计算公式：
```
价差百分比 = (现货价格 - 合约价格) / 合约价格 × 100%
```

- **正值**：现货溢价（现货价格 > 合约价格）
- **负值**：合约溢价（合约价格 > 现货价格）

### 2. 独特的告警格式

现货-合约价差告警使用**独特的消息格式**，与合约价格异动告警明显区分：

**价差告警格式**：
```
══════════════════════════════
🔺 *现货-合约价差异动* 🔺
══════════════════════════════

🪙 币种: `BTCUSDT`
📊 层级: 大盘

💵 现货价格: $43,250.50
⚡ 合约价格: $43,100.00

📊 价差: *+0.35%* (现货溢价)
⚠️ 阈值: 0.30%
⏱ 检测窗口: 60秒

🕐 时间: 2026-01-05 15:30:45

⚡ *套利机会提示* ⚡
• 价差已超过阈值 1.2 倍

══════════════════════════════
💬 回复 `/info BTC` 查看详情
```

**对比：普通合约异动告警**：
```
📈 *价格异动告警*

📌 币种: `BTCUSDT`
📊 层级: 大盘
💵 价格: $43,100.00
📈 变化: +2.5%
⚡ 阈值: 2.5%
⏱ 窗口: 60秒
🕐 时间: 15:30:45

💬 回复 `/info BTC` 查看K线详情
```

### 3. 智能套利提示

当价差超过阈值 1.5 倍时，系统会自动添加"套利机会提示"，帮助快速识别重大机会。

## 配置说明

### 配置文件位置
`config/settings.yaml`

### 配置项

```yaml
alerts:
  # 现货-合约价差告警
  spot_futures_spread:
    enabled: true             # 是否启用
    threshold: 0.3            # 价差阈值（%）
    time_window: 60           # 检测窗口（秒）
    poll_interval: 30         # 现货价格轮询间隔（秒）
```

### 参数说明

| 参数 | 类型 | 默认值 | 说明 |
|-----|------|--------|------|
| `enabled` | bool | true | 是否启用价差监控 |
| `threshold` | float | 0.3 | 价差阈值，单位为百分比（%） |
| `time_window` | int | 60 | 检测窗口时间（秒） |
| `poll_interval` | int | 30 | 现货价格更新频率（秒） |

### 调整建议

**大盘币种**（BTC、ETH）：
```yaml
threshold: 0.2  # 阈值设低一些，捕捉小幅价差
```

**中小盘币种**：
```yaml
threshold: 0.5  # 阈值可以稍高，避免频繁告警
```

**高频监控**：
```yaml
poll_interval: 15  # 缩短轮询间隔
```

## 技术实现

### 架构组件

1. **binance_client.py**
   - 新增现货 API 接口：
     - `get_spot_ticker_24h()` - 获取单个现货价格统计
     - `get_all_spot_tickers()` - 批量获取所有现货价格
     - `get_spot_price()` - 获取单个现货价格

2. **models.py**
   - 新增 `AlertType.SPOT_FUTURES_SPREAD` 告警类型
   - 新增 `SpotTickerData` 现货数据模型
   - 新增 `_format_spread_message()` 价差告警格式化方法

3. **price_tracker.py**
   - 新增现货价格追踪功能
   - 新增 `update_spot_price()` 更新现货价格
   - 新增 `batch_update_spot_prices()` 批量更新
   - 新增 `get_spot_futures_spread()` 计算价差

4. **alert_engine.py**
   - 新增 `check_spot_futures_spread()` 价差检测逻辑
   - 集成到 `check_all()` 统一检测流程

5. **main.py**
   - 新增 `_poll_spot_prices()` 现货价格轮询任务
   - 自动启动/停止价格轮询

### 数据流程

```
现货价格轮询 (每30秒)
    ↓
批量获取现货价格
    ↓
更新 PriceTracker
    ↓
合约行情推送 (实时)
    ↓
计算价差
    ↓
检测是否超阈值
    ↓
触发告警 → Telegram 推送
```

## 使用示例

### 1. 启用功能

编辑 `config/settings.yaml`：
```yaml
alerts:
  spot_futures_spread:
    enabled: true
    threshold: 0.3
```

重启应用：
```bash
python main.py
```

### 2. 查看日志

```bash
# 查看价差告警日志
tail -f logs/monitor.log | grep "现货-合约价差"

# 输出示例：
# 15:30:45 | INFO | [现货-合约价差] BTCUSDT: +0.35% (现货溢价)
```

### 3. 接收告警

当价差超过阈值时，Telegram 会收到独特格式的推送消息。

## 监控要点

### 关注币种

建议重点监控以下币种的价差：

1. **主流币**：BTC、ETH、BNB
   - 流动性好，价差持续时间短
   - 适合快速套利

2. **热门币**：SOL、AVAX、MATIC
   - 成交量大，机会频繁
   - 需要快速反应

3. **新上线币**：
   - 价差波动大
   - 风险较高，谨慎操作

### 价差解读

| 价差范围 | 含义 | 操作建议 |
|---------|------|---------|
| 0.2% - 0.5% | 正常波动 | 观察 |
| 0.5% - 1.0% | 小型套利机会 | 考虑操作 |
| > 1.0% | 重大机会 | 快速行动 |
| > 2.0% | 异常情况 | 谨慎判断 |

### 风险提示

⚠️ **注意事项**：

1. **时间差**：现货价格每30秒更新一次，可能有短暂延迟
2. **交易费用**：套利需要考虑交易手续费和资金费率
3. **滑点风险**：大额交易可能面临滑点
4. **市场波动**：极端行情下价差可能快速消失
5. **资金成本**：合约持仓需要考虑资金费率

## 性能优化

### 批量更新

系统使用批量 API 获取所有现货价格，单次请求即可获取全部数据，性能优异：

- ✅ 批量获取：1次请求 = 所有币种
- ❌ 逐个获取：N个币种 = N次请求

### 智能过滤

只监控合约已追踪的币种，避免浪费资源。

### 内存管理

现货价格历史数据使用 deque 自动限制长度（maxlen=100），防止内存溢出。

## 故障排查

### 问题 1: 没有收到价差告警

**排查步骤**：

1. 检查配置是否启用：
   ```yaml
   spot_futures_spread:
     enabled: true  # 确认为 true
   ```

2. 检查阈值设置：
   ```yaml
   threshold: 0.3  # 是否设置过高？
   ```

3. 查看日志：
   ```bash
   grep "现货价格轮询" logs/monitor.log
   ```

### 问题 2: 现货价格轮询失败

**可能原因**：
- 网络连接问题
- API 访问受限
- 现货 API endpoint 变更

**解决方法**：
1. 检查网络连接
2. 运行连接测试：`python test_connection.py`
3. 查看错误日志：`tail -f logs/monitor.log | grep ERROR`

### 问题 3: 价差计算不准确

**可能原因**：
- 现货价格更新延迟
- 轮询间隔过长

**解决方法**：
缩短轮询间隔：
```yaml
poll_interval: 15  # 从 30 秒改为 15 秒
```

## API 端点

### 现货 API

- **批量价格**：`GET https://api.binance.com/api/v3/ticker/price`
- **24h 统计**：`GET https://api.binance.com/api/v3/ticker/24hr`

### 合约 API（已有）

- **合约价格**：通过 WebSocket 实时推送
- **持仓量**：`GET https://fapi.binance.com/fapi/v1/openInterest`

## 扩展开发

### 自定义告警格式

修改 `src/models.py` 中的 `_format_spread_message()` 方法：

```python
def _format_spread_message(self) -> str:
    # 自定义你的消息格式
    pass
```

### 添加其他交易所

在 `binance_client.py` 中添加其他交易所的现货 API：

```python
async def get_okx_spot_price(self, symbol: str) -> Optional[float]:
    # 实现 OKX 现货 API
    pass
```

### 价差历史分析

在 `price_tracker.py` 中添加价差历史记录：

```python
def get_spread_history(self, symbol: str, window: int) -> List[float]:
    # 返回历史价差数据
    pass
```

## 更新日志

### v2.0 - 2026-01-05

- ✨ 新增现货-合约价差监控功能
- ✨ 独特的价差告警格式，与合约告警明显区分
- ✨ 智能套利机会提示
- ✨ 批量现货价格获取
- ✨ 可配置的监控阈值和轮询间隔
- 🔧 优化 SSL 连接配置
- 🔧 添加请求重试机制

---

**作者**: Claude Code
**日期**: 2026-01-05
**版本**: v2.0
