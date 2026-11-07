#!/usr/bin/env python3
"""月度心跳：每月最多写一次 state/heartbeat.json，保证仓库持续有提交活动。

背景
----
GitHub 对 public 仓库有一条规则：连续 60 天没有仓库活动时，会自动禁用
schedule 触发的 workflow。本流水线的定时追更一旦被禁用，就是**静默停更**
（上游更新了也不会有人告诉你）。上游若长期不动脚本，这个空窗会被轻易跨过。

本脚本把"最近一次活动"的间隔压到 31 天以内：每月第一次运行时写一次时间戳，
当月后续运行一律跳过。不依赖流水线是否构建成功，因此漂移失败时也照样生效。

用法
----
    python tools/heartbeat.py [--dry-run]

退出码
------
    0  正常（写入与跳过都算正常）
    1  内部错误

末行输出 ``RESULT=WRITE`` 或 ``RESULT=SKIP``，供 workflow 判断是否提交。
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

HEARTBEAT = os.path.join("state", "heartbeat.json")


def read_month(path: str = HEARTBEAT) -> str:
    """读已记录的月份；文件缺失或损坏一律当作"没有记录"。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        return ""
    month = data.get("month")
    return month if isinstance(month, str) else ""


def main() -> int:
    ap = argparse.ArgumentParser(description="月度心跳提交")
    ap.add_argument("--dry-run", action="store_true", help="只判断是否需要写，不落盘")
    # 测试用：注入固定时刻，用来验证跨月行为
    ap.add_argument("--now", default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.now:
        try:
            now = datetime.datetime.fromisoformat(args.now)
        except ValueError:
            print(f"`--now` 不是合法时间：{args.now}", file=sys.stderr)
            return 1
        if now.tzinfo is None:
            now = now.replace(tzinfo=datetime.timezone.utc)
    else:
        now = datetime.datetime.now(datetime.timezone.utc)

    month = now.strftime("%Y-%m")
    current = read_month()

    if current == month:
        print(f"本月（{month}）已有心跳记录，跳过")
        print("RESULT=SKIP")
        return 0

    if args.dry_run:
        print(f"[dry-run] 将写入 {HEARTBEAT}：{current or '(无)'} -> {month}")
        print("RESULT=WRITE")
        return 0

    os.makedirs(os.path.dirname(HEARTBEAT) or ".", exist_ok=True)
    payload = {
        "month": month,
        "updated": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    }
    with open(HEARTBEAT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"写入 {HEARTBEAT}：{current or '(无)'} -> {month}")
    print("RESULT=WRITE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
