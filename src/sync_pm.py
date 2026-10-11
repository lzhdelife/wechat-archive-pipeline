#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""私聊归档引擎（WeChat 4.x / SQLCipher 4）

与 sync.py（群聊版）的区别：
  - 目标是一对一私聊，不是群（无 @chatroom）
  - 发言归属靠 Name2Id.is_session 判定（0=我 / 1=对方），不是白名单
  - 单会话只有两个人，渲染更简单

用法：
    python sync_pm.py                  # 增量归档（首次自动全量）
    python sync_pm.py --reset          # 清空重建
    python sync_pm.py --inspect        # 只看会话概况，不落盘
    python sync_pm.py --list-sessions  # 列出本机所有私聊会话

配置：读取同目录 config_pm.json
"""

import argparse
import csv
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
TOOL_DIR = os.environ.get("KEY_TOOL_DIR", os.path.join(TOOLS, "vendor"))
TOOL_PY = os.path.join(TOOL_DIR, "wcdb_key_tool_windows.py")
KEYS = os.environ.get("ALL_KEYS", os.path.join(TOOL_DIR, "all_keys.json"))
DATA = os.environ.get("WXPIPE_DATA", os.path.join(TOOLS, "data"))
DEC = os.path.join(DATA, "decrypted")
ARCHIVE = os.path.join(DATA, "archive")
# 用哪个 Python 调密钥工具：默认当前解释器（需已装 pycryptodome / psutil）
VENV_PY = os.environ.get("KEY_TOOL_PYTHON", sys.executable)

try:
    import zstandard as zstd
    _Z = zstd.ZstdDecompressor()
except Exception:
    _Z = None

# 微信 4.x 的消息内容常为 zstd 压缩；缺依赖会静默产出乱码，必须显式拦截
if _Z is None:
    print("[!] 缺少依赖 zstandard —— 消息内容无法解压（会变成乱码）\n"
          "    请先安装：pip install zstandard pycryptodome psutil", flush=True)
    raise SystemExit(1)

LOW32 = 0xFFFFFFFF

TYPE_HINT = {
    1: "", 3: "图片", 34: "语音", 43: "视频", 47: "表情", 48: "位置",
    49: "链接/文件", 50: "通话", 10000: "系统", 10002: "系统",
}

APPMSG_LABEL = {
    5: "链接", 6: "文件", 8: "表情", 19: "聊天记录", 24: "合并转发",
    33: "小程序", 36: "小程序", 44: "视频号", 49: "引用", 51: "视频号",
    57: "引用", 62: "视频号", 87: "群公告", 2000: "转账", 2001: "红包",
}


def log(*a):
    print(*a, flush=True)


def md5hex(s):
    return hashlib.md5(s.encode()).hexdigest()


def dec_bytes(x, ctf=None):
    if x is None:
        return ""
    if isinstance(x, bytes):
        if ctf == 4 and _Z is not None:
            try:
                return _Z.decompress(x).decode("utf-8", "ignore")
            except Exception:
                return ""
        if x[:4] == b"\x28\xb5\x2f\xfd" and _Z is not None:
            try:
                return _Z.decompress(x).decode("utf-8", "ignore")
            except Exception:
                pass
        return x.decode("utf-8", "ignore")
    return str(x)


def xml_field(txt, tag):
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", txt, re.S)
    return (m.group(1).strip() if m else "")


def render_body(local_type, raw):
    """把原始内容渲染成给人看的一行。"""
    lt = local_type & LOW32
    raw = raw or ""
    body = raw
    # 私聊内容一般不带 "wxid:\n" 前缀，但兼容一下
    if lt not in (10000, 10002):
        m = re.match(r"^([A-Za-z0-9_\-]{5,}):\n?(.*)$", raw, re.S)
        if m and "wxid_" in m.group(1):
            body = m.group(2)
    body = re.sub(r"\n{2,}", "\n", body.strip())

    if lt == 1:
        return body
    if lt == 3:
        return "[图片]"
    if lt == 34:
        sec = re.search(r'voicelength="(\d+)"', body)
        return f"[语音 {round(int(sec.group(1)) / 1000)}秒]" if sec else "[语音]"
    if lt == 43:
        return "[视频]"
    if lt == 47:
        return "[表情]"
    if lt == 48:
        return "[位置]"
    if lt == 49:
        at = xml_field(body, "type")
        label = APPMSG_LABEL.get(int(at) if at.isdigit() else 0, "链接/文件")
        # 取主标题前先剔除 refermsg，否则会被引用短标题顶掉
        main_src = re.sub(r"<refermsg>.*?</refermsg>", "", body, flags=re.S)
        main = xml_field(main_src, "title") or xml_field(main_src, "des")
        ref = ""
        if "<refermsg>" in body:
            rc = xml_field(body, "content")
            rn = xml_field(body, "displayname") or xml_field(body, "fromusr")
            if rc:
                rc = html.unescape(rc)
                if "<" in rc:
                    rc = xml_field(rc, "title") or xml_field(rc, "des") or \
                         re.sub(r"<[^>]+>", "", rc).strip()
                rc = re.sub(r"\s+", " ", rc).strip()
                if rc:
                    ref = f"（↩ {rn[:12]}：{rc[:60]}）"
        if label == "文件" and not main:
            main = xml_field(body, "title")
        return (f"[{label}] {main[:200]}{ref}" if main else f"[{label}]{ref}")
    if lt in (10000, 10002):
        tmpl = xml_field(body, "template")
        if tmpl:
            return "[系统] " + re.sub(r"\$[a-z_]+\$", "…", tmpl)
        return "[系统]"
    if body and not body.startswith("<"):
        return body
    return f"[{TYPE_HINT.get(lt, '未知')}]"


# -------- 各类小工具 --------

def load_cfg():
    p = os.path.join(HERE, "config_pm.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_contacts():
    """从 contact.db 读 wxid -> 显示名。"""
    out = {}
    p = os.path.join(DEC, "contact", "contact.db")
    if not os.path.exists(p):
        return out
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        for u, rk, nk in con.execute(
                "select username, remark, nick_name from contact"):
            out[u] = (rk or "").strip() or (nk or "").strip() or u
    except Exception:
        pass
    con.close()
    return out


def _workdir_shards():
    d = os.path.join(DEC, "message")
    if not os.path.isdir(d):
        return []
    return [os.path.join(d, p) for p in sorted(os.listdir(d))
            if p.startswith("message_") and p.endswith(".db")]


def decrypt_snapshot(db_storage):
    if not os.path.exists(KEYS):
        log("[!] 缺少密钥文件 all_keys.json，先跑 extract（见交接说明 §3.4）")
        sys.exit(1)
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(DEC, exist_ok=True)
    r = subprocess.run(
        [VENV_PY, TOOL_PY, "decrypt", "--db-dir", db_storage,
         "--keys", KEYS, "--output", DEC],
        capture_output=True, text=True, cwd=TOOL_DIR,
    )
    m = re.search(r"结果: (\d+) 成功, (\d+) 失败", r.stdout or "")
    if not m:
        log("[!] 解密失败：", (r.stdout or "")[-400:])
        sys.exit(1)
    return int(m.group(1)), int(m.group(2))


# -------- 会话发现 --------

def list_sessions(cfg):
    """列出本机所有私聊会话（用于找目标对象的 wxid）。"""
    p = os.path.join(DEC, "session", "session.db")
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    rows = con.execute(
        "select username, summary, last_timestamp from SessionTable "
        "where last_timestamp>0 order by last_timestamp desc").fetchall()
    con.close()
    contacts = load_contacts()
    out = []
    for u, s, t in rows:
        if u.endswith("@chatroom"):
            continue
        if u in ("filehelper", "notifymessage", "weixin"):
            continue
        if u.startswith("gh_"):
            continue
        out.append({
            "wxid": u,
            "name": contacts.get(u, u),
            "last": dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M"),
            "summary": (s or "")[:40],
        })
    return out


def collect(cfg, include_system=False):
    """跨分片收集私聊消息，按指纹去重。

    私聊的发言归属：Name2Id.is_session == 0 → 我（本人），== 1 → 对方。
    """
    peer = cfg["peer_wxid"]
    table = "Msg_" + md5hex(peer)
    merged = {}
    shard_info = []

    for path in _workdir_shards():
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except Exception:
            continue
        if not con.execute("select 1 from sqlite_master where name=?",
                           (table,)).fetchone():
            con.close()
            continue
        # 关键：每个分片用自己的 Name2Id（跨分片 rowid 会冲突）
        smap = {r: (u, iss) for r, u, iss in
                con.execute("select rowid, user_name, is_session from Name2Id")}
        rows = con.execute(
            f"select local_type, create_time, real_sender_id, "
            f"message_content, compress_content, WCDB_CT_message_content "
            f"from {table}").fetchall()
        shard_info.append((os.path.basename(path), len(rows)))
        for lt, ct, rs, mc, cc, ctf in rows:
            user_name, is_session = smap.get(rs, ("", 0))
            if (lt & LOW32) in (10000, 10002) and not include_system:
                continue
            raw = dec_bytes(mc if mc is not None else cc, ctf)
            if not raw.strip():
                continue
            is_self = (is_session == 0)
            fp = hashlib.sha1(
                f"{ct}|{user_name}|{lt}|{raw}".encode("utf-8", "ignore")
            ).hexdigest()[:16]
            if fp in merged:
                continue
            merged[fp] = {
                "fp": fp,
                "ts": int(ct or 0),
                "time": dt.datetime.fromtimestamp(ct or 0).strftime("%Y-%m-%d %H:%M"),
                "date": dt.datetime.fromtimestamp(ct or 0).strftime("%Y-%m-%d"),
                "self": is_self,
                "wxid": user_name,
                "type": lt & LOW32,
                "raw": raw,
            }
        con.close()
    return sorted(merged.values(), key=lambda m: (m["ts"], m["fp"])), shard_info


# -------- 输出 --------

def paths(cfg):
    base = os.path.join(ARCHIVE, cfg["chat_name"])
    return base + ".jsonl", base + ".md", base + ".csv"


def load_jsonl(p):
    out = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        pass
    return out


def render_md(msgs, cfg):
    peer_label = cfg.get("peer_label") or cfg["peer_wxid"]
    self_label = cfg.get("self_label") or "我"
    lines = [f"# {cfg['chat_name']} · 聊天记录", ""]
    cur = None
    for m in msgs:
        if m["date"] != cur:
            if cur is not None:
                lines.append("")
            lines.append(f"## {m['date']}")
            lines.append("")
            cur = m["date"]
        who = self_label if m.get("self") else peer_label
        body = render_body(m.get("type", 1), m.get("raw", ""))
        body = body.replace("\n", " / ")
        lines.append(f"- **{m['time'][11:]} {who}**：{body}")
    return "\n".join(lines) + "\n"


def export_csv(msgs, p, cfg):
    peer_label = cfg.get("peer_label") or cfg["peer_wxid"]
    self_label = cfg.get("self_label") or "我"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "发送者", "是否本人", "类型", "内容"])
        for m in msgs:
            who = self_label if m.get("self") else peer_label
            w.writerow([m["time"], who, "是" if m.get("self") else "否",
                        TYPE_HINT.get(m.get("type"), m.get("type")),
                        render_body(m.get("type", 1), m.get("raw", ""))])


def do_sync(cfg, reset=False, dry=False, no_decrypt=False):
    if not no_decrypt:
        ok, bad = decrypt_snapshot(cfg["db_storage"])
        log(f"[解压] 解密 {ok} 个库，失败 {bad}")
    msgs, shard_info = collect(cfg, cfg.get("include_system", False))
    if not msgs:
        log("[!] 没抽到消息——检查 peer_wxid 是否正确、聊天记录是否在本机")
        return
    log(f"[抽取] {len(msgs)} 条  |  分片: {shard_info}")
    span = (dt.datetime.fromtimestamp(msgs[0]['ts']).strftime('%Y-%m-%d'),
            dt.datetime.fromtimestamp(msgs[-1]['ts']).strftime('%Y-%m-%d'))
    n_self = sum(1 for m in msgs if m.get("self"))
    log(f"[范围] {span[0]} ~ {span[1]}  |  我 {n_self} 条 / 对方 {len(msgs)-n_self} 条")
    if dry:
        log("[dry-run] 不落盘")
        return
    jp, mp, cp = paths(cfg)
    os.makedirs(ARCHIVE, exist_ok=True)
    old = {} if reset else {m["fp"]: m for m in load_jsonl(jp)}
    for m in msgs:
        old[m["fp"]] = m
    allmsgs = sorted(old.values(), key=lambda m: (m["ts"], m.get("fp", "")))
    with open(jp, "w", encoding="utf-8") as f:
        for m in allmsgs:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_md(allmsgs, cfg))
    export_csv(allmsgs, cp, cfg)
    log(f"[落盘] {len(allmsgs)} 条 → {jp}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空重建")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-decrypt", action="store_true")
    ap.add_argument("--inspect", action="store_true", help="只看概况")
    ap.add_argument("--list-sessions", action="store_true", help="列出私聊会话")
    a = ap.parse_args()
    cfg = load_cfg()
    if a.list_sessions:
        for s in list_sessions(cfg):
            print(f"{s['last']}  {s['name'][:20]:<22} {s['wxid']:<28} {s['summary']}")
        return
    if a.inspect:
        msgs, shards = collect(cfg, cfg.get("include_system", False))
        log(f"分片: {shards}")
        log(f"共 {len(msgs)} 条")
        if msgs:
            log(f"范围: {dt.datetime.fromtimestamp(msgs[0]['ts'])}"
                f" ~ {dt.datetime.fromtimestamp(msgs[-1]['ts'])}")
            for m in msgs[-15:]:
                who = "我" if m.get("self") else "对方"
                log(f"  [{m['time']}] {who}: "
                    f"{render_body(m['type'], m['raw'])[:60]!r}")
        return
    do_sync(cfg, reset=a.reset, dry=a.dry_run, no_decrypt=a.no_decrypt)


if __name__ == "__main__":
    main()
