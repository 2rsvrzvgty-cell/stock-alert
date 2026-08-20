# 多倍策略预警 · GitHub Actions 免费版

把股票预警搬到 GitHub 上免费跑，电脑关机也能每天自动扫描、命中推微信。全程零成本。

## 它是干什么的

每个交易日收盘后（北京时间 15:40），GitHub 的免费服务器会自动：
1. 拉取 watchlist 里每只股票的日线数据（新浪/腾讯行情，无需任何 key）
2. 用多倍股策略内核算回调信号：跌破硬止损、单日大跌、从高点回撤、跌破关键均线
3. 有信号就推送到你的微信（Server酱），没信号默认不打扰

## 你需要准备的两样东西（都要免费）

1. **GitHub 账号**：github.com 注册
2. **Server酱**：sct.ftqq.com 微信扫码登录，拿到一个 SCKEY（一串字符），它负责把消息推到你微信

## 部署步骤（5 分钟）

1. 在 GitHub 上新建一个仓库（Public 或 Private 都行，选 Private 更稳）
2. 把本目录下的文件全部传上去，保持结构：
   ```
   .github/workflows/daily_scan.yml
   gha_scan.py
   gha_config.json
   strategy_core.py
   ```
   不会用 git 命令的话，直接在 GitHub 网页上点 Add file 逐个上传，或把文件夹拖进网页上传都行。
3. 进仓库 Settings → Secrets and variables → Actions → New repository secret
   - Name 填：`SCKEY`
   - Secret 填：你在 Server酱 拿到的 SCKEY
4. 等当天 15:40 自动跑，或手动验证：仓库 Actions 页 → daily_scan → Run workflow（右上角）→ 点一次立即跑

跑完点进日志，看到 "推送结果: OK" 就是通了，微信会收到测试消息。

## 想监控别的股票

编辑 `gha_config.json` 的 `watchlist`，按同样格式加一行：
```json
"sh600000": {"name": "浦发银行", "level": "watch"}
```
保存后下次自动生效。

## 常用设置

- `push_on_clear: true`：没信号时也推一条"今日无预警"，适合刚开始想确认链路
- 北京时间 15:40 跑，改时间请编辑 workflow 里的 cron（注意 GitHub 用 UTC 时间，北京时间减 8 小时）

## 免责声明

仅作技术学习与风控辅助，不构成投资建议。股市有风险，决策自负。
