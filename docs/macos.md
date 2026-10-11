# macOS：走 GreenBubbles（不适用本仓库的流程）

> **本仓库的流程（`src/`、`tools/`）只覆盖 Windows + 微信 4.1.x。**
> 在 Mac 上不要照搬——两台机器上微信的数据与加密机制不同。

Mac 上目前没有"自己实现一遍"的成熟路径。可用的方案是现成的开源工具
**[GreenBubbles](https://github.com/bojieli/greenbubbles)**（作者李博杰，MIT，Rust + Swift，~370 star）。

**不懂技术、只想照做**：直接用 [`prompt-oneclick.md`](prompt-oneclick.md) 文末的
《附：Mac 用户流程》——那里有可以整份粘给 AI 助手的三步走提示词。

---

## 前提（缺一不可）

| 项 | 要求 |
|---|---|
| 芯片 | **Apple 芯片**（M1/M2/M3/M4）。**Intel Mac 官方未构建、未测试** |
| 系统 | **macOS 14 或更新** |
| 微信 | **4.1 或更新** |
| 权限 | **管理员（`sudo`）** |
| 工具 | `lldb`（`xcode-select --install`） |
| 状态 | 官方自述 **research alpha**（面向能读懂 JSON、能判断"完整/不完整"标记的技术用户） |

## 安装

```sh
brew tap bojieli/greenbubbles https://github.com/bojieli/greenbubbles.git
brew install bojieli/greenbubbles/greenbubbles
```

若提示公式不受信任：`brew trust --formula bojieli/greenbubbles/greenbubbles` 后重试。
也可以用 [Releases](https://github.com/bojieli/greenbubbles/releases) 里的 **DMG 应用版**
（Developer ID 签名 + 苹果公证）。

## 取密钥（一次性）

```sh
greenbubbles-acquire preflight                     # 自检，会直接告诉你缺什么
sudo codesign --force --deep --sign - /Applications/WeChat.app   # 重新签名微信（用户自己跑）
# 退出微信 → 重新打开
sudo greenbubbles-acquire capture                  # 抓取
# 在 5 分钟窗口内：微信里「退出登录」→ 重新登录
```

密钥落在 `~/.greenbubbles-acquire/passphrase.txt`（仅本人可读），**之后命令无需额外参数**。

## 日常读取

```sh
greenbubbles chats                                  # 列出会话
greenbubbles chats find "Alice"                     # 按昵称/备注/微信号/ID 找会话
greenbubbles messages list --conversation "Alice" --json
greenbubbles messages search --conversation "Alice" --query "关键词"
greenbubbles messages list --conversation "Alice" --since <unix秒> --until <unix秒>
greenbubbles source status                          # 只查存储与访问状态，不读正文
```

输出为 JSON Lines；`hasMore: true` 时用 `--cursor <nextCursor>` 继续翻页。

## Agent 集成（它的强项）

GreenBubbles 自带 skill，把 skill 路径给 agent 即可（Claude Code / Codex / Gemini CLI / Kimi / Grok 等）：

```sh
echo "$(brew --prefix greenbubbles)/libexec/skills/greenbubbles-personal-memory/SKILL.md"
# 或装进 agent 的 skills 目录：
python3 "$(brew --prefix greenbubbles)/libexec/scripts/install-skills.py" --agent codex
```

## 两个方案的关键差异

| | 本仓库（Windows） | GreenBubbles（macOS） |
|---|---|---|
| 取密钥 | 读 `Weixin.exe` 内存，解 XOR 混淆的 keyspec | `LLDB` attach + 微信**重新签名** + 登录时抓取 |
| 对微信的改动 | **零**（纯只读内存与文件副本） | **替换 WeChat.app 的苹果签名**（不影响聊天数据；升级后恢复） |
| 是否需管理员 | 需要（读进程内存） | 需要（`sudo`） |
| 成熟度 | 一路实测跑通 | 官方自称 research alpha |
| 产出 | jsonl / md / csv / **单文件 HTML** | 实时查询 / 加密快照 / agent 写 Markdown 笔记 |

> 也就是说：**Windows 的路子"干净"但封闭在 Windows；Mac 的路子省了自己实现，代价是要动 App 签名。**

## 已知限制（来自官方 docs/KNOWN_LIMITATIONS.md）

- 搜索慢：用不上微信自己的索引时，只直接搜最近 500 条。
- 跨库非原子：多库依次读取，输出会标 `crossDatabaseAtomic: false`；要固定视图应读**备份快照**。
- 联系人名最多解析 500 个，解析不到就显示原始 `wxid_…` 并标 `contactDisplayNameUnresolved`。
- 视频/文档定位是 best-effort（改过名或移动过可能找不到）。
- **备份快照不含图片、视频、文档**（含语音，因为语音存在库里）。
- 旧备份不会自动删除，会持续占空间。
- 公开版本**不能发消息**。

## 隐私

- 数据、密钥、备份、笔记**全部留在本机**，无遥测。
- 但**选了云端 AI 时，它读到的消息会发给该模型提供方**；内置摘要器会把允许的会话发给 Google Gemini（需自备 `GEMINI_API_KEY`，另行计费）。
- 官方明确提醒：**不要把数据库密钥或恢复短语粘进任何提示词。**

## 参考

- 仓库：https://github.com/bojieli/greenbubbles
- 文档索引：`https://cdn.jsdelivr.net/gh/bojieli/greenbubbles@main/docs/README.md`
- 密钥获取：`.../docs/PASSPHRASE_ACQUISITION.md`　常见问题：`.../docs/FAQ.md`
- 已知限制：`.../docs/KNOWN_LIMITATIONS.md`　威胁模型：`.../docs/THREAT_MODEL.md`
- 隐私：`.../PRIVACY.md`

> GreenBubbles 是独立第三方项目，与腾讯无关。本文仅为"Mac 用户该怎么办"的指路，未在本机实测（本机为 Windows）。
