# -*- coding: utf-8 -*-
"""混淆步骤：把明文产物转成分发用的混淆版，并返回可审计的统计。

为什么单独成模块而不塞进 pipeline：
  混淆是本流水线里唯一「不可逆」的一步 —— 产物进去就再也回不到明文语义，
  所以它必须是一个边界清晰的阶段，前后各自有独立门禁（明文门禁 → 混淆 → 混淆门禁）。

设计约束见 tools/obfuscate.cjs 顶部注释，核心三条：
  1. `_0x` 密度必须 < 20/KB，否则 collector 判 high 会在灌入前剔除；
  2. 不能引入空转死循环 / debugger / 沙箱逃逸，否则 host 判 A 级直接隔离；
  3. banner 必须保留，否则 extractMeta 解析不到 @name/@version。
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

NODE = "node"


def run(plain: Path, out: Path, profile: str = "medium", seed: int = 20261107,
        root: Path | None = None) -> dict[str, Any]:
    """对 plain 做混淆，写出 out。返回 {ok, stats, error}。"""
    root = Path(root or Path(__file__).resolve().parent.parent)
    tool = root / "tools" / "obfuscate.cjs"
    if not tool.is_file():
        return {"ok": False, "stats": {}, "error": f"缺少 {tool}"}
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [NODE, str(tool), str(plain), str(out), "--profile", profile,
           "--seed", str(seed), "--json"]
    try:
        p = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                           timeout=600, encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "stats": {}, "error": f"调用混淆器失败：{e}"}
    if p.returncode != 0:
        return {"ok": False, "stats": {}, "error": (p.stderr or p.stdout).strip()[:600]}
    # obfuscate.cjs 的 --json 是 indent=2 的多行输出，必须整段解析；
    # 早先按「取最后一行」解析会拿到单个 '}' 而误报「输出不可解析」。
    try:
        stats = json.loads(p.stdout)
    except Exception:
        try:
            i, j = p.stdout.index("{"), p.stdout.rindex("}")
            stats = json.loads(p.stdout[i:j + 1])
        except Exception:
            return {"ok": False, "stats": {},
                    "error": f"混淆器输出不可解析：{p.stdout[:300]}"}

    # 绝对路径不进 manifest（manifest 会入库，路径里可能带本机工作区名）
    stats.pop("output", None)

    # 自检：三条硬约束不满足就在这里拦下，不等到后面门禁
    problems = []
    if stats.get("max_line_length", 0) > 5000:
        problems.append(f"最长行 {stats['max_line_length']} > 5000")
    if not stats.get("banner_preserved"):
        problems.append("banner 未保留")
    hexd = (stats.get("hex") or {}).get("perKb", 0)
    if hexd >= 20:
        problems.append(f"_0x 密度 {hexd}/KB ≥ 20（猎手会判 high 并剔除）")
    return {"ok": not problems, "stats": stats, "error": "；".join(problems)}


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python tools/obfuscate_step.py <输入.js> <输出.js> [profile]")
        sys.exit(2)
    r = run(Path(sys.argv[1]), Path(sys.argv[2]),
            sys.argv[3] if len(sys.argv) > 3 else "medium")
    print(json.dumps(r, ensure_ascii=False, indent=2))
    sys.exit(0 if r["ok"] else 4)
