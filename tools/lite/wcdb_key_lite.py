#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""wcdb_key_lite.py — 微信 4.1.x 数据库密钥提取 + 解密（精简参考实现）

`tools/vendor/wcdb_key_tool_windows.py` 的精简替代：约 250 行纯 Python，
只依赖 `pycryptodome`，用「已知明文反推 XOR pad」取密钥——
不依赖运行时对象结构，也不依赖任何版本相关的固定掩码常量。

原理
----
1) 微信 4.1 把整串 keyspec

       x'<64位 enc_key 的 hex><32位 salt 的 hex>'      （共 99 字节）

   用一个 32 字节 pad 循环 XOR（``keyspec[j] ^= pad[j % 32]``）混淆后放进程内存。

2) keyspec **尾部 32 字节的明文 = 该库 salt 的 ASCII hex**（已知），据此反推 pad：

       temp = ct[66:98] ^ salt_hex          # = pad[2:32] ++ pad[0:2]
       pad  = temp[30:32] + temp[0:30]      # 旋转到正确相位
       自校验 ct[0] ^ pad[0] == ord('x')
       pt[2:66] 即 32 字节 raw key

3) 校验（关键，见 docs/pitfalls.md 坑 11）：page1 前 16 字节被 salt 占据，
   解密后**看不到** ``SQLite format 3`` 魔数，所以改判据：

       · 解密 page1 后 pt[0:2] == \\x10\\x00（page_size = 4096）
         且 pt[5:8] == \\x40\\x20\\x20（SQLite 文件头余段固定值）
       · HMAC-SHA512：mac_key = PBKDF2-SHA512(raw_key, salt ^ 0x3A, 2, 32)

   若 raw key 直接验不过，再回退试 passphrase 路线（PBKDF2 256000）。

4) 解密：AES-256-CBC 逐页；page1 前 16 字节补回 ``SQLite format 3\\0``。

用法
----
    # 取密钥（需管理员权限，且微信登录运行中）
    python wcdb_key_lite.py extract --db-dir "<...>/xwechat_files/<wxid>_<后缀>/db_storage"

    # 解密（产物可直接被 src/sync.py / src/sync_pm.py 读取）
    python wcdb_key_lite.py decrypt --db-dir "<...>/db_storage" \
        --keys all_keys.json --output data/decrypted

与 vendor 工具的兼容性
--------------------
``decrypt`` 的进度行格式（``结果: N 成功, M 失败``）与 vendor 版一致，
可直接用环境变量切进来：

    KEY_TOOL_PY=<python> TOOL_PY=<本脚本路径> python src/sync_pm.py

注意：产出的 ``all_keys.json`` 只对**本机本次登录**有效，不要外传。
本脚本只用于读取**本人设备、本人账号**的本地数据。
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import hmac as hmac_mod
import json
import os
import struct
import subprocess
import sys

from Crypto.Cipher import AES

# ---- 页 / keyspec 常量 ----
PAGE_SZ = 4096
RESERVE_SZ = 80                       # IV(16) + HMAC(64)
IV_OFF = PAGE_SZ - RESERVE_SZ         # 4016
SALT_SZ = 16
KEY_HEX_LEN = 64
SALT_HEX_LEN = 32
SALT_OFF = 2 + KEY_HEX_LEN            # 66
KEYSPEC_LEN = 2 + KEY_HEX_LEN + SALT_HEX_LEN + 1   # 99
SQLITE_HDR = b"SQLite format 3\x00"

VALID_PAGE_SIZES = (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536)

# ---- Windows 内存读取 ----
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_GUARD = 0x100
READABLE = (0x02, 0x04, 0x20, 0x40)
MAX_USER_ADDR = 0x7FFFFFFF0000

_k32 = ctypes.windll.kernel32 if os.name == "nt" else None
if _k32 is not None:
    _k32.OpenProcess.restype = ctypes.c_void_p
    _k32.VirtualQueryEx.restype = ctypes.c_size_t


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


def _find_pids(image="Weixin.exe"):
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="gbk", errors="ignore",
    ).stdout
    pids = []
    for line in out.strip().splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 5:
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def _iter_regions(h):
    mbi = _MBI()
    addr = 0
    while addr < MAX_USER_ADDR:
        if not _k32.VirtualQueryEx(ctypes.c_void_p(h), ctypes.c_void_p(addr),
                                   ctypes.byref(mbi), ctypes.sizeof(mbi)):
            break
        size = mbi.RegionSize or 0
        if size == 0:
            break
        if mbi.State == MEM_COMMIT and (mbi.Protect & 0xFF) in READABLE \
                and not (mbi.Protect & PAGE_GUARD):
            yield int(mbi.BaseAddress), size
        addr += size


# ---- 密钥校验与解密 ----

def page1_ok(enc_key, db_path):
    """判据：页头字段 + HMAC-SHA512（不要用 SQLite 魔数，见坑 11）。"""
    try:
        with open(db_path, "rb") as f:
            data = f.read(PAGE_SZ)
    except OSError:
        return False
    if len(data) < PAGE_SZ:
        return False
    iv = data[IV_OFF:IV_OFF + 16]
    ct = data[SALT_SZ:IV_OFF]
    try:
        pt = AES.new(enc_key, AES.MODE_CBC, iv).decrypt(ct)
    except Exception:
        return False
    if int.from_bytes(pt[0:2], "big") not in VALID_PAGE_SIZES or pt[5:8] != b"\x40\x20\x20":
        return False
    mac_salt = bytes(b ^ 0x3A for b in data[:SALT_SZ])
    mac_key = hashlib.pbkdf2_hmac("sha512", enc_key, mac_salt, 2, dklen=32)
    hm = hmac_mod.new(mac_key, data[SALT_SZ:PAGE_SZ - RESERVE_SZ + SALT_SZ], hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == data[PAGE_SZ - 64:PAGE_SZ]


def decrypt_file(src, dst, key):
    with open(src, "rb") as f:
        data = f.read()
    n_pages = len(data) // PAGE_SZ
    out = bytearray()
    for i in range(n_pages):
        page = data[i * PAGE_SZ:(i + 1) * PAGE_SZ]
        iv = page[IV_OFF:IV_OFF + 16]
        if i == 0:
            pt = AES.new(key, AES.MODE_CBC, iv).decrypt(page[SALT_SZ:IV_OFF])
            out += SQLITE_HDR + pt + b"\x00" * RESERVE_SZ
        else:
            pt = AES.new(key, AES.MODE_CBC, iv).decrypt(page[:IV_OFF])
            out += pt + b"\x00" * RESERVE_SZ
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "wb") as f:
        f.write(out)
    return len(out)


# ---- keyspec 扫描 ----

def keyspec_from(ct, salt_hex):
    """从 99 字节 XOR 混淆的 keyspec 密文里解出 32 字节 raw key。"""
    if len(ct) < KEYSPEC_LEN:
        return None
    temp = bytes(a ^ b for a, b in zip(ct[SALT_OFF:SALT_OFF + SALT_HEX_LEN], salt_hex))
    pad = temp[SALT_HEX_LEN - 2:] + temp[:SALT_HEX_LEN - 2]
    if ct[0] ^ pad[0] != 0x78 or ct[1] ^ pad[1] != 0x27:      # "x'"
        return None
    pt = bytes(ct[j] ^ pad[j % 32] for j in range(KEYSPEC_LEN))
    if pt[:2] != b"x'" or pt[SALT_OFF:SALT_OFF + SALT_HEX_LEN] != salt_hex or pt[-1] != 0x27:
        return None
    keyhex = pt[2:2 + KEY_HEX_LEN]
    if any(c not in b"0123456789abcdefABCDEF" for c in keyhex):
        return None
    try:
        return bytes.fromhex(keyhex.decode())
    except ValueError:
        return None


def scan_window(data, salt_table):
    """单块内存窗口里找所有库的 keyspec。

    用大整数异或一次算出 diff[j] = data[j] ^ data[j+96]，
    再在 diff 上用 2 字节特征定位候选（比逐位置试快几个数量级）。
    """
    n = len(data)
    if n < KEYSPEC_LEN + 128:
        return []
    m = n - 96
    diff = (int.from_bytes(data[:m], "big") ^ int.from_bytes(data[96:96 + m], "big")).to_bytes(m, "big")
    hits = []
    for rel, salt_hex in salt_table:
        pat = bytes([0x78 ^ salt_hex[30], 0x27 ^ salt_hex[31]])
        start = 0
        while True:
            j = diff.find(pat, start)
            if j < 0:
                break
            start = j + 1
            if j + KEYSPEC_LEN <= n:
                rk = keyspec_from(data[j:j + KEYSPEC_LEN], salt_hex)
                if rk:
                    hits.append((rel, rk))
    return hits


def _db_salts(db_dir):
    out = []
    for root, _, files in os.walk(db_dir):
        for fn in files:
            if not fn.endswith(".db"):
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, db_dir).replace("\\", "/")
            with open(p, "rb") as f:
                salt = f.read(16)
            out.append((rel, salt.hex().encode()))
    return out


def cmd_extract(a):
    if os.name != "nt":
        raise SystemExit("本精简版只覆盖 Windows；其他平台请用 tools/vendor 下的版本")
    db_dir = a.db_dir
    pids = _find_pids("Weixin.exe")
    if not pids:
        raise SystemExit("[!] 未找到运行中的 Weixin.exe（请先登录微信）")
    print(f"[*] Weixin.exe pids = {pids}")

    salts = _db_salts(db_dir)
    print(f"[*] 数据库 {len(salts)} 个")

    CHUNK = 48 * 1024 * 1024
    OVERLAP = 256
    buf = ctypes.create_string_buffer(CHUNK)
    cand = {}
    total = 0
    for pid in pids:
        h = _k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            print(f"  pid {pid}: OpenProcess 失败 err={_k32.GetLastError()}（需要管理员权限）")
            continue
        got_bytes = 0
        for base, size in _iter_regions(h):
            off = 0
            while off < size:
                n = min(CHUNK, size - off)
                got = ctypes.c_size_t(0)
                ok = _k32.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(base + off),
                                            buf, n, ctypes.byref(got))
                if ok and got.value:
                    data = buf.raw[:got.value]
                    total += got.value
                    got_bytes += got.value
                    for rel, rk in scan_window(data, salts):
                        cand.setdefault(rel, rk)
                if n < CHUNK:
                    break
                off += n - OVERLAP
        _k32.CloseHandle(ctypes.c_void_p(h))
        print(f"  pid {pid}: 读取 {got_bytes / 1048576:.1f} MB，候选累计 {len(cand)}")

    print(f"[*] 总计读取 {total / 1048576:.1f} MB")

    out_path = a.output or os.path.join(os.getcwd(), "all_keys.json")
    cand_path = os.path.join(os.path.dirname(os.path.abspath(out_path)), "candidates.json")
    with open(cand_path, "w", encoding="utf-8") as f:
        json.dump({k: v.hex() for k, v in cand.items()}, f, ensure_ascii=False, indent=2)

    found = {}
    for rel, raw in cand.items():
        db = os.path.join(db_dir, rel.replace("/", os.sep))
        route = "raw"
        ok = page1_ok(raw, db)
        if not ok:
            salt = open(db, "rb").read(16) if os.path.exists(db) else b""
            k2 = hashlib.pbkdf2_hmac("sha512", raw, salt, 256000, dklen=32)
            if page1_ok(k2, db):
                raw, ok, route = k2, True, "PBKDF2(256000)"
        if a.verbose or not ok:
            print(f"  [{'OK' if ok else '--'}] {rel}  ({route})")
        if ok:
            found[rel] = raw.hex()

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(found, f, ensure_ascii=False, indent=2)
    print(f"[+] matched {len(found)}/{len(salts)} salts -> {out_path}")
    if len(found) < len(salts):
        print("[!] 部分库未命中：确认微信正在登录运行、且以管理员身份执行")
    return 0


def cmd_decrypt(a):
    with open(a.keys, encoding="utf-8") as f:
        keys = json.load(f)
    ok = bad = 0
    for rel, key_hex in keys.items():
        src = os.path.join(a.db_dir, rel.replace("/", os.sep))
        if not os.path.exists(src):
            bad += 1
            continue
        dst = os.path.join(a.output, rel.replace("/", os.sep))
        decrypt_file(src, dst, bytes.fromhex(key_hex))
        ok += 1
    print(f"结果: {ok} 成功, {bad} 失败")
    print(f"输出目录: {a.output}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="微信 4.1.x 数据库密钥提取 + 解密（精简参考实现）")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印每个库的结果")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="从运行中的微信进程内存提取密钥")
    pe.add_argument("--db-dir", required=True, help="<...>/xwechat_files/<wxid>_<后缀>/db_storage")
    pe.add_argument("-o", "--output", help="all_keys.json 输出路径（默认当前目录）")
    pe.set_defaults(func=cmd_extract)

    pd = sub.add_parser("decrypt", help="用 all_keys.json 解密数据库副本")
    pd.add_argument("--db-dir", required=True)
    pd.add_argument("--keys", required=True, help="all_keys.json 路径")
    pd.add_argument("--output", required=True, help="明文输出目录")
    pd.set_defaults(func=cmd_decrypt)

    a = ap.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
