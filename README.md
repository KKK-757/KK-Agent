# KK-Agent

基于币安公开行情与现货 API 的市场分析助手：行情聚合、技术信号（EMA/RSI/MACD/布林带）、
市场报告、现货交易接口（多重安全限制）、币安官方 MCP 接入配置生成。
提供 CLI 与 Web 两种使用方式。

---

## 功能总览

| 方向 | 模块 | 状态 |
|---|---|---|
| 数据与分析 | 实时价格 / K线图（15m~1d 多周期）/ 深度 / 涨跌榜 / 成交额榜 / 振幅榜 / 信号扫描 / 市场报告 | 可用（公开 API，无需 Key） |
| 信号分析 | 复合评分（-4..+4）/ 多周期共振（1h+4h 同向加成）/ 信号扫描 | 可用 |
| 历史回测 | 信号门限策略 vs 买入持有，收益曲线 / 胜率 / 最大回撤 / 手续费 | 可用 |
| 自选列表 | 收藏币种 / 实时涨跌 / 本地持久化 / 信号推送（金叉死叉弹提示） | 可用 |
| 交易工作流 | 现货下单（市价/限价 `at 价格`）/ 余额 / 挂单查询 / 撤单，测试网与正式盘一键切换 | 代码就绪（下单总开关默认关闭） |
| 支付工作流 | x402 可编程支付接口 + Agentic Wallet 绑定指引 | 预留（待钱包授权） |
| 链上自动化 | 质押 / DeFi / 链上操作接口 + 限额风控设计 | 预留（待钱包授权） |
| LLM 推理 | DeepSeek / OpenAI / GLM / Anthropic 等可切换，网页设置面板直接填写 | Key 留槽，填入即生效 |
| 币安 MCP | 官方端点校验 + Claude/Cursor 配置生成器 | 指引（`python -m cli mcp`） |

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 启动
python webapp.py            # Web 应用 → http://127.0.0.1:8788
# 或终端对话
python -m cli chat
```

- 所有市场数据（行情/K线/榜单/扫描/回测）**无需任何 Key**，开箱即用
- 需要下单时，在网页「Key 设置」面板填入币安 API Key（支持模拟盘/正式盘切换、保存前自动验证有效性），立即生效且持久化
- LLM Key 同样在设置面板填写，或编辑 `.env` 后重启

> 本机如果挂了系统代理，浏览器访问 `127.0.0.1:8788` 一般不受影响；
> 命令行测试时请设置 `NO_PROXY=127.0.0.1,localhost`。

Docker 部署（可选）：

```bash
docker compose up -d        # 构建并启动，端口绑定在 127.0.0.1:8788
```

## 架构

**设计思路**：LLM 是脑，工具是手，风控是闸。

```
agent/
├── config.py            # 配置加载 + 运行时热更新（网页改 Key 即时生效并落盘）
├── llm.py               # LLM 抽象：OpenAI 兼容/Anthropic/mock 三通道，Key 留槽
├── risk.py              # 风控安全层：默认拒绝、硬黑名单、限额、环境切换
├── market.py            # 行情数据层：公开 API + 多域名回退 + 榜单/扫描/共振
├── signals.py           # 信号引擎：EMA/RSI/MACD/布林带 + 复合评分（-4..+4）
├── backtest.py          # 历史回测：门限策略 vs 买入持有，无未来函数
├── trading.py           # 交易工作流：签名 API、市价/限价单、Key 验证、撤单
├── payments_onchain.py  # x402 支付 + 链上自动化（接口预留 + 绑定指引）
├── mcp_support.py       # MCP 端点校验（防钓鱼）+ 客户端配置生成器
└── core.py              # Agent 主脑：工具编排 + 对话路由 + 规则引擎回退
cli.py                   # 终端入口（chat/price/signal/report/buy/orders/mcp/...）
webapp.py + web/         # Web 应用（FastAPI + 单页前端：K线/榜单/设置面板）
scripts/                 # git pre-commit 钩子（防密钥误提交）及安装器
tests/                   # 单元测试（风控/信号/市场/回测/配置热更新/共振/自选）
Dockerfile + docker-compose.yml   # 容器化部署
```

**安全设计**：

交易资金侧：
- 下单总开关 `BIN_AGENT_TRADING_ENABLED` 默认 **false**
- 交易环境由 `BIN_AGENT_BINANCE_TRADE_BASE` 一键切换：测试网（虚拟资金）/ 正式盘（真钱）
- 单笔名义金额上限、币种黑名单、仅稳定币计价交易对
- **提币/划转在代码层不存在调用路径**（硬约束，非开关）
- Web 前端禁用下单（硬编码）；正式盘每笔下单需 CLI 手动确认
- MCP 端点防钓鱼校验：非官方地址直接拒绝

Web 服务侧（针对本机服务模型的攻击面）：
- **会话令牌**：Key 设置接口要求服务启动时生成的本机令牌（`/api/auth` 发放，前端自动携带）
- **Host 校验**：写请求的 Host 必须为 `127.0.0.1/localhost`（防 DNS rebinding）
- **同源校验**：浏览器写请求的 Origin/Referer 必须为本机（防 CSRF 跨站伪造）
- **密钥不回显**：接口只返回掩码（`abcd****wxyz`），完整 Key 永不返回
- **git 钩子**：`python scripts/install_hook.py` 安装后，commit 时自动拦截 `.env` 与疑似硬编码密钥

**启用真实交易的步骤（自行承担风险）**：
1. 币安创建 **子账户** API Key，权限只勾「读取 + 现货交易」，**绝不开提币**
2. 网页「Key 设置」选择正式盘并填入 Key（或编辑 `.env`）
3. 勾选「允许下单」，CLI 先用小额测试，`BIN_AGENT_MAX_ORDER_NOTIONAL` 保持低位

## 币安 MCP 接入

```bash
python -m cli mcp    # 输出完整接入指引 + 各客户端配置片段
```

- 官方唯一端点：`https://agent.binance.com/mcp/agentic`
- Claude Code 一行命令、Claude Desktop / Cursor 配置 JSON 自动生成
- 配置前务必核对端点域名，谨防钓鱼

## 测试

```bash
python -m pytest tests/ -q      # 42 passed
```

覆盖：风控全路径（默认拒绝、提币硬禁止、Web 来源拒绝、限额、黑名单、Key 前置）、
信号引擎（指标形态、镜像对称性、金叉/突破）、市场数据（榜单排序、稳定币过滤）、
回测（无未来函数、手续费、净值曲线、胜率统计）、配置热更新（白名单、落盘、内存生效）、
多周期共振（加成与分歧）、自选列表持久化。

## 已知限制

- 信号扫描/回测仅基于公开行情，未接入深度账本与资金费率等衍生品数据
- 回测为单策略门限模型，仅用于验证"信号→执行"链路，不代表实盘收益
- Web 服务设计为本机单人使用，多用户/公网部署需自行加认证层
- Windows 控制台建议 `chcp 65001` 或设置 `PYTHONIOENCODING=utf-8`

## 免责声明

加密资产交易风险极高，本项目代码中的信号与回测输出不构成投资建议，
历史回测收益不代表未来表现。启用真实交易前请充分理解风险，并使用子账户 + 小额限额。
