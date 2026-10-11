#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync.py — 微信群聊归档流水线（全程只读）

流程：
  1. 用已提取的密钥，把微信加密数据库解密成一份快照
  2. 从快照里抽取指定群的消息（全群 / 白名单两种模式）
  3. 增量落到可读归档：jsonl 为主数据，markdown 为阅读视图，csv 供分析

设计要点：
  - 只读：只调 wcdb-key-tool 的解密（ReadProcessMemory + 本地解密），不注入、不改微信
  - 幂等：以消息指纹去重，重复跑不会产生重复条目
  - 主库即当前：实测解密快照能读到"当前这一秒"的消息，无需额外处理 WAL
  - 分片独立映射：每个 message_N.db 有自己独立的 Name2Id（rowid 跨分片冲突率 99%），
    因此必须"用本分片自己的映射"解析 real_sender_id，绝不可跨分片共用映射表

用法：
  python sync.py               同步一次（解密 + 抽取 + 落盘）
  python sync.py --rebuild     只用已有 jsonl 重渲染 md/csv
  python sync.py --dry-run     只报告会新增多少条，不写任何文件
  python sync.py --no-decrypt  跳过解密，直接读上一次的快照
  python sync.py --list-shards 诊断：列出目标群在各分片中的分布
  python sync.py --members     导出群成员清单
  python sync.py --stats       输出社群基础统计
  python sync.py --reset       丢弃已有归档，从当前数据库全量重建
"""
import argparse
import collections
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import datetime as dt

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

TYPE_HINT = {
    1: "", 3: "图片", 34: "语音", 43: "视频", 47: "表情", 48: "位置",
    49: "链接/文件", 50: "通话", 10000: "系统", 10002: "系统",
}

# 微信 4.x 把子类型塞进 local_type 高 32 位，判断类型前必须掩码
LOW32 = 0xFFFFFFFF

# appmsg（type 49）内部子类型
APPMSG_LABEL = {
    5: "链接", 6: "文件", 8: "表情", 19: "聊天记录", 24: "合并转发",
    33: "小程序", 36: "小程序", 44: "视频号", 49: "引用", 51: "视频号",
    57: "引用", 62: "视频号", 87: "群公告", 2000: "转账", 2001: "红包",
}


# ---------------------------------------------------------------- 工具函数
def log(*a):
    print(*a, flush=True)


def load_cfg():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


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


def render_body(local_type, raw, names=None, sender=""):
    """把原始内容渲染成给人看的一行。"""
    lt = local_type & LOW32
    raw = raw or ""
    body = raw
    if lt not in (10000, 10002):
        m = re.match(r"^([A-Za-z0-9_\-]{5,}):\n?(.*)$", raw, re.S)
        if m and ("wxid_" in m.group(1) or m.group(1).endswith("@chatroom")):
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
        main = xml_field(body, "title") or xml_field(body, "des")
        ref = ""
        if "<refermsg>" in body:
            rc = xml_field(body, "content")
            rn = xml_field(body, "displayname") or xml_field(body, "fromusr")
            if rc:
                # 被引用的是 app/xml 消息时，抠出可读文本
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
        return body[:300]
    return f"[{TYPE_HINT.get(lt, 'type' + str(lt))}]"


def load_names():
    """wxid -> 显示名（备注优先，其次昵称、微信号）"""
    p = os.path.join(DEC, "contact", "contact.db")
    out = {}
    if not os.path.exists(p):
        return out
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        for rm, nk, al, u in con.execute(
                "select remark,nick_name,alias,username from contact"):
            if u:
                out[u] = (rm or nk or al or "").strip()
        con.close()
    except Exception:
        pass
    return out


def load_members(chatroom):
    """从 contact.db 拿到群成员清单：[(wxid, 显示名, 群内备注名)]"""
    p = os.path.join(DEC, "contact", "contact.db")
    out = []
    if not os.path.exists(p):
        return out
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        rid = con.execute("select id from contact where username=?",
                          (chatroom,)).fetchone()
        if not rid:
            con.close()
            return out
        rid = rid[0]
        rows = con.execute("""
            select ct.username, ct.remark, ct.nick_name, ct.alias
            from chatroom_member m left join contact ct on ct.id = m.member_id
            where m.room_id = ?""", (rid,)).fetchall()
        for u, rm, nk, al in rows:
            if not u:
                continue
            out.append((u, (nk or al or u).strip(), (rm or "").strip()))
        # 群主字段
        con.close()
    except Exception as e:
        log("[!] 读群成员失败：", e)
    return out


def chat_meta(chatroom):
    """群名 / 群主"""
    p = os.path.join(DEC, "contact", "contact.db")
    name, owner = "", ""
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        r = con.execute("select nick_name, remark from contact where username=?",
                        (chatroom,)).fetchone()
        if r:
            name = (r[1] or r[0] or "").strip()
        r = con.execute("select owner from chat_room where username=?",
                        (chatroom,)).fetchone()
        if r:
            owner = r[0] or ""
        con.close()
    except Exception:
        pass
    return name, owner


# ---------------------------------------------------------------- 解密
def decrypt_snapshot(db_storage):
    if not os.path.exists(KEYS):
        log("[!] 缺少密钥文件，先跑一次 extract：")
        log(f'    {VENV_PY} {TOOL_PY} -v extract --db-dir "{db_storage}"')
        sys.exit(1)
    # 不删除旧目录：解密会覆盖同名 .db，旧文件留着无害
    # （注：沙箱会把 shutil.rmtree 劫持成"移入回收站"，故避免使用）
    os.makedirs(DATA, exist_ok=True)
    os.makedirs(DEC, exist_ok=True)
    r = subprocess.run(
        [VENV_PY, TOOL_PY, "decrypt", "--db-dir", db_storage,
         "--keys", KEYS, "--output", DEC],
        capture_output=True, text=True, cwd=TOOL_DIR,
    )
    tail = (r.stdout or "")[-400:]
    m = re.search(r"结果: (\d+) 成功, (\d+) 失败", r.stdout or "")
    if not m:
        log("[!] 解密失败：", tail)
        sys.exit(1)
    ok, bad = int(m.group(1)), int(m.group(2))
    if bad:
        log(f"[!] 有 {bad} 个库解密失败——密钥可能已失效，需要重新 extract")
    return ok, bad


# ---------------------------------------------------------------- 抽取
def collect(cfg, include_system=None):
    """跨所有分片收集目标群消息，按指纹去重，返回按时间升序的列表。

    关键：每个分片用自己的 Name2Id 解析发送者（跨分片 rowid 会冲突）。
    """
    chatroom = cfg["chatroom"]
    mode = (cfg.get("track_mode") or "whitelist").lower()
    track = cfg.get("track_senders") or {}
    selfs = {w: n for w, n in track.items()}
    if include_system is None:
        include_system = cfg.get("include_system", False)

    table = "Msg_" + md5hex(chatroom)
    names = load_names()
    merged = {}
    shard_info = []

    d = os.path.join(DEC, "message")
    if not os.path.isdir(d):
        log("[!] 无解密快照")
        return [], []

    for p in sorted(os.listdir(d)):
        if not (p.startswith("message_") and p.endswith(".db")):
            continue
        path = os.path.join(d, p)
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except Exception:
            continue
        if not con.execute("select 1 from sqlite_master where name=?",
                           (table,)).fetchone():
            con.close()
            continue
        smap = {r: u for r, u in
                con.execute("select rowid,user_name from Name2Id")}
        rows = con.execute(
            f"select local_type,create_time,real_sender_id,message_content,"
            f"compress_content,WCDB_CT_message_content from {table}"
        ).fetchall()
        shard_info.append((p, len(rows)))
        for lt, ct, rs, mc, cc, ctf in rows:
            wxid = smap.get(rs, "")
            if mode == "whitelist" and wxid not in track:
                continue
            if (lt & LOW32) in (10000, 10002) and not include_system:
                continue
            raw = dec_bytes(mc if mc is not None else cc, ctf)
            if not raw.strip():
                continue
            fp = hashlib.sha1(
                f"{ct}|{wxid}|{lt}|{raw}".encode("utf-8", "ignore")
            ).hexdigest()[:16]
            if fp in merged:
                continue
            if wxid in selfs:
                sender = selfs[wxid]          # 本人的两个号，用熟悉的名字
                is_self = True
            elif wxid:
                sender = names.get(wxid) or wxid
                is_self = False
            else:
                sender = "系统"
                is_self = False
            merged[fp] = {
                "fp": fp,
                "ts": int(ct or 0),
                "time": dt.datetime.fromtimestamp(ct or 0).strftime("%Y-%m-%d %H:%M"),
                "date": dt.datetime.fromtimestamp(ct or 0).strftime("%Y-%m-%d"),
                "sender": sender,
                "wxid": wxid,
                "self": is_self,
                "type": lt & LOW32,
                "raw": raw,   # 原始内容；渲染在输出阶段做
            }
        con.close()
    msgs = sorted(merged.values(), key=lambda m: (m["ts"], m["fp"]))
    return msgs, shard_info


# ---------------------------------------------------------------- 输出
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


def to_text(m):
    return render_body(m.get("type", 1), m.get("raw", m.get("text", "")) or "")


def render_md(msgs, cfg, members=None):
    n_self = sum(1 for m in msgs if m.get("self"))
    people = len({m["wxid"] for m in msgs if m.get("wxid")})
    lines = [
        f"# {cfg['chat_name']} · 对话归档",
        "",
        f"> 共 {len(msgs)} 条　| 发言人 {people} 人　| 我的发言 {n_self} 条　"
        f"| 更新 {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
    ]
    cur = None
    for m in msgs:
        if m["date"] != cur:
            cur = m["date"]
            wd = "一二三四五六日"[dt.datetime.strptime(cur, "%Y-%m-%d").weekday()]
            lines += ["", f"## {cur} 周{wd}", ""]
        t = m["time"][11:]
        tag = "（我）" if m.get("self") else ""
        lines.append(f"- `{t}` **{m['sender']}{tag}**：{to_text(m)}")
    lines.append("")
    return "\n".join(lines)


def export_csv(msgs, cfg, cp):
    with open(cp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["时间", "日期", "时刻", "发言人", "是否我", "wxid",
                    "类型", "内容"])
        for m in msgs:
            w.writerow([
                m["time"], m["date"], m["time"][11:], m["sender"],
                "是" if m.get("self") else "", m.get("wxid", ""),
                m.get("type", ""), to_text(m),
            ])


def sync(cfg, dry=False, no_decrypt=False, reset=False):
    if not no_decrypt:
        log("[1/3] 解密数据库快照 …")
        ok, bad = decrypt_snapshot(cfg["db_storage"])
        log(f"      {ok} 个库成功" + (f"，{bad} 个失败" if bad else ""))
    else:
        log("[1/3] 跳过解密，复用现有快照")

    mode = (cfg.get("track_mode") or "whitelist").lower()
    log(f"[2/3] 抽取群消息 …（模式：{'全群所有成员' if mode == 'all' else '白名单'}）")
    msgs, shards = collect(cfg)
    log(f"      分片分布 {shards}；匹配到 {len(msgs)} 条（去重后）")

    jp, mp, cp = paths(cfg)
    old = [] if reset else load_jsonl(jp)
    seen = {m["fp"] for m in old}
    added = [m for m in msgs if m["fp"] not in seen]

    log(f"[3/3] 归档：已有 {len(old)} 条，新增 {len(added)} 条")
    for m in added[:20]:
        log(f"      + [{m['time']}] {m['sender']}：{to_text(m)[:60]}")
    if len(added) > 20:
        log(f"      … 其余 {len(added)-20} 条略")

    if dry:
        log("（--dry-run，未写文件）")
        return added

    allm = sorted(old + added, key=lambda m: (m["ts"], m["fp"]))
    os.makedirs(ARCHIVE, exist_ok=True)
    with open(jp, "w", encoding="utf-8") as f:
        for m in allm:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_md(allm, cfg))
    log(f"      → {jp}")
    log(f"      → {mp}")
    if cfg.get("export_csv", True):
        export_csv(allm, cfg, cp)
        log(f"      → {cp}")
    return added


def rebuild(cfg):
    jp, mp, cp = paths(cfg)
    allm = sorted(load_jsonl(jp), key=lambda m: (m["ts"], m["fp"]))
    os.makedirs(ARCHIVE, exist_ok=True)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(render_md(allm, cfg))
    if cfg.get("export_csv", True):
        export_csv(allm, cfg, cp)
    log(f"已从 {len(allm)} 条记录重渲染 → {mp}")


def list_shards(cfg):
    table = "Msg_" + md5hex(cfg["chatroom"])
    d = os.path.join(DEC, "message")
    log(f"群 {cfg['chat_name']} / {cfg['chatroom']} → 表 {table}")
    if not os.path.isdir(d):
        log("[!] 无解密快照，先跑一次 sync")
        return
    for p in sorted(os.listdir(d)):
        if not (p.startswith("message_") and p.endswith(".db")):
            continue
        con = sqlite3.connect(f"file:{os.path.join(d,p)}?mode=ro", uri=True)
        if con.execute("select 1 from sqlite_master where name=?", (table,)).fetchone():
            n, mn, mx = con.execute(
                f"select count(*),min(create_time),max(create_time) from {table}"
            ).fetchone()
            log(f"  {p:16} {n:>6} 行  "
                f"{dt.datetime.fromtimestamp(mn or 0):%Y-%m-%d} → "
                f"{dt.datetime.fromtimestamp(mx or 0):%Y-%m-%d %H:%M}")
        con.close()


def dump_members(cfg):
    mem = load_members(cfg["chatroom"])
    name, owner = chat_meta(cfg["chatroom"])
    selfs = set((cfg.get("track_senders") or {}).keys())
    p = os.path.join(ARCHIVE, f"{cfg['chat_name']}-成员.md")
    os.makedirs(ARCHIVE, exist_ok=True)
    lines = [f"# {name or cfg['chat_name']} · 成员清单", "",
             f"> 共 {len(mem)} 人　| 群主：{owner}"
             f"{'（＝我的号' + (cfg.get('track_senders') or {}).get(owner, '') + '）' if owner else ''}",
             "", "| # | 群昵称/微信名 | 我的备注 | wxid |", "|---|---|---|---|"]
    for i, (u, disp, rm) in enumerate(sorted(mem, key=lambda x: x[1]), 1):
        me = " ★我" if u in selfs else ""
        lines.append(f"| {i} | {disp}{me} | {rm} | `{u}` |")
    lines.append("")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"成员清单（{len(mem)} 人）→ {p}")
    return mem


def stats(cfg):
    jp, mp, cp = paths(cfg)
    rows = sorted(load_jsonl(jp), key=lambda m: m["ts"])
    if not rows:
        log("[!] 归档为空，先跑一次 sync")
        return
    per_person = collections.Counter(r["sender"] for r in rows)
    per_day = collections.Counter(r["date"] for r in rows)
    per_hour = collections.Counter(r["time"][11:13] for r in rows)
    per_type = collections.Counter(r.get("type") for r in rows)
    first, last = rows[0]["time"], rows[-1]["time"]
    log(f"=== {cfg['chat_name']} 社群统计 ===")
    log(f"总消息 {len(rows)} 条 | 跨度 {first} → {last} "
        f"（{(dt.date.fromisoformat(rows[-1]['date']) - dt.date.fromisoformat(rows[0]['date'])).days} 天）")
    log(f"参与人数 {len(per_person)} | 有发言天数 {len(per_day)}")
    log("")
    log("-- 发言 TOP 15 --")
    for i, (s, c) in enumerate(per_person.most_common(15), 1):
        log(f"  {i:>2}. {c:>5} 条  {s}")
    log("")
    log("-- 最活跃的 10 天 --")
    for d, c in per_day.most_common(10):
        log(f"  {d}  {c} 条")
    log("")
    log("-- 24 小时活跃分布 --")
    for h in range(24):
        c = per_hour.get(f"{h:02d}", 0)
        if c:
            log(f"  {h:02d}时 {'█' * max(1, round(c / max(per_hour.values()) * 30))} {c}")
    log("")
    log("-- 消息类型 --")
    for t, c in per_type.most_common():
        log(f"  type{t}: {c}")


def main():
    ap = argparse.ArgumentParser(description="微信群聊归档流水线（只读）")
    ap.add_argument("--rebuild", action="store_true", help="只用已有 jsonl 重渲染")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    ap.add_argument("--no-decrypt", action="store_true", help="跳过解密，复用现有快照")
    ap.add_argument("--list-shards", action="store_true", help="诊断分片分布")
    ap.add_argument("--members", action="store_true", help="导出群成员清单")
    ap.add_argument("--stats", action="store_true", help="输出社群统计")
    ap.add_argument("--reset", action="store_true", help="丢弃已有归档，全量重建")
    a = ap.parse_args()
    cfg = load_cfg()
    if a.list_shards:
        list_shards(cfg)
    elif a.members:
        dump_members(cfg)
    elif a.stats:
        stats(cfg)
    elif a.rebuild:
        rebuild(cfg)
    else:
        sync(cfg, dry=a.dry_run, no_decrypt=a.no_decrypt, reset=a.reset)


if __name__ == "__main__":
    main()
