# tools/lite — 精简取密钥 / 解密参考实现

`wcdb_key_lite.py` 是 [`tools/vendor/wcdb_key_tool_windows.py`](../vendor/wcdb_key_tool_windows.py)
的**精简替代**：约 250 行纯 Python，只依赖 `pycryptodome`，把"内存里取密钥 → 解密数据库"
这件事用一个文件讲清楚。

## 和 vendor 版有什么不同

| | `tools/vendor`（上游） | `tools/lite`（本目录） |
|---|---|---|
| 定位方式 | 先找 `com.Tencent.WCDB.Config.Cipher` 字符串 → 找引用它的对象节点 → 按对象结构取字段 | **不做对象定位**，直接全内存扫 XOR 混淆后的 keyspec |
| 解混淆 | 用一个**版本相关的固定掩码常量** | 用**已知明文反推 pad**：keyspec 尾部就是本库 salt 的 ASCII hex |
| 版本升级 | 掩码常量若变，需要跟着改 | 只要 keyspec 还带 salt 尾就还能用 |
| 代码量 | ~1050 行（含 Linux/macOS/断点等） | ~250 行，只覆盖 Windows |
| 依赖 | 无第三方库（走系统 CNG） | `pycryptodome` |

两者最终都做同一件事：拿到 32 字节 raw key → **HMAC-SHA512 校验** → AES-256-CBC 逐页解密。
校验判据见 [`docs/pitfalls.md`](../../docs/pitfalls.md) 坑 11（**不是**看 `SQLite format 3` 魔数）。

## 用法

```bash
pip install pycryptodome

# 1) 取密钥（管理员命令行；微信需登录运行中）
python tools/lite/wcdb_key_lite.py extract \
    --db-dir "C:/Users/<你>/xwechat_files/<wxid>_<后缀>/db_storage"
#    成功标志：matched N/N salts   → all_keys.json

# 2) 解密
python tools/lite/wcdb_key_lite.py decrypt \
    --db-dir "C:/Users/<你>/xwechat_files/<wxid>_<后缀>/db_storage" \
    --keys all_keys.json --output data/decrypted
#    成功标志：结果: N 成功, 0 失败
```

`decrypt` 的进度行与 vendor 版一致，因此可以直接替换进归档引擎：

```bash
# Windows (Git Bash / PowerShell 下按需改写)
KEY_TOOL_PY=python \
TOOL_PY=tools/lite/wcdb_key_lite.py \
python src/sync_pm.py
```

## 注意

- 需要**管理员权限**（读别的进程内存）+ **微信登录运行中**。
- 密钥只对**本机本次登录**有效，退出重登后要重取。
- 只用于读取**本人设备、本人账号**的本地数据。
