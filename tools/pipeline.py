# -*- coding: utf-8 -*-
"""B2 主编排：获取上游 → blob sha 判据 → 补丁 → 门禁 → 原子发布 → 报告。

退出码约定（CI 与本地共用）：
  0  NO_CHANGE（无需重建）或 BUILD_OK
  2  ANCHOR_DRIFT（锚点失配，需人工更新 anchors.json）
  3  GATE_FAILED（补丁成功但门禁不过）
  4  FETCH_FAILED（上游拉取失败）

防静默三条铁律：
  1. 门禁跑的是 state/build/ 下的临时产物，全过才 os.replace 到 dist/；
  2. 任一步失败都不推进 state.json、不碰 manifest.json；
  3. 失败也一定写报告，绝不静默退出。
"""
from __future__ import annotations
import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_upstream as fu
import gate as gates_mod
import obfuscate_step
import patch as patch_mod
from drift_report import now, write_reports

EXIT_OK, EXIT_DRIFT, EXIT_GATE, EXIT_FETCH = 0, 2, 3, 4


def _read_json(p: Path) -> dict[str, Any]:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(p: Path, data: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def _repo_slug() -> str:
    return os.environ.get("GITHUB_REPOSITORY") or "zues0077/doujs"


def _public_base() -> str:
    return (os.environ.get("PUBLIC_BASE") or
            f"https://raw.githubusercontent.com/{_repo_slug()}/main").rstrip("/")


def _render_index_html(manifest: dict[str, Any]) -> str:
    """网页入口：让采集器把本仓库当 webRepo 采集（零 GitHub 配额）。

    公开分发场景下本页刻意中性：不出现用途词，避免被站外搜索命中。
    版本号单独成列 —— 猎手 extractLinkMeta 只从「不含文件名的单元格」取版本。
    """
    fn = Path(manifest["filename"]).name
    base = fn[:-3] if fn.endswith(".js") else fn
    ver = manifest["version"]
    day = manifest["built_at"][:10]
    url = manifest["download"]["release_latest"]
    return f"""<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{base}</title></head>
<body>
<h1>{base}</h1>
<p>version <code>{ver}</code> · updated {day} · patch rev {manifest['patch_rev']}</p>
<p>upstream blob <code>{manifest['upstream']['blob_sha'][:12]}</code></p>
<table border="1" cellpadding="6">
<tr><th>name</th><th>version</th><th>updated</th><th>file</th></tr>
<tr>
  <td>{base}</td>
  <td>{ver}</td>
  <td>{day}</td>
  <td><a href="{url}">{fn}</a></td>
</tr>
</table>
<p>sha256 <code>{manifest['product']['sha256']}</code> · {manifest['product']['bytes']} bytes</p>
</body>
</html>
"""


def build(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).resolve()
    contract = patch_mod.load_contract(root / "anchors.json")
    fixed_name = contract["product"]["fixed_name"]
    fixed_filename = contract["product"]["fixed_filename"]
    state_path = root / "state" / "state.json"
    state = _read_json(state_path)
    build_dir = root / "state" / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {
        "status": "RUNNING", "run_at": now(), "exit_code": 0,
        # 报告文件会入库并公开：只放 branch/path/blob，不放 owner/repo 与直达 URL，
        # 否则公开仓库等于把上游来源直接写进搜索结果里。
        "upstream": {"branch": os.environ.get("UPSTREAM_BRANCH", "master"),
                     "path": contract["upstream"]["path"],
                     "blob_sha": "", "blob_note": "", "head_sha": ""},
        "change_verdict": "", "last_good_version": state.get("full_version", "n/a"),
        "last_good_sha256": (state.get("product") or {}).get("sha256", "n/a"),
        "product": {"fixed_filename": fixed_filename},
        "applied": [], "drifts": [], "warnings": [], "scan_hits": [], "gates": [],
        "obf_gates": [], "obfuscation": {},
    }

    try:
        up = fu.from_local(args.upstream_file, contract) if args.upstream_file else fu.from_network(contract)
    except Exception as e:
        data.update(status="FETCH_FAILED", exit_code=EXIT_FETCH,
                    change_verdict=f"拉取失败：{e}")
        write_reports(root, data)
        print(f"[FETCH_FAILED] {e}")
        return EXIT_FETCH

    data["upstream"].update({k: up[k] for k in ("blob_sha", "head_sha", "blob_note")})

    last_blob = (state.get("upstream") or {}).get("blob_sha", "")
    if last_blob and last_blob == up["blob_sha"] and not args.force:
        data.update(status="NO_CHANGE", exit_code=EXIT_OK,
                    change_verdict=f"blob sha 未变（{up['blob_sha'][:12]}），跳过重建")
        out = write_reports(root, data)
        print(f"[NO_CHANGE] 上游脚本未变：{up['blob_sha'][:12]}；报告 {out['run_md']}")
        return EXIT_OK

    same_upstream = last_blob == up["blob_sha"]
    patch_rev = args.patch_rev or ((state.get("patch_rev", 0) + 1) if same_upstream else 1)
    res = patch_mod.apply_patches(up["text"], contract, patch_rev)
    data["full_version"] = res.full_version
    data["applied"] = res.applied
    data["warnings"] = res.warnings
    data["scan_hits"] = res.scan_hits
    data["upstream"]["version"] = res.upstream_version

    if not res.ok:
        data.update(status="ANCHOR_DRIFT", exit_code=EXIT_DRIFT,
                    change_verdict=f"上游 {up['blob_sha'][:12]} 与锚点契约不匹配",
                    drifts=[dataclasses.asdict(d) for d in res.drifts])
        out = write_reports(root, data)
        print(f"[ANCHOR_DRIFT] {len(res.drifts)} 项失配，已阻断；产物未改动")
        for d in res.drifts:
            print(f"  - {d.anchor_id} {d.title}: {d.reason[:160]}")
        print(f"  修复工单：{out.get('drift_md', out['run_md'])}")
        return EXIT_DRIFT

    tmp_product = build_dir / fixed_filename
    tmp_product.write_bytes(res.text.encode("utf-8"))

    gate_results = gates_mod.run_all(tmp_product, res.text, root, fixed_name,
                                     res.full_version, Path(args.smoke_test) if args.smoke_test else None)
    data["gates"] = gate_results
    if not all(g["ok"] for g in gate_results):
        data.update(status="GATE_FAILED", exit_code=EXIT_GATE,
                    change_verdict="补丁成功但门禁未通过",
                    product_sha256=hashlib.sha256(tmp_product.read_bytes()).hexdigest())
        out = write_reports(root, data)
        print("[GATE_FAILED] 门禁未通过，已阻断；产物未改动")
        for g in gate_results:
            print(f"  {'PASS' if g['ok'] else 'FAIL'} {g['name']}: {str(g['detail'])[:200]}")
        print(f"  报告：{out['run_md']}")
        return EXIT_GATE

    # ---------- 混淆阶段（全流程唯一不可逆步骤，前后各有一道门禁）----------
    # 顺序不能颠倒：明文的语义/协议/冒烟先验一遍，混淆之后再整套重验一遍。
    # 混淆产物一旦有行为差异，必须在这里被拦住 —— 它出库之后就再也回不到明文语义了。
    plain_bytes = tmp_product.read_bytes()
    plain_sha = hashlib.sha256(plain_bytes).hexdigest()
    if args.profile == "none":
        final_src = tmp_product
        data["obfuscation"] = {"profile": "none", "note": "已关闭混淆，直接分发明文产物"}
        data["obf_gates"] = []
    else:
        obf_product = build_dir / "obf" / fixed_filename
        obf_r = obfuscate_step.run(tmp_product, obf_product, args.profile, args.seed, root)
        data["obfuscation"] = {"profile": args.profile, "seed": args.seed,
                               "stats": obf_r.get("stats") or {}}
        data["obf_gates"] = []
        if not obf_r["ok"]:
            data.update(status="GATE_FAILED", exit_code=EXIT_GATE,
                        change_verdict=f"混淆阶段失败：{obf_r['error']}",
                        product_sha256=plain_sha)
            out = write_reports(root, data)
            print(f"[GATE_FAILED] 混淆失败：{obf_r['error']}；产物未改动")
            print(f"  报告：{out['run_md']}")
            return EXIT_GATE
        obf_gates = gates_mod.run_obf_checks(
            obf_product, root, fixed_name, res.full_version,
            Path(args.smoke_test) if args.smoke_test else None)
        data["obf_gates"] = obf_gates
        if not all(g["ok"] for g in obf_gates):
            data.update(status="GATE_FAILED", exit_code=EXIT_GATE,
                        change_verdict="混淆产物复验未通过（混淆改变了行为或协议形态）",
                        product_sha256=hashlib.sha256(obf_product.read_bytes()).hexdigest())
            out = write_reports(root, data)
            print("[GATE_FAILED] 混淆产物复验未通过，已阻断；产物未改动")
            for g in obf_gates:
                print(f"  {'PASS' if g['ok'] else 'FAIL'} {g['name']}: {str(g['detail'])[:200]}")
            print(f"  报告：{out['run_md']}")
            return EXIT_GATE
        final_src = obf_product
        print(f"[混淆] profile={args.profile} seed={args.seed} "
              f"{plain_bytes.__len__()}B → {obf_product.stat().st_size}B")

    raw_bytes = final_src.read_bytes()
    sha = hashlib.sha256(raw_bytes).hexdigest()
    if args.dry_run:
        data.update(status="BUILD_OK", exit_code=EXIT_OK, change_verdict="dry-run：门禁全过，未落盘",
                    product_sha256=sha)
        out = write_reports(root, data)
        print(f"[DRY-RUN OK] {res.full_version} sha256={sha[:16]}… 产物未替换")
        for g in gate_results:
            print(f"  {'PASS' if g['ok'] else 'FAIL'} {g['name']}: {str(g['detail'])[:200]}")
        print(f"  报告：{out['run_md']}")
        return EXIT_OK

    dist = root / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    final_product = dist / fixed_filename
    os.replace(final_src, final_product)

    manifest = {
        "schema": 2,
        "name": fixed_name,
        "version": res.full_version,
        "patch_rev": patch_rev,
        "built_at": now(),
        "filename": fixed_filename,
        "upstream": {"branch": os.environ.get("UPSTREAM_BRANCH", "master"),
                     "path": contract["upstream"]["path"], "blob_sha": up["blob_sha"],
                     "version": res.upstream_version, "head_sha": up["head_sha"]},
        "product": {"sha256": sha, "bytes": len(raw_bytes),
                    "obfuscated": args.profile != "none",
                    "plain_sha256": plain_sha, "plain_bytes": len(plain_bytes)},
        "obfuscation": data.get("obfuscation") or {},
        "download": {
            "release_latest": f"https://github.com/{_repo_slug()}/releases/latest/download/{fixed_filename}",
            "release_api": f"https://api.github.com/repos/{_repo_slug()}/releases/latest",
            "note": "产物只随 Release 分发，仓库树内不放 .js —— Release 附件不被 GitHub 代码搜索索引。",
        },
        "hunter": {
            "add_as_repo": _repo_slug(),
            "or_web_repo": f"https://raw.githubusercontent.com/{_repo_slug()}/main/docs/index.html",
            "suggested_filename": fixed_filename,
            "release_asset": fixed_filename,
            "note": "固定 @name 与文件名，勿改；猎手按 name 分组去重，改名会产生重复脚本。",
        },
        "anchors": [a["id"] for a in contract["anchors"]],
        "gates": [{"name": g["name"], "ok": g["ok"]} for g in gate_results],
        "obf_gates": [{"name": g["name"], "ok": g["ok"]} for g in (data.get("obf_gates") or [])],
    }
    _write_json(root / "manifest.json", manifest)
    _write_json(state_path, {
        "schema": 1,
        "upstream": manifest["upstream"],
        "patch_rev": patch_rev,
        "full_version": res.full_version,
        "product": manifest["product"],
        "built_at": manifest["built_at"],
        "anchors": manifest["anchors"],
    })
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "index.html").write_text(_render_index_html(manifest), encoding="utf-8", newline="\n")

    data.update(status="BUILD_OK", exit_code=EXIT_OK,
                change_verdict=f"上游 {up['blob_sha'][:12]} → 新产物 {res.full_version}",
                product_sha256=sha)
    out = write_reports(root, data)
    print(f"[BUILD_OK] {res.full_version}  sha256={sha[:16]}…  bytes={len(raw_bytes)}  "
          f"obf={args.profile}")
    for g in gate_results:
        print(f"  {'PASS' if g['ok'] else 'FAIL'} {g['name']}: {str(g['detail'])[:140]}")
    for g in (data.get("obf_gates") or []):
        print(f"  {'PASS' if g['ok'] else 'FAIL'} {g['name']}: {str(g['detail'])[:140]}")
    print(f"  已更新：{final_product} / manifest.json / state/state.json / docs/index.html")
    print(f"  报告：{out['run_md']}")
    return EXIT_OK


def main() -> None:
    ap = argparse.ArgumentParser(description="B2 上游脚本追新流水线")
    ap.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--upstream-file", default="", help="本地上游 lx-source.js（离线复现，跳过网络）")
    ap.add_argument("--smoke-test", default="", help="上游冒烟用例路径（tests/lx-source-smoke-test.js）")
    ap.add_argument("--patch-rev", type=int, default=0, help="强制补丁修订号")
    ap.add_argument("--force", action="store_true", help="blob sha 未变也重建")
    ap.add_argument("--dry-run", action="store_true", help="门禁通过也不落盘")
    ap.add_argument("--profile", default="medium", choices=["medium", "light", "none"],
                    help="混淆档位：medium=短名+字符串数组(base64) / light=仅短名 / none=不混淆")
    ap.add_argument("--seed", type=int, default=20261107,
                    help="混淆随机种子，固定才能保证同输入同输出（可 diff）")
    args = ap.parse_args()
    sys.exit(build(args))


if __name__ == "__main__":
    main()
