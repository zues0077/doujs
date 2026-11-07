# -*- coding: utf-8 -*-
"""B2 补丁引擎：按 anchors.json 的契约改造上游 lx-source.js。

设计原则（与 A 一次性改法的区别）：
  1. 锚点契约与替换文本全部外置到 anchors.json，上游漂移时人只改 JSON，不改代码；
  2. 三级判定 MATCH / DRIFT / SEMANTIC_DRIFT，任一 blocking 失配即整体失败；
  3. 失败时返回完整诊断数据，由 drift_report.py 生成人类可读报告；
  4. 绝不「猜测着继续」——不静默产出半兼容脚本。
"""
from __future__ import annotations
import difflib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Drift:
    anchor_id: str
    title: str
    severity: str
    why: str
    reason: str
    expected: str = ""
    actual_context: str = ""
    best_match_line: int = 0
    best_match_score: float = 0.0
    hint: str = ""


@dataclass
class PatchResult:
    ok: bool = True
    text: str = ""
    applied: list[dict[str, Any]] = field(default_factory=list)
    drifts: list[Drift] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    post_check_failures: list[str] = field(default_factory=list)
    scan_hits: list[dict[str, Any]] = field(default_factory=list)
    upstream_version: str = ""
    full_version: str = ""


def load_contract(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_upstream_version(src: str) -> str:
    m = re.search(r"^\s*\*\s*@version\s+(\d+\.\d+\.\d+)", src, re.MULTILINE)
    if not m:
        raise ValueError("无法从上游头部解析 @version x.y.z（上游改了头部格式？）")
    return m.group(1)


def _fmt(lines: list[str], ctx: dict[str, str]) -> str:
    """只替换已知占位符——不能走 str.format，锚点文本里全是 JS 大括号。"""
    out = []
    for x in lines:
        for k, v in ctx.items():
            x = x.replace("{" + k + "}", v)
        out.append(x)
    return "\n".join(out)


def _context(src: str, needle: str, window: int = 8) -> tuple[str, int, float]:
    """失配时找源码里与 needle 最相似的窗口，给人看「上游把它改成什么样了」。"""
    src_lines = src.splitlines()
    needle_lines = needle.splitlines()
    key = needle_lines[0].strip() if needle_lines else needle.strip()
    best_score, best_idx = 0.0, 0
    n = max(1, len(needle_lines))
    for i in range(len(src_lines)):
        chunk = "\n".join(src_lines[i:i + n])
        score = difflib.SequenceMatcher(None, key[:400], "\n".join(src_lines[i:i + n])[:400]).ratio()
        if score > best_score:
            best_score, best_idx = score, i
        if not key and i > 10:
            break
    lo, hi = max(0, best_idx - window), min(len(src_lines), best_idx + n + window)
    body = "\n".join(f"{j + 1:5d} | {src_lines[j]}" for j in range(lo, hi))
    return body, best_idx + 1, round(best_score, 3)


def _drift_for(a: dict[str, Any], reason: str, src: str, needle: str) -> Drift:
    body, line, score = _context(src, needle)
    return Drift(
        anchor_id=a["id"], title=a["title"], severity=a["severity"], why=a["why"],
        reason=reason, expected=needle, actual_context=body,
        best_match_line=line, best_match_score=score,
        hint="打开上游 commit diff，定位这处改动，然后更新 anchors.json 的 find/replace；"
             "改完先跑 `python tools/pipeline.py --upstream-file <本地文件>` 本地复现，再推 main。",
    )


def apply_patches(
    src: str,
    contract: dict[str, Any],
    patch_rev: int = 1,
) -> PatchResult:
    r = PatchResult()
    r.upstream_version = parse_upstream_version(src)
    # 版本号模板以契约为唯一真相源：早先在代码里硬编码后缀，改契约不生效，
    # 导致 post_check 与产物版本号不一致而在语义校验阶段被拦下。
    tpl = (contract.get("product") or {}).get("version_template") or "{upstream_version}-r{patch_rev}"
    r.full_version = (tpl.replace("{upstream_version}", r.upstream_version)
                         .replace("{patch_rev}", str(patch_rev)))
    ctx = {"upstream_version": r.upstream_version, "full_version": r.full_version}
    text = src.replace("\r\n", "\n")

    for a in contract["anchors"]:
        kind = a["kind"]
        aid, sev = a["id"], a["severity"]

        if kind == "replace_lines":
            lines = text.splitlines(keepends=False)
            idxs, prefixes = a["line_indexes"], a["line_prefixes"]
            bad = []
            for i, pref in zip(idxs, prefixes):
                if i >= len(lines) or not lines[i].strip().startswith(pref):
                    bad.append(f"L{i + 1} 期望前缀 {pref!r}，实际 {lines[i].strip()[:80] if i < len(lines) else '<EOF>'!r}")
            if bad:
                d = _drift_for(a, "头部行位置/前缀变化：" + "；".join(bad), text, "\n".join(prefixes))
                r.drifts.append(d)
                if sev == "blocking":
                    r.ok = False
                else:
                    r.warnings.append(f"{aid} 未命中（warning，继续）：{d.reason}")
                continue
            new_lines = a["replace"]
            for i, val in zip(idxs, _fmt(new_lines, ctx).split("\n")):
                lines[i] = val
            text = "\n".join(lines) + "\n"
            r.applied.append({"id": aid, "title": a["title"], "kind": kind,
                              "before": " / ".join(prefixes), "after": " / ".join(new_lines)[:160]})

        elif kind == "replace_block_once":
            needle = "\n".join(a["find"])
            count = text.count(needle)
            if count != 1:
                d = _drift_for(a, f"精确锚点出现 {count} 次（要求恰好 1 次）——上游改动了这段代码", text, needle)
                r.drifts.append(d)
                if sev == "blocking":
                    r.ok = False
                else:
                    r.warnings.append(f"{aid} 未命中（warning，继续）：{d.reason}")
                continue
            repl = _fmt(a["replace"], ctx)
            before_line = text[:text.index(needle)].count("\n") + 1
            text = text.replace(needle, repl, 1)
            r.applied.append({"id": aid, "title": a["title"], "kind": kind,
                              "before_line": before_line, "added_lines": len(a["replace"]) - len(a["find"])})

        elif kind == "regex_replace_once":
            flags = re.MULTILINE
            if "DOTALL" in a.get("flags", []):
                flags |= re.DOTALL
            pat = re.compile(a["find_regex"], flags)
            hits = list(pat.finditer(text))
            if len(hits) != 1:
                d = _drift_for(a, f"正则锚点命中 {len(hits)} 次（要求恰好 1 次）", text, a["find_regex"])
                r.drifts.append(d)
                if sev == "blocking":
                    r.ok = False
                else:
                    r.warnings.append(f"{aid} 未命中（warning，继续）：{d.reason}")
                continue
            repl = _fmt([a["replace"]], ctx)
            before_line = text[:hits[0].start()].count("\n") + 1
            text = pat.sub(lambda _m: repl, text, count=1)
            r.applied.append({"id": aid, "title": a["title"], "kind": kind, "before_line": before_line})

        else:
            raise ValueError(f"未知锚点类型: {kind}")

    # 语义后置校验：锚点都命中不代表结果语义正确
    pc = contract["post_checks"]
    for pat in pc["must"]:
        if not re.search(pat, text):
            r.post_check_failures.append(f"缺少必要结构: {pat}")
    for pat in pc["must_not"]:
        if re.search(pat, text, re.MULTILINE):
            r.post_check_failures.append(f"残留禁止结构: {pat}")
    if r.post_check_failures:
        r.ok = False
        r.drifts.append(Drift(
            anchor_id="POSTCHECK", title="替换后语义校验失败", severity="blocking",
            why="锚点文本仍可匹配，但改造后的结构已不符合服务端协议契约",
            reason="；".join(r.post_check_failures),
            actual_context="", expected="见 post_checks.must / must_not",
            hint="多半是上游在锚点附近插入了新代码，或新增了第二处等价出口；按报告逐条核对。",
        ))

    for s in contract["global_scans"]:
        hits = len(re.findall(s["pattern"], text, re.MULTILINE))
        item = {"id": s["id"], "title": s["title"], "hits": hits,
                "max_hits": s["max_hits"], "severity": s["severity"], "why": s["why"]}
        r.scan_hits.append(item)
        if hits > s["max_hits"]:
            msg = f"{s['id']} {s['title']}: 命中 {hits} 次 > 上限 {s['max_hits']}"
            if s["severity"] == "blocking":
                r.ok = False
                r.drifts.append(Drift(anchor_id=s["id"], title=s["title"], severity="blocking",
                                      why=s["why"], reason=msg, hint="检查上游新增代码，必要时新增锚点覆盖。"))
            else:
                r.warnings.append(msg + "（warning，继续）")

    r.text = text
    return r
