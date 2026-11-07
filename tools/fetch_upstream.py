# -*- coding: utf-8 -*-
"""上游获取：本地文件优先，网络走 curl（本机 python urllib 走代理取 GitHub 会返回空 body，见 P-1166）。"""
from __future__ import annotations
import json
import os
import subprocess
from pathlib import Path
from typing import Any


def _curl(args: list[str], timeout: int = 60) -> tuple[int, bytes]:
    p = subprocess.run(["curl", "-sS", "-L", "--max-time", str(timeout)] + args,
                       capture_output=True, timeout=timeout + 15)
    return p.returncode, p.stdout


def _token() -> str:
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if t:
        return t
    cred = Path.home() / ".workbuddy" / "git-credentials"
    if cred.is_file():
        for line in cred.read_text(encoding="utf-8", errors="replace").splitlines():
            m = line.strip()
            if "@github.com" in m and "://" in m:
                head, _, _ = m.rpartition("@")
                return head.split("//", 1)[-1].split(":", 1)[-1]
    return ""


def _subst(s: str) -> str:
    """把契约里的 {UPSTREAM_REPO} / {UPSTREAM_BRANCH} 换成环境变量。

    仓库是公开的，owner/repo 写进文件就会被全局代码搜索索引到，
    从而可以由上游名反查出本仓库。所以上游标识一律外部注入：
    CI 走仓库 Variables，本地走 shell 变量。
    """
    return (str(s)
            .replace("{UPSTREAM_REPO}", os.environ.get("UPSTREAM_REPO", ""))
            .replace("{UPSTREAM_BRANCH}", os.environ.get("UPSTREAM_BRANCH", "master")))


def resolve_upstream(contract: dict[str, Any]) -> dict[str, Any]:
    up = contract.get("upstream") or {}
    return {k: (_subst(v) if isinstance(v, str) else v) for k, v in up.items()}


def git_blob_sha(b: bytes) -> str:
    import hashlib
    return hashlib.sha1(b"blob " + str(len(b)).encode() + b"\0" + b).hexdigest()


def from_local(path: str | Path, contract: dict[str, Any]) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8")
    return {
        "text": text,
        "blob_sha": git_blob_sha(raw),
        "size": len(raw),
        "head_sha": "",
        "source": f"local:{Path(path).name}",
        "raw_url": resolve_upstream(contract).get("raw_url", ""),
        "compare_url": "",
        "blob_note": "由本地文件计算，等价于 GitHub blob sha",
    }


def from_network(contract: dict[str, Any]) -> dict[str, Any]:
    up = resolve_upstream(contract)
    if not up.get("repo") or "{" in str(up.get("repo")):
        raise RuntimeError(
            "未设置 UPSTREAM_REPO 环境变量。公开仓库不写死上游标识，"
            "CI 请在仓库 Settings → Secrets and variables → Actions → Variables "
            "配置 UPSTREAM_REPO（形如 owner/repo）与 UPSTREAM_BRANCH。"
            "本地离线构建请改用 --upstream-file。")
    token = _token()
    headers = ["-H", "Accept: application/vnd.github+json", "-H", "User-Agent: b2-compat-ci"]
    if token:
        headers += ["-H", f"Authorization: Bearer {token}"]

    code, body = _curl(headers + [up["contents_api"]], 45)
    if code != 0 or not body:
        raise RuntimeError(f"contents API 请求失败（curl exit={code}）")
    try:
        meta = json.loads(body)
    except Exception as e:
        raise RuntimeError(f"contents API 返回非 JSON: {body[:200]!r}") from e
    if isinstance(meta, dict) and meta.get("message"):
        raise RuntimeError(f"contents API 报错: {meta['message']}")

    code, head_body = _curl(headers + [f"https://api.github.com/repos/{up['repo']}/git/ref/heads/{up['branch']}"], 30)
    head_sha = ""
    if code == 0 and head_body:
        try:
            head_sha = json.loads(head_body).get("object", {}).get("sha", "")
        except Exception:
            pass

    code, raw = _curl(headers + [up["raw_url"]], 60)
    if code != 0 or not raw:
        raise RuntimeError(f"raw 下载失败（curl exit={code}）")

    blob = meta.get("sha") or git_blob_sha(raw)
    compare = ""
    if head_sha:
        compare = f"https://github.com/{up['repo']}/commits/{up['branch']}/{up['path']}"
    return {
        "text": raw.decode("utf-8"),
        "blob_sha": blob,
        "size": len(raw),
        "head_sha": head_sha,
        "source": "network",
        "raw_url": up["raw_url"],
        "compare_url": compare,
        "blob_note": "GitHub contents API 的 sha（即 blob sha）",
    }
