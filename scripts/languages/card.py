#!/usr/bin/env python3
"""Fast replacement for lowlighter/metrics' indepth "most used languages" card.

metrics checks out every commit and re-runs linguist over the whole repository
whenever a commit touches a path it has not classified yet, which explodes on
repositories with thousands of commits. This script computes the same numbers
(bytes you added per language across your non-merge commits) with one
`git log -p` and one linguist pass per repository, then renders the same card.

Configuration comes from environment variables, see main().
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
HASH = re.compile(rb"^[0-9a-f]{40}$")


def env_list(name: str, default: str = "", sep: str = ",") -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(sep) if item.strip()]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def git(*args: str, cwd: Path | None = None, stdin: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=cwd, input=stdin, check=True, capture_output=True,
        env={**os.environ, "LANG": "en_GB", "GIT_TERMINAL_PROMPT": "0"},
    ).stdout


# ---------------------------------------------------------------- repositories

QUERY = """
query($cursor: String) {
  viewer {
    login
    repositories(first: 100, after: $cursor, isFork: false,
                 ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER]) {
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner isEmpty languages(first: 100) { nodes { name } } }
    }
    repositoriesContributedTo(first: 100, includeUserRepositories: false,
                              contributionTypes: [COMMIT, PULL_REQUEST, REPOSITORY]) {
      nodes { languages(first: 100) { nodes { name } } }
    }
  }
}
"""


def graphql(token: str, query: str, variables: dict) -> dict:
    request = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "User-Agent": "languages-card"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload["data"]


def list_repositories(token: str) -> tuple[list[str], int]:
    repos, unique, cursor = [], set(), None
    while True:
        viewer = graphql(token, QUERY, {"cursor": cursor})["viewer"]
        page = viewer["repositories"]
        for node in page["nodes"]:
            unique.update(lang["name"] for lang in node["languages"]["nodes"])
            if not node["isEmpty"]:
                repos.append(node["nameWithOwner"])
        for node in viewer["repositoriesContributedTo"]["nodes"]:
            unique.update(lang["name"] for lang in node["languages"]["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return repos, len(unique)
        cursor = page["pageInfo"]["endCursor"]


def clone(args: tuple[str, str, Path]) -> Path | None:
    repo, token, workdir = args
    target = workdir / (repo.replace("/", "__") + ".git")
    url = f"https://x-access-token:{token}@github.com/{repo}.git" if token else f"https://github.com/{repo}.git"
    try:
        git("clone", "--quiet", "--bare", url, str(target))
        return target
    except subprocess.CalledProcessError as error:
        log(f"  ! failed to clone {repo}: {error.stderr.decode(errors='replace').strip()}")
        return None


# -------------------------------------------------------------------- analysis

def authored_commits(repo: Path, authors: list[str]) -> list[bytes]:
    """Same selection as metrics: --author or --grep match (case-insensitive), no merges."""
    seen: dict[bytes, None] = {}
    for author in authors:
        for flag in ("--author", "--grep"):
            out = git("log", f"{flag}={author}", "--regexp-ignore-case", "--no-merges", "--format=%H", cwd=repo)
            for line in out.splitlines():
                if HASH.match(line.strip()):
                    seen.setdefault(line.strip())
    return list(seen)


def parse_patches(repo: Path, commits: list[bytes]) -> tuple[list[dict[str, int]], dict[str, str]]:
    """Return, per commit, {path: added bytes} for every file added/modified in it,
    plus one commit per path where that file exists (used to classify deleted files)."""
    proc = subprocess.Popen(
        ["git", "-c", "core.quotePath=false", "log", "--no-walk=unsorted", "--stdin",
         "--patch", "--no-color", "--format=%x00%H"],
        cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env={**os.environ, "LANG": "en_GB"},
    )
    assert proc.stdin and proc.stdout
    proc.stdin.write(b"\n".join(commits) + b"\n")
    proc.stdin.close()
    result: list[dict[str, int]] = []
    present: dict[str, str] = {}
    current: dict[str, int] | None = None
    path: str | None = None
    sha = ""
    for raw in proc.stdout:
        line = raw.rstrip(b"\n")
        if line.startswith(b"\x00"):
            current = {}
            result.append(current)
            path = None
            sha = line[1:].decode()
        elif current is None:
            continue
        elif line.startswith(b"+++ "):
            path = line[6:].decode(errors="replace") if line.startswith(b"+++ b/") else None
            if path is not None:
                current.setdefault(path, 0)
                present.setdefault(path, sha)
        elif line.startswith(b"diff --git "):
            path = None
        elif path is not None and line.startswith(b"+") and len(line) > 1:
            content = line[1:].lstrip()  # metrics ignores leading indentation
            current[path] += len(content) or 1
    proc.wait()
    return result, present


def detect(repo: Path, present: dict[str, str], scratch: Path) -> tuple[dict[str, str], dict[str, dict]]:
    """Classify every path once: files at HEAD as they are now, deleted or renamed files
    with their content from a commit where they still existed."""
    head = scratch / "head"
    past = scratch / "past"
    head.mkdir()
    past.mkdir()
    archive = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=repo, check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(head)], input=archive, check=True)
    for name in (".gitattributes", ".gitignore"):
        if (head / name).exists():
            shutil.copy(head / name, past / name)
    gone = [(rel, sha) for rel, sha in present.items() if "\n" not in rel and not (head / rel).is_file()]
    if gone:
        blobs = subprocess.run(["git", "cat-file", "--batch"], cwd=repo, check=True, capture_output=True,
                               input="".join(f"{sha}:{rel}\n" for rel, sha in gone).encode()).stdout
        cursor = 0
        for rel, _ in gone:
            end = blobs.index(b"\n", cursor)
            header = blobs[cursor:end].split()
            cursor = end + 1
            if len(header) != 3 or header[1] != b"blob":
                continue
            size = int(header[2])
            content, cursor = blobs[cursor:cursor + size], cursor + size + 1
            target = past / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            except OSError:
                pass
    out = subprocess.run(["node", str(HERE / "detect.mjs"), str(head), str(past)],
                         check=True, capture_output=True).stdout
    data = json.loads(out)
    return data["files"], data["languages"]


def analyze(args: tuple[Path, list[str]]) -> dict:
    repo, authors = args
    start = time.time()
    commits = authored_commits(repo, authors)
    if not commits:
        return {"repo": repo.name, "commits": 0, "files": 0, "total": 0, "stats": {}, "languages": {}, "seconds": 0}
    editions, present = parse_patches(repo, commits)
    with tempfile.TemporaryDirectory() as scratch:
        files, languages = detect(repo, present, Path(scratch))
    stats: dict[str, int] = defaultdict(int)
    total = 0
    for commit in editions:
        for path, size in commit.items():
            total += size
            if path in files:
                stats[files[path]] += size
    return {
        "repo": repo.name, "commits": len(editions), "files": sum(len(c) for c in editions),
        "total": total, "stats": dict(stats), "languages": languages, "seconds": round(time.time() - start, 1),
    }


# ------------------------------------------------------------------- rendering

CODE_ICON = "M1.5 2.75a.25.25 0 01.25-.25h12.5a.25.25 0 01.25.25v8.5a.25.25 0 01-.25.25h-6.5a.75.75 0 00-.53.22L4.5 14.44v-2.19a.75.75 0 00-.75-.75h-2a.25.25 0 01-.25-.25v-8.5zM1.75 1A1.75 1.75 0 000 2.75v8.5C0 12.216.784 13 1.75 13H3v1.543a1.457 1.457 0 002.487 1.03L8.061 13h6.189A1.75 1.75 0 0016 11.25v-8.5A1.75 1.75 0 0014.25 1H1.75zm5.03 3.47a.75.75 0 010 1.06L5.31 7l1.47 1.47a.75.75 0 01-1.06 1.06l-2-2a.75.75 0 010-1.06l2-2a.75.75 0 011.06 0zm2.44 0a.75.75 0 000 1.06L10.69 7 9.22 8.47a.75.75 0 001.06 1.06l2-2a.75.75 0 000-1.06l-2-2a.75.75 0 00-1.06 0z"
STYLE = "svg{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif,Apple Color Emoji,Segoe UI Emoji;color:#777}h2,h3{margin:8px 0 2px;padding:0;color:#0366d6;font-weight:400}h2 svg{fill:currentColor}h2{font-size:16px}h3,svg{font-size:14px}section>.field{margin-left:5px;margin-right:5px}.field{display:flex;align-items:center;margin-bottom:2px;white-space:nowrap}.field svg{margin:0 8px;fill:#959da5;flex-shrink:0}.row{display:flex;flex-wrap:wrap}.row section{flex:1 1 0}.column{display:flex;flex-direction:column;align-items:center}.fill-width{width:100%}svg.bar{margin:4px 0}.field.language{margin:0 8px;flex-grow:0}.field.language.details,.field.language.details small{display:flex;justify-content:space-between}.field.language.details small{color:#666;text-align:right}.field.language.details small>*,.field.language.details>*{flex:1 1 0}"


def human_bytes(size: float) -> str:
    for unit in ("b", "kb", "mb", "gb"):
        if size < 1000 or unit == "gb":
            return f"{size:.1f}{unit}".replace(".0", "") if unit != "b" else f"{int(size)}b"
        size /= 1000
    return f"{size}b"


def percent(value: float) -> str:
    return f"{round(value * 100, 2):g}%"


def render(entries: list[tuple[str, float, str]], unique: int, total: int, files: int, commits: int) -> str:
    width = 480
    rows = math.ceil(len(entries) / 2)
    height = 103 + 20 * rows
    bar = []
    x = 0.0
    for name, value, color in entries:
        bar.append(f'<rect mask="url(#languages-bar)" x="{x}" y="0" width="{value * 460}" height="8" fill="{color}"/>')
        x += value * 460

    def item(name: str, value: float, color: str) -> str:
        return (
            '<div class="field language details"><div class="field">'
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16">'
            f'<path fill="{color}" fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8z"/></svg>'
            f'{html.escape(name)}</div><small><div>{percent(value)}</div></small></div>'
        )

    left = "".join(item(*e) for e in entries[:rows])
    right = "".join(item(*e) for e in entries[rows:])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
<style>{STYLE}</style>
<foreignObject x="0" y="0" width="100%" height="100%">
<div xmlns="http://www.w3.org/1999/xhtml">
<section><h2 class="field"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16"><path fill-rule="evenodd" d="{CODE_ICON}"/></svg>{unique} Languages</h2></section>
<section class="column">
<h3 class="field">Most used languages</h3>
<small>estimation from {human_bytes(total)} of code in {files} edited files across {commits} commits</small>
<svg class="bar" xmlns="http://www.w3.org/2000/svg" width="460" height="8"><mask id="languages-bar"><rect x="0" y="0" width="460" height="8" fill="white" rx="5"/></mask>{"".join(bar)}</svg>
<div class="row fill-width"><section>{left}</section><section>{right}</section></div>
</section>
</div>
</foreignObject>
</svg>
"""


# ------------------------------------------------------------------------ main

def main() -> None:
    token = os.environ.get("GH_TOKEN", "")
    authors = env_list("AUTHORS")
    skipped = {repo.lower() for repo in env_list("SKIP", sep=" ")}
    categories = set(env_list("CATEGORIES", "programming"))
    ignored = {name.lower() for name in env_list("IGNORED")}
    aliases = {k.strip().lower(): v.strip() for k, v in (a.split(":", 1) for a in env_list("ALIASES") if ":" in a)}
    limit = int(os.environ.get("LIMIT", "8"))
    output = Path(os.environ.get("OUTPUT", "metrics/languages.svg"))
    workdir = Path(os.environ.get("WORKDIR") or tempfile.mkdtemp(prefix="languages-"))
    workdir.mkdir(parents=True, exist_ok=True)

    start = time.time()
    repos, unique = list_repositories(token)
    repos = [repo for repo in repos if repo.lower() not in skipped]
    log(f"{len(repos)} repositories to analyze")

    with ThreadPoolExecutor(8) as pool:
        clones = [path for path in pool.map(clone, [(repo, token, workdir) for repo in repos]) if path]
    log(f"cloned {len(clones)} repositories in {time.time() - start:.0f}s")

    totals = {"commits": 0, "files": 0, "total": 0}
    stats: dict[str, int] = defaultdict(int)
    colors: dict[str, str] = {}
    with ProcessPoolExecutor(os.cpu_count() or 2) as pool:
        for result in pool.map(analyze, [(path, authors) for path in clones]):
            log(f"  {result['repo']}: {result['commits']} commits, {human_bytes(result['total'])} in {result['seconds']}s")
            for key in totals:
                totals[key] += result[key]
            for language, size in result["stats"].items():
                info = result["languages"].get(language, {})
                if info.get("type") in categories:
                    stats[language] += size
                    colors.setdefault(language, info.get("color") or "#ededed")

    merged: dict[str, int] = defaultdict(int)
    for language, size in stats.items():
        name = aliases.get(language.lower(), language)
        merged[name] += size
        colors.setdefault(name, colors[language])
    ranked = sorted(((n, s) for n, s in merged.items() if n.lower() not in ignored), key=lambda e: -e[1])[:limit]
    visible = sum(size for _, size in ranked) or 1
    entries = [(name, size / visible, colors.get(name, "#ededed")) for name, size in ranked]

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render(entries, unique, totals["total"], totals["files"], totals["commits"]))
    for name, value, _ in entries:
        log(f"  {name:<12} {percent(value)}")
    log(f"done in {time.time() - start:.0f}s -> {output}")


if __name__ == "__main__":
    main()
