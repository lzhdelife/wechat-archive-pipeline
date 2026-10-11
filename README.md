# wechat-archive-pipeline

> 只读导出**你自己**的微信本地聊天记录（**Windows 4.1.x**；macOS 见 [`docs/macos.md`](docs/macos.md)），
> 输出 jsonl / md / csv（+ 可选 html 阅读版）。
> 不注入、不 Hook、不修改微信。适用于个人归档与让 AI 助手了解自己。

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> **不懂技术？** 别往下读了 —— 打开 [`docs/prompt-oneclick.md`](docs/prompt-oneclick.md)，
> 全选复制、粘给你的 AI 助手（如 WorkBuddy）、改一行联系人的名字，剩下的事它自己做。
> 本文以下内容是给"要自己跑/要读代码"的人看的。

---

## 平台：本仓库覆盖 Windows；Mac 请走 GreenBubbles

| 你的电脑 | 走哪条路 |
|---|---|
| **Windows**（微信 4.1.x） | **本仓库**：`src/` + `tools/`，见下 |
| **macOS（Apple 芯片 + 14+）** | **本仓库流程不适用** → 用 [GreenBubbles](https://github.com/bojieli/greenbubbles)，见 [`docs/macos.md`](docs/macos.md) |
| macOS（Intel）/ 手机 / 平板 | 目前没有可行方案 |

> 两台机器上微信的**数据与加密机制不同**（Mac 走 LLDB 抓取 + 重新签名微信，Windows 走读进程内存 + XOR keyspec），
> 所以 Mac 上**不要照搬**本仓库的取密钥思路。

## 自动跑（不懂技术看这里）

`docs/prompt-oneclick.md` 是一份**面向非技术用户的一键提示词**：把整份文件粘给支持本地文件操作的
AI 助手（WorkBuddy 等），它会自己探测环境 → 取密钥 → 解密 → 找到联系人 → 导出网页版聊天记录。
**它同时含 Windows 与 macOS 两条流程**，AI 会按系统自己选对应的那条。

Windows 前提：

| # | 前提 |
|---|---|
| 1 | Windows 电脑（Mac 见 `docs/macos.md`；手机不行）|
| 2 | 装了会用本地文件的 AI 助手（如 WorkBuddy）|
| 3 | 电脑版微信已登录你自己，且**正在运行**（取密钥需要）|
| 4 | 想读**完整历史**，需先在**手机**上做「**迁移到电脑微信**」（见坑 10）|

提示词里已把本仓库踩过的坑全部前置（XOR keyspec、判据不能用 SQLite 魔数、分片 Name2Id 独立、
找人的内容反查法、跳过 `*_fts.db`、CSV BOM、WAL 顺序、管理员权限引导、失败兜底……），
所以照着跑通常一趟就能过。

Mac 前提（更严）：Apple 芯片、macOS 14+、微信 4.1+、**管理员权限**，且工具会**替换 WeChat.app 的苹果签名**。
详见 [`docs/macos.md`](docs/macos.md)。

---

## ⚠️ 边界（请先读）

- 只支持读取**本人账号、本人设备、本地文件**。
- 密钥只存在于**当前登录者**的进程内存里——别人的电脑上这套方法无法工作（这是微信的加密设计）。
- 请遵守当地法律法规与平台服务条款，仅供个人数据管理使用。

## 工作原理

```
微信在运行（登录在线）
   ↓  只读扫描进程内存里的 Config.Cipher / keyspec（XOR 混淆，字符串扫不到）
取出本次登录的数据库密钥 → 立刻落盘
   ├─ Windows runtime 扫描：直接得到 raw key（x'<64hex key><32hex salt>'）
   └─ 其他平台 / passphrase 路线：PBKDF2-HMAC-SHA512(passphrase, salt, 256000, 32)
   ↓  HMAC-SHA512 校验（mac_key = PBKDF2-SHA512(key, salt^0x3A, 2, 32)）
【可选保险】请用户退出微信（触发 WAL 收尾 checkpoint；见坑 9）
   ↓
解密数据库【副本】（原文件不动）
   ↓  跨所有 message_N.db 分片合并 + 指纹去重
导出 jsonl / md / csv（+ 可选 html 阅读版）
```

> ⚠️ 校验密钥对错时**不要**看 `SQLite format 3` 魔数——page1 前 16 字节被 salt 占着，
> 解密后根本看不到它。判据是「页头字段 + HMAC-SHA512」，详见 [`docs/pitfalls.md`](docs/pitfalls.md) 坑 11。
>
> ⚠️ **顺序**：先取密钥、再关微信、后解密。关微信不是必需的，但它触发 WAL 合并；而一旦关掉，
> 密钥就取不到了。详见坑 9。

支持两种会话类型：

| 类型 | 消息表 | 发言归属判定 |
|---|---|---|
| 群聊 | `Msg_<md5(群ID)>` | `Name2Id` 映射 + 白名单 |
| 私聊 | `Msg_<md5(对方wxid)>` | `Name2Id.is_session`（0=我，1=对方）|

## 环境要求

- Windows 10/11，管理员权限
- 微信 Windows 版 **4.1.x**（登录运行中）
- Python 3.10+
- 依赖：`pycryptodome` `psutil` `zstandard`

```bash
pip install pycryptodome psutil zstandard
```

## 快速开始

### 1. 提取密钥（管理员命令行）

```bash
python tools/vendor/wcdb_key_tool_windows.py -v extract \
    --db-dir "C:/Users/<你>/xwechat_files/<你的wxid>_<后缀>/db_storage"
```

成功标志：`matched N/N salts`。产物 `all_keys.json`。

> ⚠️ `-v` 必须放在 `extract` 前面。密钥每次登录可能变化，失效就重取。
> 想用一个 ~250 行的精简替代实现（不依赖版本相关掩码常量），见下方 [tools/lite](#精简取密钥参考实现toolslite)。

### 2. 配置

群聊编辑 `config.json`，私聊编辑 `config_pm.json`：

```json
{
  "peer_wxid": "对方或群的 ID",
  "db_storage": "C:/Users/<你>/xwechat_files/<wxid>_<后缀>/db_storage",
  "chat_name": "导出文件名"
}
```

### 3. 运行

```bash
# 群聊
python src/sync.py

# 私聊
python src/sync_pm.py --inspect   # 先自检
python src/sync_pm.py             # 正式归档

# 可选：生成单文件 HTML 阅读版（米色纸感、可搜索、只看某人）
python src/gen_html.py --jsonl "data/archive/与某人.jsonl" --peer-name "对方"
```

输出到 `data/archive/`。

## 目录结构

```
├── src/
│   ├── sync.py            群聊归档引擎
│   ├── sync_pm.py         私聊归档引擎
│   ├── gen_html.py        jsonl → 单文件 HTML 阅读版
│   ├── config.json        群聊配置
│   └── config_pm.json     私聊配置
├── tools/
│   ├── vendor/            密钥提取（上游 wcdb-key-tool）
│   └── lite/              精简取密钥/解密参考实现（~250 行，见下）
└── docs/
    ├── prompt-oneclick.md 面向非技术用户的一键提示词（Windows + macOS 两条流程）
    ├── macos.md           macOS 走 GreenBubbles 的说明与限制
    ├── pitfalls.md        14 个踩坑清单
    └── quickstart.md      快速开始
```

## 精简取密钥参考实现（tools/lite）

[`tools/lite/wcdb_key_lite.py`](tools/lite/wcdb_key_lite.py) 是 vendor 版密钥工具的精简替代：
约 250 行纯 Python（只依赖 `pycryptodome`），把"取密钥 → 解密"讲清楚。

- **不依赖运行时对象结构**，也不依赖任何**版本相关的固定掩码常量**；
- 用「**已知明文反推 pad**」：keyspec 尾部 32 字节就是本库 salt 的 ASCII hex，
  据此当场解出 XOR pad，再取 `[2:66]` 得到 raw key（详见 [`docs/pitfalls.md`](docs/pitfalls.md) 坑 12）；
- CLI 与 vendor 版对齐，`decrypt` 的进度行格式一致，可直接用环境变量切进 `src/sync*.py`：

```bash
python tools/lite/wcdb_key_lite.py extract --db-dir "<...>/db_storage"
python tools/lite/wcdb_key_lite.py decrypt --db-dir "<...>/db_storage" \
    --keys all_keys.json --output data/decrypted

KEY_TOOL_PY=python TOOL_PY=tools/lite/wcdb_key_lite.py python src/sync_pm.py
```

## 已知坑（完整版见 docs/pitfalls.md）

| # | 坑 |
|---|---|
| 1 | 4.1+ 内存中无明文密钥，旧工具全部失效 |
| 2 | 密钥 XOR 混淆，需以运行时对象视角读取（也可用已知明文反推，见 12）|
| 3 | 同一消息表跨 5 个分片，需合并 + 指纹去重 |
| 4 | **各分片 Name2Id rowid 独立，必须按分片解析发送者** |
| 5 | `local_type` 高 32 位是子类型 |
| 6 | 内容可能 zstd 压缩 |
| 7 | 引用消息 HTML 转义 |
| 8 | `<refermsg>` 内含 `<title>`，先剔除再取主标题 |
| 9 | WAL 不用单独解密（实测主库已最新），但顺序是：**先取密钥 → 关微信 → 后解密** |
| 10 | 手机记录需「迁移」（非备份）到电脑才有 |
| 11 | **page1 前 16 字节被 salt 占，别拿 `SQLite format 3` 当判据** |
| 12 | keyspec 是 XOR 混淆的，可用已知明文（salt hex）反推 pad |
| 13 | 名字在 remark/nick 里搜不到时，用解密数据 grep 名字反查 wxid |
| 14 | 解密范围要取舍：跳过 `*_fts.db`（可达 1.3GB）及辅助库 |

## FAQ

**会封号吗？** 只读内存与文件副本，不注入不联网，微信无感知。风险自担。

**能读别人的记录吗？** 不能。见顶部边界说明。

**Mac / iOS？** 本仓库只覆盖 Windows。**macOS 请走 [GreenBubbles](https://github.com/bojieli/greenbubbles)**
（Apple 芯片 + macOS 14+ + 微信 4.1+，会重新签名微信），见 [`docs/macos.md`](docs/macos.md)。
iOS / Android 没有可行方案。

## 致谢

- 密钥提取基于 [wcdb-key-tool](https://octohz.com/p/2259)（只读扫描 Config.Cipher）的思路实现
- 灵感与对照：`328336690/wechat-decrypt`（4.0 时代方案）、`tarry9110/wechat-chat-export-decrypt`（Frida 方案）

## License

MIT
