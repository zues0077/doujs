# -*- coding: utf-8 -*-
"""锚点漂移 / 构建运行报告生成器。

用户诉求：「上游一旦动到锚点，脚本会直接报错停下，需要人工更新锚点——这是好事，
          我也需要有一定的机制报告我修改」。

报告分两类：
  - RUN-*.md：每次运行都写（含 NO_CHANGE / BUILD_OK / DRIFT），供审计与变更通知；
  - ANCHOR-DRIFT-*.md：仅失配时写，是可直接照着动手的修复工单。
"""
from __future__ import annotations
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 运行历史
# 缺口：RUN-*.md 被 gitignore，只活在 artifact 里（retention 30 天），40 天后
# 「那天为什么挂」永久无证；latest.json 是覆盖写，只有「这一刻」，看不出趋势。
# 这里按月累积一条稀疏时间线，入库，跟 git log 同寿命。
#
# 为什么是「稀疏」而不是每 run 一行：
#   每天一次 schedule，若每 run 都追加，history 每天都有 diff → 每天都得 commit，
#   一年 365 个空提交，与 monthly heartbeat「同月跳过、不污染历史」的取向冲突。
#   稀疏规则：进入 NO_CHANGE 的那天记一行，期间连续 NO_CHANGE 不记；
#   BUILD_OK 与任何失败每次都记（这两类本来就伴随提交 / Issue，零额外成本）。
HISTORY_REL = Path("state") / "history.jsonl"
HISTORY_MAX = 200
_KEEP_STATUSES = ("BUILD_OK", "NO_CHANGE", "ANCHOR_DRIFT", "GATE_FAILED", "FETCH_FAILED")


def _history_enabled() -> bool:
    """默认只在 CI 里写；本地 dry-run 不写，避免每次复现都把工作区弄脏。

    DOUJS_HISTORY=on  强制写（本地验证用）
    DOUJS_HISTORY=off 强制不写
    未设置时看 GITHUB_ACTIONS：CI 为 true。
    """
    v = (os.environ.get("DOUJS_HISTORY") or "").strip().lower()
    if v in ("off", "0", "false", "no"):
        return False
    if v in ("on", "1", "true", "yes"):
        return True
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _iso_utc(run_at: str) -> str:
    """run_at 是 CST 人类可读串，history 一律存 UTC ISO8601。

    混用时区是审计口径出错的头号原因：本地 12:00 与 UTC 04:00 是同一条记录，
    按字符串比时间会差 8 小时（这个坑在 lxserver 8h 报告里踩过，见手册 P-1348）。
    """
    try:
        dt = datetime.strptime(run_at, "%Y-%m-%d %H:%M:%S %z")
    except Exception:
        dt = datetime.now(CST)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cst_short(iso: str) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc).astimezone(CST)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso or "n/a"


def _age_days(iso: str) -> int | None:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _history_record(data: dict[str, Any]) -> dict[str, Any]:
    """一行 = 一次判定的摘要。几十字节。

    脱敏红线：本文件会入库且仓库是 public，**只放** blob 短 sha / 版本号 /
    产物 sha256 短值 / 状态与退出码。绝不出现上游 owner/repo/raw URL ——
    这些只从仓库 Variables 注入，写进来等于把上游来源公开进代码搜索。
    """
    up = data.get("upstream") or {}
    gates = list(data.get("gates") or [])
    obf = list(data.get("obf_gates") or [])
    allg = gates + obf
    return {
        "at": _iso_utc(data.get("run_at", "")),
        "status": data.get("status", ""),
        "code": data.get("exit_code", 0),
        "blob": (up.get("blob_sha") or "")[:12],
        "up_ver": up.get("version") or "",
        "ver": data.get("full_version") or "",
        "prod": (data.get("product_sha256") or "")[:12],
        "gates": "%d/%d" % (sum(1 for g in gates if g.get("ok")), len(gates)),
        "obf_gates": "%d/%d" % (sum(1 for g in obf if g.get("ok")), len(obf)),
        "fail": [str(g.get("name")) for g in allg if not g.get("ok")][:6],
        "drifts": len(data.get("drifts") or []),
        "run": os.environ.get("GITHUB_RUN_NUMBER", ""),
        "dry": bool(data.get("dry_run")),
    }


def _should_record(prev: dict[str, Any] | None, rec: dict[str, Any]) -> bool:
    if prev is None:
        return True
    if rec["status"] != prev.get("status"):
        return True                 # 状态切换点：进入/退出 NO_CHANGE、开始失败、恢复成功
    if rec["status"] != "NO_CHANGE":
        return True                 # BUILD_OK / 失败：每次都记（连续失败计数要靠它）
    return False                    # 连续 NO_CHANGE：只保留段首那一行，零 diff


def _history_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    # 「上次构建成功」必须排除 dry-run：dry-run 的 status 也是 BUILD_OK，
    # 但它没有产物可下载，混进来会让人以为线上已经有新版。
    last_ok = next((r for r in reversed(records)
                    if r.get("status") == "BUILD_OK" and not r.get("dry")), None)
    last_ok_at = (last_ok or {}).get("at", "")
    fails = 0
    for r in reversed(records):
        if r.get("status") in ("BUILD_OK", "NO_CHANGE"):
            break
        fails += 1
    nc_since = ""
    for r in reversed(records):
        if r.get("status") == "NO_CHANGE":
            nc_since = r.get("at", "")
        else:
            break
    return {
        "n": len(records),
        "last_at": (records[-1].get("at", "") if records else ""),
        "last_status": (records[-1].get("status", "") if records else ""),
        "last_ok_at": last_ok_at,
        "last_ok_age_days": _age_days(last_ok_at) if last_ok_at else None,
        "consecutive_failures": fails,
        "no_change_since": nc_since,
        "no_change_days": _age_days(nc_since) if nc_since else None,
    }


def _read_history(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue            # 坏行直接跳过，history 不能因为一行残缺让流水线挂掉
        if isinstance(rec, dict):
            out.append(rec)
    return out


def append_history(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    """把本次判定写进 state/history.jsonl（稀疏）。返回本次是否写入 + 聚合指标。"""
    path = root / HISTORY_REL
    records = _read_history(path)
    stats = _history_stats(records)
    if not _history_enabled():
        return {"recorded": False, "reason": "disabled", **stats}

    rec = _history_record(data)
    if rec["status"] not in _KEEP_STATUSES:
        return {"recorded": False, "reason": f"status {rec['status']} 不入账", **stats}

    prev = records[-1] if records else None
    # 同一次 run 被重复调用（脚本重跑 / write_reports 二次进入）时幂等
    if prev and rec["run"] and prev.get("run") == rec["run"] and prev.get("status") == rec["status"]:
        return {"recorded": False, "reason": "same run", **stats}
    if not _should_record(prev, rec):
        return {"recorded": False, "reason": "no_change 段内", **stats}

    records.append(rec)
    records = records[-HISTORY_MAX:]          # 只保留最近 200 条（≈ 数年事件量）
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records) + "\n",
        encoding="utf-8", newline="\n")
    return {"recorded": True, "reason": "", **(_history_stats(records))}


def now() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S %z")


def tag() -> str:
    return datetime.now(CST).strftime("%Y%m%d-%H%M%S")


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def git_blob_sha(b: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(b)).encode() + b"\0" + b).hexdigest()


def _impact_layer(anchor_id: str) -> str:
    return {
        "A1": "④状态层（指纹变更影响 sourceGuard 旧隔离解除）",
        "A2": "①返回值形态（形态开关快照）",
        "A3": "②actions 声明合规",
        "A4": "①返回值形态（决定性根因）",
        "A5": "③600s 自排探测卡顿",
        "A6": "可观测性（stdout 噪音）",
        "S1": "②actions 声明合规",
        "S2": "③600s 自排探测卡顿",
        "S3": "①返回值形态",
        "S4": "③600s 自排探测卡顿",
        "POSTCHECK": "整体协议契约",
    }.get(anchor_id, "整体协议契约")


def _drift_section(d: Any) -> str:
    return f"""### `{d.anchor_id}` · {d.title}

- **严重级别**：`{d.severity}`　**影响层**：{_impact_layer(d.anchor_id)}
- **失配原因**：{d.reason}
- **为什么这处必须改**：{d.why}
- **最相似位置**：上游 L{d.best_match_line} 附近（相似度 {d.best_match_score}）

<details><summary>期望的锚点文本</summary>

```
{d.expected}
```

</details>

<details><summary>上游当前实际代码（最相似位置 ±8 行）</summary>

```
{d.actual_context}
```

</details>

**建议动作**：{d.hint}

"""


def build_report_markdown(data: dict[str, Any]) -> str:
    status = data["status"]
    head = f"""# B2 追新运行报告 · {status}

- 运行时间：{data['run_at']}
- 上游：branch `{data['upstream'].get('branch', 'n/a')}` / 文件 `{data['upstream']['path']}`（owner/repo 由仓库 Variables 注入，不写进报告）
- 上游 blob sha：`{data['upstream']['blob_sha']}`（{data['upstream']['blob_note']}）
- 上游 head：`{data['upstream'].get('head_sha', 'n/a')}`
- 上次成功产物：`{data.get('last_good_version', 'n/a')}` / `{data.get('last_good_sha256', 'n/a')[:16]}…`
- 判据结果：`{data['change_verdict']}`
- 退出码：`{data['exit_code']}`

"""
    if status == "NO_CHANGE":
        return head + """## 结论：无需重建

上游 `lx-source.js` 的 blob sha 与上次成功构建一致，仓库 head 可能已前进（多为 docs /
巡检数据提交），但核心脚本没变。**不重打补丁、不动 dist/、不推 main、不通知猎手**。
这是刻意设计：判据用 blob sha 而非 head，避免无意义版本抖动。

"""

    if status == "BUILD_OK":
        applied = "\n".join(
            f"- `{a['id']}` {a['title']}" for a in data.get("applied", [])
        )
        warnings = data.get("warnings") or []
        warn_txt = ("\n## 告警（不阻断）\n\n" + "\n".join(f"- {w}" for w in warnings)) if warnings else ""
        scans = "\n".join(
            f"- `{s['id']}` {s['title']}：命中 {s['hits']} / 上限 {s['max_hits']}（{s['severity']}）"
            for s in data.get("scan_hits", [])
        )
        gates = "\n".join(
            f"- {'PASS' if g['ok'] else 'FAIL'} {g['name']}：{str(g.get('detail', ''))[:180]}"
            for g in data.get("gates", [])
        )
        return head + f"""## 结论：构建成功，产物已更新

- 产物版本：`{data.get('full_version')}`
- 产物 sha256：`{data.get('product_sha256')}`
- 产物路径：`dist/{data['product']['fixed_filename']}`

### 本次生效的锚点

{applied}

### 静态哨兵

{scans}

### 门禁

{gates}
{warn_txt}
"""

    # DRIFT / GATE_FAILED
    drifts = data.get("drifts", [])
    dsecs = "\n".join(_drift_section(
        type("D", (), {
            "anchor_id": d["anchor_id"], "title": d["title"], "severity": d["severity"],
            "reason": d["reason"], "why": d["why"], "expected": d.get("expected", ""),
            "actual_context": d.get("actual_context", ""),
            "best_match_line": d.get("best_match_line", 0),
            "best_match_score": d.get("best_match_score", 0), "hint": d.get("hint", ""),
        })()
    ) for d in drifts)

    gates = "\n".join(
        f"- {'PASS' if g['ok'] else 'FAIL'} {g['name']}：{str(g.get('detail', ''))[:200]}"
        for g in data.get("gates", [])
    )

    return head + f"""## 结论：构建已阻断，线上产物保持上一版

> **防静默保护已生效**：`dist/`、`state.json`、`manifest.json` 均未改动，
> 采集器继续采集上一版可用产物，不会灌入半兼容脚本。
> 上一版：`{data.get('last_good_version', 'n/a')}`（sha256 `{data.get('last_good_sha256', 'n/a')[:16]}…`）

### 上游改动定位

- 上游标识：见仓库 Settings → Variables 的 `UPSTREAM_REPO`（不在报告与代码里出现，避免被代码搜索反查）
- 定位方式：打开上游仓库，看 `{data['upstream']['path']}` 在 branch `{data['upstream'].get('branch', 'master')}` 的最近提交，
  与上一成功 blob `{data.get('last_good_sha256_src', '（见 state.json）')}` 对比
- 当前 blob sha：`{data['upstream']['blob_sha']}`

### 失配明细（共 {len(drifts)} 项）

{dsecs if dsecs else '（本次无锚点失配，失败来自门禁）'}

### 门禁结果

{gates if gates else '（未执行到门禁）'}

### 人工修复清单（按顺序做完再重跑）

1. 打开上面的 diff 链接，确认上游到底改了什么（是重构、加参数，还是换了实现）。
2. 编辑仓库根目录 `anchors.json`：更新对应锚点的 `find` / `find_regex` / `replace`；
   若上游新增了等价出口，新增一个锚点并在 `post_checks` / `global_scans` 补哨兵。
3. 本地复现：`python tools/pipeline.py --upstream-file <上游文件> --smoke-test <上游冒烟测试>`，
   必须看到 `BUILD_OK` 且全部门禁 PASS。
4. 回归 17951：`upload → toggle` 后跑 30 分钟相位采样，确认 `longTimers600s=0`、
   `stallAttributions=0`，并做一次真实网络取链。
5. 推 main 触发 CI；CI 会自动关闭本 Issue 对应的 drift 报告并发布新产物。
6. 确认采集器采到新版本后，再决定是否推广到生产容器。

### 四层禁用链条对照

| 层 | 风险 | 本次是否受影响 |
|---|---|---|
| ① 返回值形态 | 对象 → 判「未返回有效的链接字符串」→ 巡检第 2 次失败关平台 | {'是' if any(_impact_layer(d['anchor_id']).startswith('①') for d in drifts) else '否'} |
| ② actions 声明 | 非 local 声明私有扩展 → 严格宿主过滤/初始化失败 | {'是' if any(_impact_layer(d['anchor_id']).startswith('②') for d in drifts) else '否'} |
| ③ 自排探测 | 600s 周期堵事件循环 → 卡顿归因/熔断/隔离 | {'是' if any(_impact_layer(d['anchor_id']).startswith('③') for d in drifts) else '否'} |
| ④ 状态残留 | states / source-guard / .js.disabled | 需重新灰度时单独清 |

"""


def write_reports(root: Path, data: dict[str, Any]) -> dict[str, str]:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    out: dict[str, str] = {}

    # 先记账：下面 Summary 要引用聚合指标（连续无变化天数 / 上次成功距今）
    hist = append_history(root, data)

    latest = reports / "latest.json"
    latest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    out["latest_json"] = str(latest)

    run_md = reports / f"RUN-{tag()}.md"
    run_md.write_text(build_report_markdown(data), encoding="utf-8", newline="\n")
    out["run_md"] = str(run_md)

    if data["status"] in ("ANCHOR_DRIFT", "GATE_FAILED"):
        short = (data["upstream"]["blob_sha"] or "unknown")[:8]
        drift_md = reports / f"ANCHOR-DRIFT-{short}.md"
        drift_md.write_text(build_report_markdown(data), encoding="utf-8", newline="\n")
        out["drift_md"] = str(drift_md)

    # GitHub Actions Job Summary（CI 里由 workflow 再 append，这里本地也生成一份）
    # 同时是 Issue 正文：这里是「多少天没成功」唯一会被眼睛看到的地方。
    summary = reports / "ci-summary.md"
    hb: list[str] = []
    if hist.get("consecutive_failures"):
        hb.append(f"⚠ 连续失败 {hist['consecutive_failures']} 次")
    if hist.get("no_change_since"):
        d = hist.get("no_change_days")
        hb.append(f"连续无变化自 {_cst_short(hist['no_change_since'])}"
                  + (f"（已 {d} 天）" if d is not None else ""))
    if hist.get("last_ok_at"):
        d = hist.get("last_ok_age_days")
        hb.append(f"上次构建成功 {_cst_short(hist['last_ok_at'])}"
                  + (f"（{d} 天前）" if d is not None else ""))
    hb.append(f"累计 {hist.get('n', 0)} 条 · `state/history.jsonl`")
    lines = [
        f"## B2 追新 · {data['status']}",
        "",
        f"- 上游 blob sha：`{data['upstream']['blob_sha'][:12]}`",
        f"- 判据：`{data['change_verdict']}`",
        f"- 退出码：`{data['exit_code']}`",
        "- 运行历史：" + " ｜ ".join(hb),
    ]
    if data["status"] == "BUILD_OK":
        lines += [f"- 新产物：`{data.get('full_version')}` `{data.get('product_sha256', '')[:16]}…`",
                  f"- 门禁：{sum(1 for g in data.get('gates', []) if g['ok'])}/{len(data.get('gates', []))} PASS"]
    elif data["status"] != "NO_CHANGE":
        # 标题必须与真实原因一致：GATE_FAILED 且 drifts 为空时，根因通常在取材/依赖，
        # 写「需要人工更新锚点」会让人白改一圈 anchors.json（2026-11-08 G4 缺
        # endpoint-registry 就是这么误导的，Issue 标题已分流，正文这里补上）。
        if data.get("drifts"):
            lines += ["", f"### 需要人工更新锚点（{len(data['drifts'])} 项）", ""]
            for d in data.get("drifts", []):
                lines.append(f"- `{d['anchor_id']}` {d['title']}：{d['reason'][:160]}")
        else:
            lines += ["", "### 门禁未通过（锚点 0 失配）", "",
                      "先查取材/依赖（冒烟用例是否缺同仓模块）或真实断言失败，"
                      "**不要改 `anchors.json`**。"]
        _dm = out.get('drift_md', 'reports/')
        # 只保留相对路径：绝对路径会把本机工作区目录名带进公开仓库的 CI Summary
        _dm = "reports/" + Path(str(_dm)).name if str(_dm) != 'reports/' else _dm
        lines += ["", f"完整修复工单见 `{_dm}`"]
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    out["ci_summary"] = str(summary)
    return out
