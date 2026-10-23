# Claude Usage Guard

[简体中文](readme/README_zh_Hans.md)

A [LangBot](https://github.com/langbot-app/LangBot) plugin that puts a **usage meter and gate** in front of the **Claude Code Runner**. It records how many tokens every call uses and locks the bot when your Claude account or the bot itself has used too much. It only looks at the Claude side, so it works the same for QQ, WeChat, Telegram or any other platform.

> Unofficial community plugin. Not affiliated with or endorsed by Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.

## Three gates – any one locks the bot

| Gate | Data source | Unlocks when |
|---|---|---|
| Account 5-hour usage ≥ your % | `rate_limit_event` that Claude Code prints during each call (real utilization of your subscription) | the account's 5-hour window resets |
| Account 7-day usage ≥ your % | same | the account's 7-day window resets |
| Bot window tokens ≥ your limit | `usage` at the end of each call | the window ends (starts at the first call after the previous window ended, lasts N hours – like Claude's own 5-hour window) |

When locked, **claude is not called**. Instead the wrapper returns your "reply when locked" text as a normal assistant reply, so LangBot and other plugins handle it as usual.

The account usage is checked twice: before the call with the last known numbers, and again during the call as soon as Claude Code reports fresh numbers. If the fresh numbers are already over the limit, that reply is dropped and the lock reply is sent instead (the tokens of that call are still spent and recorded as `suppressed`).

## Requirements

| | |
|---|---|
| LangBot | Self-hosted, tested on **4.11.1** |
| Runner | Official **Claude Code Runner** (`langbot-team/ClaudeCodeAgent`), local location |
| Claude Code CLI | Installed on the same machine; tested on **2.1.300** |
| Account | Account gates need a Claude **subscription** (Pro / Max) – API-key accounts don't report utilization, only the bot-window gate works there |
| OS | Tested on Windows 11 |

## Setup

1. Install the plugin. Set **data directory** to an absolute path, e.g. `D:\langbot-data\usage-guard` (recommended: the default `~/.claude-usage-guard` may end up inside LangBot's per-plugin home directory).
2. The plugin process starts with the first message LangBot routes to a plugin. After that, open `<data dir>/COMMAND.txt`. It contains a line like

   ```
   C:\path\to\python.exe -I D:\langbot-data\usage-guard\bin\claude_meter.py
   ```

3. In each pipeline that uses the Claude Code Runner, replace **command** (`claude`) with that line. Leave the arguments (`args-json`) unchanged.
4. Optional per-pipeline settings in the Runner's `env-json`:

   | Variable | Meaning |
   |---|---|
   | `CUG_BOT` | Name used in the records, e.g. `qq`, `wechat` (default `default`) |
   | `CUG_LOCK_OUTPUT` | Overrides "reply when locked" for this pipeline |
   | `CUG_ERROR_OUTPUT` | Overrides "reply when Claude errors" for this pipeline |

5. Set the limits in the plugin settings. Changes are synced to the wrapper when the next message arrives.

## Settings

| Setting | Default | Notes |
|---|---|---|
| Enable the gate | on | Off = record only |
| Account 5-hour limit (%) | 80 | 0 = no limit |
| Account 7-day limit (%) | 90 | 0 = no limit |
| Bot window length (hours) | 5 | |
| Bot window token limit | 1,000,000 | All calls through the wrapper, all pipelines together. 0 = no limit |
| Count cache-read tokens | on | Cache reads are cheap (about 1/10 of input) but usually the largest number |
| Reply when locked | `（额度暂时用完了，{resets_in}后恢复）` | Placeholders: `{reason}`, `{resets_in}`, `{kind}` |
| Reply when Claude errors | empty | Empty = pass the failure to LangBot unchanged. Otherwise sent as the reply; `{kind}` is `limit` / `refused` / `other` |

## Checking usage

```bash
<command from COMMAND.txt> --status
```

prints JSON: locked or not and why, account 5-hour / 7-day usage and reset times, the bot window's usage, and today's calls, tokens and list-price cost per bot.

Files in the data directory:

| File | Content |
|---|---|
| `config.json` | Copy of the plugin settings, written by the plugin – don't edit by hand |
| `usage.db` | Tokens and cost per call, account usage snapshots (kept 10 days, no chat content) |
| `events.log` | One line per lock and per Claude error |
| `bin/` | The wrapper, updated automatically when the plugin is upgraded |
| `COMMAND.txt` | The Runner command to use |

## Known limitations

- Account usage is refreshed only when the bot calls claude. Usage elsewhere (e.g. your own Claude Code sessions) becomes visible on the bot's next call; the in-call check makes sure that call's reply is not sent if the account is already over the limit, but its tokens are spent.
- To do the in-call check, the wrapper holds claude's output until the usage numbers arrive, so streaming output starts slightly later.

## Privacy

Only token counts, cost and account utilization are stored, locally in the data directory. No chat content. The plugin itself makes no network requests; claude talks to Anthropic as usual.

## Development

```bash
pip install langbot-plugin pytest
pytest
lbp build
```

`meter/` uses only the standard library and is shared by the plugin and the wrapper (`tools/claude_meter.py`).

## License

[Apache-2.0](LICENSE)
