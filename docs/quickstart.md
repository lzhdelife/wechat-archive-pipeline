# 快速开始

## 前置条件

- Windows 10/11，**管理员权限**
- 微信 Windows 版 **4.1.x**，且**处于登录运行状态**
- Python 3.10+

## 1. 安装依赖

```bash
pip install pycryptodome psutil zstandard
```

## 2. 找到微信数据目录

数据目录格式：

```
C:\Users\<用户名>\xwechat_files\<wxid>_<后缀>\db_storage
```

在资源管理器地址栏输入 `%USERPROFILE%\xwechat_files` 回车，
**选 `session\session.db` 修改时间最新的那个目录**（那是当前登录的账号）。

## 3. 提取密钥

```bash
# 管理员命令行执行
python tools/vendor/wcdb_key_tool_windows.py -v extract \
    --db-dir "C:/Users/<用户名>/xwechat_files/<wxid>_<后缀>/db_storage"
```

成功标志：`Windows 4.1 runtime Config.Cipher scan matched N/N salts`

产物：`tools/vendor/all_keys.json`

> ⚠️ `-v` 必须放在 `extract` 前面。
> ⚠️ 此文件只对**本机本次登录**有效，不要分享给他人。

**精简替代**（~250 行、不依赖版本相关掩码常量，用已知明文反推 XOR pad）：

```bash
python tools/lite/wcdb_key_lite.py extract \
    --db-dir "C:/Users/<用户名>/xwechat_files/<wxid>_<后缀>/db_storage"
# 成功标志：matched N/N salts
```

## 4. 配置

### 群聊 → `src/config.json`

```json
{
  "chatroom": "1234567890@chatroom",
  "chat_name": "我的群",
  "owner_wxid": "wxid_OWNER",
  "track_mode": "all",
  "self_wxids": ["wxid_SELF"],
  "db_storage": "C:/Users/<用户名>/xwechat_files/<wxid>_<后缀>/db_storage"
}
```

**群 ID 怎么查**：解密后读 `session.db`：

```bash
python -c "
import sqlite3
c=sqlite3.connect('file:data/decrypted/session/session.db?mode=ro',uri=True)
for u,s in c.execute(\"select username,summary from SessionTable where username like '%@chatroom'\"):
    print(u,'|',str(s)[:40])
"
```

### 私聊 → `src/config_pm.json`

```json
{
  "chat_name": "与某人",
  "peer_wxid": "wxid_PEER",
  "db_storage": "C:/Users/<用户名>/xwechat_files/<wxid>_<后缀>/db_storage"
}
```

**对方 wxid 怎么查**：解密后列私聊会话：

```bash
python src/sync_pm.py --list-sessions
```

> 如果对方没存备注、昵称也不是真名，`--list-sessions` 也认不出来——
> 改用「按内容反查」：在 `data/decrypted/` 里 `grep` 他的名字，
> 命中所在会话表名即 `Msg_<md5(对方wxid)>`。详见 `docs/pitfalls.md` 坑 13。

## 5. 运行

```bash
# 群聊
python src/sync.py

# 私聊（建议先自检）
python src/sync_pm.py --inspect
python src/sync_pm.py

# 可选：由 jsonl 生成单文件 HTML 阅读版（可搜索、可只看某人）
python src/gen_html.py --jsonl "data/archive/与某人.jsonl" --peer-name "对方"
```

产物在 `data/archive/`：`.jsonl`（数据）/ `.md`（阅读）/ `.csv`（表格）/ `.html`（可选，网页版）。

## 6. 增量更新

重复运行不会产生重复（指纹去重）。
想让归档反映最新消息，定期重跑即可；取密钥在有效期内可复用。

---

## 常见错误

| 报错 / 现象 | 原因 | 解决 |
|---|---|---|
| `缺少依赖 zstandard` | 没装依赖 | `pip install zstandard pycryptodome psutil` |
| 密钥提取 0/N 命中 | 微信没运行 / 没管理员权限 / 版本不匹配 | 逐项确认；或换 `tools/lite` 试 |
| `缺少密钥文件 all_keys.json` | 没先提取密钥 | 执行步骤 3 |
| 导出内容乱码 | 缺 zstandard | 装依赖后重跑 |
| **密钥"验不过"（看不到 SQLite 魔数）** | 判据用错了 | 看页头字段 + HMAC，不看魔数，见坑 11 |
| 发送者人名全错 | 分片映射共用 | 见 `docs/pitfalls.md` 第 4 条 |
| 抽不到消息 | ID 填错 / 记录不在本机 | 用 `--list-sessions` 或读 session.db 复查；名字搜不到见坑 13 |

## 环境变量（可选）

| 变量 | 说明 |
|---|---|
| `KEY_TOOL_DIR` | 密钥工具目录（默认 `tools/vendor`）|
| `ALL_KEYS` | `all_keys.json` 路径 |
| `WXPIPE_DATA` | 数据输出目录（默认 `data/`）|
| `KEY_TOOL_PY` | 调密钥工具用的解释器（默认当前解释器）|
| `TOOL_PY` | 密钥/解密脚本路径（默认 `tools/vendor/wcdb_key_tool_windows.py`，可指到 `tools/lite/wcdb_key_lite.py`）|
