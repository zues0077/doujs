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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

CST = timezone(timedelta(hours=8))


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
    summary = reports / "ci-summary.md"
    lines = [
        f"## B2 追新 · {data['status']}",
        "",
        f"- 上游 blob sha：`{data['upstream']['blob_sha'][:12]}`",
        f"- 判据：`{data['change_verdict']}`",
        f"- 退出码：`{data['exit_code']}`",
    ]
    if data["status"] == "BUILD_OK":
        lines += [f"- 新产物：`{data.get('full_version')}` `{data.get('product_sha256', '')[:16]}…`",
                  f"- 门禁：{sum(1 for g in data.get('gates', []) if g['ok'])}/{len(data.get('gates', []))} PASS"]
    elif data["status"] != "NO_CHANGE":
        lines += ["", "### 需要人工更新锚点", ""]
        for d in data.get("drifts", []):
            lines.append(f"- `{d['anchor_id']}` {d['title']}：{d['reason'][:160]}")
        _dm = out.get('drift_md', 'reports/')
        # 只保留相对路径：绝对路径会把本机工作区目录名带进公开仓库的 CI Summary
        _dm = "reports/" + Path(str(_dm)).name if str(_dm) != 'reports/' else _dm
        lines += ["", f"完整修复工单见 `{_dm}`"]
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    out["ci_summary"] = str(summary)
    return out
