# wechat-archive-pipeline

> 只读导出**你自己**的微信（Windows 4.1.x）本地聊天记录，输出 jsonl / md / csv。
> 不注入、不 Hook、不修改微信。适用于个人归档与让 AI 助手了解自己。

[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## ⚠️ 边界（请先读）

- 只支持读取**本人账号、本人设备、本地文件**。
- 密钥只存在于**当前登录者**的进程内存里——别人的电脑上这套方法无法工作（这是微信的加密设计）。
- 请遵守当地法律法规与平台服务条款，仅供个人数据管理使用。

## 工作原理

```
微信在运行（登录在线）
   ↓  只读扫描进程内存中的 Config.Cipher 运行时对象（XOR 混淆）
取出本次登录的数据库密钥
   ↓  PBKDF2-HMAC-SHA512(passphrase, salt, 256000, 32)
解密数据库【副本】（原文件不动）
   ↓  跨 5 个 message_N.db 分片合并 + 指纹去重
导出 jsonl / md / csv
```

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
python tools/key_tool/wcdb_key_tool_windows.py -v extract \
    --db-dir "C:/Users/<你>/xwechat_files/<你的wxid>_<后缀>/db_storage"
```

成功标志：`matched N/N salts`。产物 `all_keys.json`。

> ⚠️ `-v` 必须放在 `extract` 前面。密钥每次登录可能变化，失效就重取。

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
```

输出到 `data/archive/`。

## 目录结构

```
├── src/
│   ├── sync.py            群聊归档引擎
│   ├── sync_pm.py         私聊归档引擎
│   ├── config.json        群聊配置
│   └── config_pm.json     私聊配置
├── tools/key_tool/        密钥提取（见下方致谢）
└── docs/pitfalls.md       10 个踩坑清单
```

## 已知坑（完整版见 docs/pitfalls.md）

| # | 坑 |
|---|---|
| 1 | 4.1+ 内存中无明文密钥，旧工具全部失效 |
| 2 | 密钥 XOR 混淆，需以运行时对象视角读取 |
| 3 | 同一消息表跨 5 个分片，需合并 + 指纹去重 |
| 4 | **各分片 Name2Id rowid 独立，必须按分片解析发送者** |
| 5 | `local_type` 高 32 位是子类型 |
| 6 | 内容可能 zstd 压缩 |
| 7 | 引用消息 HTML 转义 |
| 8 | `<refermsg>` 内含 `<title>`，先剔除再取主标题 |
| 9 | 不需要解密 WAL |
| 10 | 手机记录需「迁移」（非备份）到电脑才有 |

## FAQ

**会封号吗？** 只读内存与文件副本，不注入不联网，微信无感知。风险自担。

**能读别人的记录吗？** 不能。见顶部边界说明。

**Mac / iOS？** 思路相同，密钥提取方式各平台不同，本仓库只覆盖 Windows。

## 致谢

- 密钥提取基于 [wcdb-key-tool](https://octohz.com/p/2259)（只读扫描 Config.Cipher）的思路实现
- 灵感与对照：`328336690/wechat-decrypt`（4.0 时代方案）、`tarry9110/wechat-chat-export-decrypt`（Frida 方案）

## License

MIT
