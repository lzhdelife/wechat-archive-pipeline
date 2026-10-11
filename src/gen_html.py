#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_html.py — 把归档 jsonl 渲染成单文件 HTML 阅读版

读取 `sync.py` / `sync_pm.py` 产出的 `.jsonl`，生成一个**单文件、可搜索、
可筛选（只看我 / 只看对方）**的 HTML 页面：米色纸感背景、气泡布局、按日期分组、
关键词高亮。适合长期保存与翻阅，也便于二次加工。

用法：
    python gen_html.py --jsonl data/archive/与某人.jsonl
    python gen_html.py --jsonl data/archive/与某人.jsonl -o 输出.html \
        --peer-name "对方" --self-name "我"

jsonl 每条记录字段（与 src/sync_pm.py 输出一致）：
    {"fp":..., "ts":..., "time":"YYYY-MM-DD HH:MM", "date":"YYYY-MM-DD",
     "self":bool, "wxid":..., "type":int, "raw":str}
"""
import argparse
import html
import json
import os
import re
import sys
from collections import Counter

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


def xml_field(txt, tag):
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", txt, re.S)
    return (m.group(1).strip() if m else "")


def render_body(local_type, raw):
    """把原始内容渲染成给人看的一行（与 src/sync_pm.py 的 render_body 保持一致）。"""
    lt = int(local_type or 0) & LOW32
    raw = raw or ""
    body = raw
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
        main_src = re.sub(r"<refermsg>.*?</refermsg>", "", body, flags=re.S)
        main = xml_field(main_src, "title") or xml_field(main_src, "des")
        ref = ""
        if "<refermsg>" in body:
            rc = xml_field(body, "content")
            rn = xml_field(body, "displayname") or xml_field(body, "fromusr")
            if rc:
                rc = html.unescape(rc)
                if "<" in rc:
                    rc = xml_field(rc, "title") or xml_field(rc, "des") or re.sub(r"<[^>]+>", "", rc).strip()
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


CSS = """
:root{--paper:#f7f2e7;--paper2:#fffdf7;--ink:#3a3226;--ink2:#8a7f6c;--line:#e3d9c6;
--self:#d8ebd1;--peer:#ffffff;--accent:#b3452f;--accent2:#8a6d3b;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;line-height:1.75;}
.ribbon{height:8px;background:linear-gradient(90deg,var(--accent),#c9752f,var(--accent2));}
header{position:sticky;top:0;z-index:20;background:rgba(247,242,231,.96);
backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:18px 22px 12px;}
h1{font-family:Georgia,"Songti SC","Noto Serif SC",serif;font-size:24px;margin:0 0 6px;letter-spacing:.5px;}
.meta{font-size:13px;color:var(--ink2);}
.meta b{color:var(--accent);}
.tools{margin-top:10px;display:flex;gap:10px;flex-wrap:wrap;align-items:center;}
.tools input{flex:1;min-width:200px;padding:8px 12px;border:1px solid var(--line);
border-radius:999px;background:var(--paper2);font-size:14px;color:var(--ink);outline:none;}
.tools input:focus{border-color:var(--accent2);}
.tools button{padding:8px 14px;border:1px solid var(--line);border-radius:999px;
background:var(--paper2);cursor:pointer;font-size:13px;color:var(--ink);}
.tools button.on{background:var(--self);border-color:#b6d3ab;}
main{max-width:820px;margin:0 auto;padding:26px 18px 90px;}
.day{text-align:center;margin:30px 0 14px;}
.day span{font-size:12px;color:var(--ink2);background:var(--paper2);
border:1px solid var(--line);border-radius:999px;padding:3px 14px;}
.msg{display:flex;margin:10px 0;}
.msg.self{justify-content:flex-end;}
.av{width:34px;height:34px;border-radius:50%;flex:0 0 34px;display:flex;
align-items:center;justify-content:center;font-size:12px;color:#fff;margin-top:2px;}
.msg.self .av{background:#7aa06a;order:2;margin-left:8px;}
.msg.peer .av{background:#c98b6b;margin-right:8px;}
.bub{max-width:72%;background:var(--peer);border:1px solid var(--line);
border-radius:14px;padding:8px 12px;box-shadow:0 1px 2px rgba(120,100,70,.06);
white-space:pre-wrap;word-break:break-word;font-size:15px;}
.msg.self .bub{background:var(--self);border-color:#bcd6b0;}
.who{font-size:11px;color:var(--ink2);margin-bottom:2px;}
.time{font-size:10px;color:#b3a894;margin-top:3px;text-align:right;}
.sys{text-align:center;font-size:12px;color:var(--ink2);margin:8px 0;}
mark{background:#ffe9a8;padding:0 2px;border-radius:3px;}
.hide{display:none;}
footer{max-width:820px;margin:0 auto;padding:0 18px 60px;font-size:12px;color:var(--ink2);}
"""


def build(msgs, title, peer_name, self_name):
    n_self = sum(1 for m in msgs if m.get("self"))
    n_peer = len(msgs) - n_self
    span = f"{msgs[0]['time']} ~ {msgs[-1]['time']}" if msgs else "-"
    types = Counter(render_type_label(m.get("type", 1)) for m in msgs)
    top = "、".join(f"{k} {v}" for k, v in types.most_common(6) if k)

    parts = [
        "<!DOCTYPE html>\n<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">\n",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n",
        f"<title>{html.escape(title)}</title>\n<style>{CSS}</style></head><body>\n",
        '<div class="ribbon"></div>\n<header>\n',
        f"<h1>{html.escape(title)}</h1>\n",
        f'<div class="meta">消息 <b>{len(msgs)}</b> 条（{html.escape(self_name)} {n_self} / '
        f"{html.escape(peer_name)} {n_peer}）<br>时间跨度：{html.escape(span)}　主要类型：{html.escape(top)}</div>\n",
        '<div class="tools">\n<input id="q" placeholder="搜索内容…">\n',
        '<button id="fAll" class="on" onclick="setF(\'all\')">全部</button>\n',
        '<button id="fSelf" onclick="setF(\'self\')">只看我</button>\n',
        '<button id="fPeer" onclick="setF(\'peer\')">只看对方</button>\n',
        "</div>\n</header>\n<main id=\"main\">\n",
    ]

    cur = None
    for m in msgs:
        d = m.get("date") or (m.get("time") or "")[:10]
        if d != cur:
            cur = d
            parts.append(f'<div class="day" data-day="1"><span>{html.escape(d)}</span></div>')
        sp = "self" if m.get("self") else "peer"
        text = render_body(m.get("type", 1), m.get("raw", ""))
        t = (m.get("time") or "")[11:16]
        who = self_name if m.get("self") else peer_name
        av = who[:1] or ("我" if sp == "self" else "对")
        safe = html.escape(text)
        if (int(m.get("type", 1) or 0) & LOW32) in (10000, 10002):
            parts.append(f'<div class="sys msg-line" data-sp="{sp}" data-text="{safe}">{safe}</div>')
            continue
        parts.append(
            f'<div class="msg {sp} msg-line" data-sp="{sp}" data-text="{safe}">'
            f'<div class="av">{html.escape(av)}</div><div class="bub"><div class="who">{html.escape(who)}</div>'
            f'<div class="body">{safe}</div><div class="time">{html.escape(t)}</div></div></div>'
        )

    parts.append(
        """</main>
<footer>
说明：本页由本地微信数据库离线解密后导出，仅供个人归档。<br>
语音/视频消息本地无文字内容，图片以 [图片] 占位；撤回消息按系统提示保留。
</footer>
<script>
var lines=[].slice.call(document.querySelectorAll('.msg-line'));
var days=[].slice.call(document.querySelectorAll('.day'));
var mode='all';
function setF(m){mode=m;
 document.getElementById('fAll').className=(m==='all'?'on':'');
 document.getElementById('fSelf').className=(m==='self'?'on':'');
 document.getElementById('fPeer').className=(m==='peer'?'on':'');
 apply();}
function escapeHtml(s){return s.replace(/[&<>]/g,function(c){return{'&':'&amp;','<':'&lt;','>':'&gt;'}[c];});}
function apply(){
 var q=document.getElementById('q').value.trim().toLowerCase();
 lines.forEach(function(el){
   var sp=el.getAttribute('data-sp');
   var okm=(mode==='all')||(mode===sp);
   var okq=!q||el.getAttribute('data-text').toLowerCase().indexOf(q)>=0;
   el.classList.toggle('hide',!(okm&&okq));
 });
 days.forEach(function(d){
   var sib=d.nextElementSibling,any=false;
   while(sib&&!sib.hasAttribute('data-day')){
     if(!sib.classList.contains('hide')){any=true;break;}
     sib=sib.nextElementSibling;}
   d.classList.toggle('hide',!any);
 });
 lines.forEach(function(el){
   var b=el.querySelector('.body')||el;
   if(el.classList.contains('hide'))return;
   var raw=b.textContent;
   if(!q){if(b.querySelector('mark')){b.textContent=raw;}return;}
   var i=raw.toLowerCase().indexOf(q);
   if(i<0)return;
   b.innerHTML=escapeHtml(raw.slice(0,i))+'<mark>'+escapeHtml(raw.slice(i,i+q.length))+'</mark>'+escapeHtml(raw.slice(i+q.length));
 });
}
document.getElementById('q').addEventListener('input',apply);
</script></body></html>"""
    )
    return "".join(parts)


def render_type_label(t):
    base = int(t or 1) & LOW32
    if base == 1:
        return "文本"
    return TYPE_HINT.get(base) or f"type={base}"


def main():
    ap = argparse.ArgumentParser(description="把归档 jsonl 渲染成单文件 HTML 阅读版")
    ap.add_argument("--jsonl", required=True, help="归档 jsonl 路径")
    ap.add_argument("-o", "--out", help="输出 html 路径（默认与 jsonl 同目录同名）")
    ap.add_argument("--title", help="页面标题（默认由文件名推断）")
    ap.add_argument("--peer-name", default="对方", help="对方显示名")
    ap.add_argument("--self-name", default="我", help="自己显示名")
    a = ap.parse_args()

    msgs = []
    with open(a.jsonl, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    msgs.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    if not msgs:
        print("[!] jsonl 里没有消息")
        return 1
    msgs.sort(key=lambda m: (m.get("ts", 0), m.get("fp", "")))

    stem = os.path.splitext(os.path.basename(a.jsonl))[0]
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.jsonl)), stem + ".html")
    title = a.title or f"与 {a.peer_name} 的聊天记录 · {stem}"
    with open(out, "w", encoding="utf-8") as f:
        f.write(build(msgs, title, a.peer_name, a.self_name))
    print(f"[+] {len(msgs)} 条 -> {out} ({os.path.getsize(out) / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
