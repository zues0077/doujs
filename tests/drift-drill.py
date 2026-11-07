# -*- coding: utf-8 -*-
"""漂移演练：验证「上游改了锚点 → 构建阻断 → 产物与状态不被破坏」这条关键路径。

用法：
  python tests/drift-drill.py --upstream <上游 lx-source.js> --smoke-test <上游冒烟测试>

判据（任一不满足即退出 1）：
  1. 演练 A（改 A4 返回块）退出码 == 2，且 reports/ANCHOR-DRIFT-*.md 生成
  2. 演练 B（新增 actions.push）退出码 == 2
  3. 两次演练后 dist 产物的 sha256 与演练前完全一致
  4. 两次演练后 state.json 的 full_version 与 upstream.blob_sha 未被推进
"""
from __future__ import annotations
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRODUCT = ROOT / "dist" / "doujs.js"
STATE = ROOT / "state" / "state.json"


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""


def state_sig() -> tuple[str, str]:
    if not STATE.is_file():
        return ("", "")
    d = json.loads(STATE.read_text(encoding="utf-8"))
    return (d.get("full_version", ""), (d.get("upstream") or {}).get("blob_sha", ""))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--smoke-test", default="")
    args = ap.parse_args()

    src = Path(args.upstream).read_text(encoding="utf-8")
    fx = ROOT / "state" / "fixtures"
    fx.mkdir(parents=True, exist_ok=True)

    a = src.replace("    .catch(err => Promise.reject(err));\n}",
                    "    .catch(err => { throw err });\n}", 1)
    b = src.replace('  actions.push("trend"); // v8.0：健康趋势 action（返回历史健康快照数组，App 折线图展示）',
                    '  actions.push("trend"); // v8.0：健康趋势 action（返回历史健康快照数组，App 折线图展示）\n'
                    '  actions.push("metrics"); // 演练：上游新增 action', 1)
    if a == src or b == src:
        print("✗ 无法构造演练样本（上游对应锚点文本已变，请先更新 anchors.json）")
        return 1
    (fx / "drill-a4.js").write_text(a, encoding="utf-8", newline="\n")
    (fx / "drill-s1.js").write_text(b, encoding="utf-8", newline="\n")

    before_sha, before_state = sha(PRODUCT), state_sig()
    results = []
    for name, sample in (("A4 返回块被重构", fx / "drill-a4.js"), ("新增 actions.push", fx / "drill-s1.js")):
        cmd = [sys.executable, str(ROOT / "tools" / "pipeline.py"), "--upstream-file", str(sample)]
        if args.smoke_test:
            cmd += ["--smoke-test", args.smoke_test]
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        results.append((name, p.returncode, (p.stdout or "") + (p.stderr or "")))

    after_sha, after_state = sha(PRODUCT), state_sig()
    drifts = sorted((ROOT / "reports").glob("ANCHOR-DRIFT-*.md"))

    ok = True
    for name, code, out in results:
        good = code == 2
        ok &= good
        first = next((l for l in out.splitlines() if l.startswith("[ANCHOR_DRIFT]")),
                     (out.splitlines() or [""])[0])[:120]
        print(f"{'✓' if good else '✗'} 演练「{name}」退出码 {code}（期望 2）| {first}")
    if not drifts:
        ok = False
        print("✗ 未生成 ANCHOR-DRIFT 修复工单")
    else:
        print(f"✓ 生成 {len(drifts)} 份漂移工单：{', '.join(d.name for d in drifts)}")

    same_sha = before_sha == after_sha
    same_state = before_state == after_state
    ok &= same_sha and same_state
    print(f"{'✓' if same_sha else '✗'} 产物未被改动：{after_sha[:16]}…")
    print(f"{'✓' if same_state else '✗'} state 未被推进：{after_state[0]} / {after_state[1][:12]}")

    print("\n漂移演练" + ("通过：阻断生效且线上产物安全" if ok else "失败：见上方 ✗ 项"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
