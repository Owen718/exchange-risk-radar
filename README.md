# Exchange Risk Radar

交易所风险雷达：把多家交易所的公开公告整理成可筛选的事件流，先验证“监控 + 分类 + 展示”的效果。

线上页面：https://owen718.github.io/exchange-risk-radar/

## 现在有什么

- `collector/collect.py`：抓取 Binance、Bithumb、Bybit、OKX、Bitget、KuCoin、Gate、MEXC、Crypto.com、Coinbase Status、Kraken Status 的公开公告，分类、抽取代币、合并历史事件，写入 `docs/data/events.json`。
- `docs/`：纯静态网页，读取 `events.json`，支持按重点/事件类型、交易所、关键词筛选，60 秒自动刷新。
- GitHub Pages 只托管静态页面；采集器可以在本机、VPS 或 GitHub Actions 运行。

## 资源占用

- 单进程顺序采集，无数据库、无浏览器、无队列；默认内存占用约几十 MB。
- 输出是压缩 JSON，900 条事件约 400 KB；前端一次加载无压力。
- 默认最多保留 900 条事件；可用 `--max-events` 调整。
- 建议每 5–10 分钟跑一次，而不是常驻进程；VPS 上用 cron 足够。

```bash
# 每 10 分钟采集一次（示例）
*/10 * * * * cd /opt/exchange-risk-radar && /usr/bin/python3 collector/collect.py >> /var/log/radar.log 2>&1
```

## 北美 VPS 注意

Binance.com、Bybit、MEXC 等会按地区拒绝部分 IP（常见 HTTP 403/451）。采集器会逐源记录状态，失败不会影响其他源。页面上红色数据源标签就是失败源。若北美 VPS 访问不了关键源，可以把采集器放到允许的地区，或只依赖 Coinbase/Kraken/Bithumb 等当前可用的源。

本次 GitHub Actions（美国）实测：Binance、Bithumb、OKX、Bitget、KuCoin、Gate、Coinbase Status、Kraken Status 可用；Bybit 403、MEXC 403/404、Crypto.com 404。数据源失败会在页面上标红，不影响其他源。

## 本地运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r collector/requirements.txt
python collector/collect.py
python3 -m http.server 8080 -d docs
```

打开 `http://localhost:8080`。

## 部署

1. 推到 GitHub。
2. 仓库 Settings → Pages → Source 选 **GitHub Actions**。
3. `Deploy Pages` workflow 会把 `docs/` 发布到 `https://<user>.github.io/exchange-risk-radar/`。
4. `Collect announcements` workflow 每 10 分钟运行一次，更新 `docs/data/events.json`。

注意：GitHub Actions 的托管 runner 在美国，Binance/Bithumb 可能按地区拒绝请求。如果采集失败，页面仍会显示已有数据；正式监控建议把采集器放在首尔/东京的 VPS 或自托管 runner 上，然后把 `events.json` 推回仓库。

## 数据源

- Binance CMS 公开公告接口（catalog 48 / 161 / 49）
- Bithumb 公开公告接口 `GET /v1/notices`

只使用公开接口，不抓需要登录的页面，不绕过访问控制。数据仅作信息展示，不构成投资建议。

## 下一步

- 加入 Upbit（需要合规的韩国账户/API Key）和 Bybit/OKX/Bitget/KuCoin。
- 事件生命周期：监控标签 → 下架/恢复，加入历史统计。
- AI 结构化摘要：异步生成，不阻塞快路径。
- 服务端 API / Webhook。
