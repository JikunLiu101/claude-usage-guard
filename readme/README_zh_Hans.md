# Claude 用量闸门（Claude Usage Guard）

[English](../README.md)

一个 [LangBot](https://github.com/langbot-app/LangBot) 插件：给 **Claude Code Runner** 加一道**用量记录与闸门**。它记下每次调用用了多少 token，当你的 Claude 账号或者机器人自己用得太多时就锁定机器人。它只看 Claude 这一侧，所以接 QQ、微信、Telegram 还是别的平台都一样。

> 非官方社区插件，与 Anthropic 无关，也未获其认可。"Claude""Claude Code" 是 Anthropic 的商标。

## 三道闸门，任一超过就锁定

| 闸门 | 数据来源 | 何时解锁 |
|---|---|---|
| 账号 5 小时占用 ≥ 设定百分比 | 每次调用时 Claude Code 输出的 `rate_limit_event`（订阅账号的真实占用） | 账号 5 小时窗口重置 |
| 账号 7 天占用 ≥ 设定百分比 | 同上 | 账号 7 天窗口重置 |
| 机器人窗口内 token ≥ 设定上限 | 每次调用结束时的 `usage` | 窗口结束（从上个窗口结束后的第一次调用开始计时，满 N 小时清零，和 Claude 自己的 5 小时窗口一样） |

锁定时**不调用 claude**，而是把"锁定时的回复"当作一次正常回复交给 LangBot，后面的插件、发送照常处理。

账号占用会检查两次：调用前用上一次记下的数字查一次；调用中 Claude Code 一报出最新数字就再查一次。最新数字已经超限，就丢掉这次回复、改发锁定回复（这次调用的 token 照样花掉，记为 `suppressed`）。

## 运行要求

| | |
|---|---|
| LangBot | 自建部署，在 **4.11.1** 上测试 |
| Runner | 官方 **Claude Code Runner**（`langbot-team/ClaudeCodeAgent`），本机运行模式 |
| Claude Code 命令行 | 装在同一台机器上，在 **2.1.300** 上测试 |
| 账号 | 账号闸门需要 Claude **订阅**（Pro / Max）；API Key 账号不报告占用，只有机器人窗口那道闸门生效 |
| 系统 | 在 Windows 11 上测试 |

## 安装与配置

1. 安装插件，把**数据目录**设成绝对路径，例如 `D:\langbot-data\usage-guard`（建议填写：默认的 `~/.claude-usage-guard` 可能落在 LangBot 给插件单独分配的 home 目录里，不好找）。
2. LangBot 第一次把消息交给插件处理时，插件进程才会启动。之后打开 `<数据目录>/COMMAND.txt`，里面有一行类似

   ```
   C:\path\to\python.exe -I D:\langbot-data\usage-guard\bin\claude_meter.py
   ```

3. 在每条用 Claude Code Runner 的流水线里，把 **command**（原来是 `claude`）换成这一行。参数（`args-json`）不用改。
4. 可选：在 Runner 的 `env-json` 里给单条流水线设置

   | 变量 | 含义 |
   |---|---|
   | `CUG_BOT` | 记账用的名字，例如 `qq`、`wechat`（默认 `default`） |
   | `CUG_LOCK_OUTPUT` | 覆盖这条流水线的"锁定时的回复" |
   | `CUG_ERROR_OUTPUT` | 覆盖这条流水线的"Claude 出错时的回复" |

5. 在插件配置里设好各项上限。改动会在下一条消息到来时同步给包装脚本。

## 配置项

| 配置项 | 默认 | 说明 |
|---|---|---|
| 启用闸门 | 开 | 关闭时只记录不锁定 |
| 账号 5 小时额度上限（%） | 80 | 0 表示不限 |
| 账号 7 天额度上限（%） | 90 | 0 表示不限 |
| 机器人窗口长度（小时） | 5 | |
| 机器人窗口 token 上限 | 1,000,000 | 所有流水线经过包装脚本的调用合计。0 表示不限 |
| 缓存读取也计入 | 开 | 缓存读取很便宜（约为输入的 1/10），但数量通常最大 |
| 锁定时的回复 | `（额度暂时用完了，{resets_in}后恢复）` | 占位符：`{reason}`、`{resets_in}`、`{kind}` |
| Claude 出错时的回复 | 空 | 空 = 出错原样交给 LangBot；填了就当作回复发出，`{kind}` 为 `limit` / `refused` / `other` |

## 查看用量

```bash
<COMMAND.txt 里的命令> --status
```

输出 JSON：是否锁定及原因、账号 5 小时 / 7 天占用和重置时间、机器人窗口用了多少，以及今天各机器人的调用次数、token 和按标价折算的费用。

数据目录里的文件：

| 文件 | 内容 |
|---|---|
| `config.json` | 插件配置的副本，由插件写入，不要手改 |
| `usage.db` | 每次调用的 token 和费用、账号占用快照（保留 10 天，不含聊天内容） |
| `events.log` | 每次锁定、每次 Claude 出错各一行 |
| `bin/` | 包装脚本，插件升级时自动更新 |
| `COMMAND.txt` | Runner 应该填的命令 |

## 已知限制

- 账号占用只在机器人调用 claude 时刷新。别处（比如你自己用 Claude Code）用掉的额度，要到机器人下一次调用时才看得到；调用中的那次检查能保证账号已超限时这次回复不会发出，但这次的 token 会花掉。
- 为了做调用中的检查，包装脚本会先扣住 claude 的输出，等占用数据到达，所以流式输出会晚开始一点。

## 隐私

只在本机数据目录里记 token 数、费用和账号占用，不记聊天内容。插件本身不访问网络；claude 照常连接 Anthropic。

## 开发

```bash
pip install langbot-plugin pytest
pytest
lbp build
```

`meter/` 只用标准库，插件和包装脚本（`tools/claude_meter.py`）共用。

## 许可证

[Apache-2.0](../LICENSE)
