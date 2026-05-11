#!/usr/bin/env python3
"""Fetch the user's merged PRs on third-party public repos and write data/contributions.json."""

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USERNAME = os.environ.get("GH_USERNAME", "").strip()
TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
OUTPUT = Path(os.environ.get("OUTPUT_PATH", "data/contributions.json"))
README = Path(os.environ.get("README_PATH", "README.md"))
MIN_STARS = int(os.environ.get("MIN_STARS", "200"))

README_START = "<!-- CONTRIBUTIONS:START -->"
README_END = "<!-- CONTRIBUTIONS:END -->"

# Owners to exclude (yours/your orgs). Comma-separated env var.
EXCLUDE_OWNERS = [
    o.strip().lower()
    for o in os.environ.get("EXCLUDE_OWNERS", USERNAME).split(",")
    if o.strip()
]

API = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 10  # search API returns at most 1000 results


def gh(url: str) -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": f"{USERNAME or 'contrib'}-fetcher",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < 2:
                reset = int(e.headers.get("X-RateLimit-Reset", "0") or 0)
                wait = max(5, reset - int(time.time())) if reset else 30
                print(f"Rate-limited ({e.code}); sleeping {wait}s", file=sys.stderr)
                time.sleep(min(wait, 90))
                continue
            raise


def search_merged_prs() -> list[dict]:
    items: list[dict] = []
    exclude = " ".join(f"-user:{o}" for o in EXCLUDE_OWNERS)
    query = f"author:{USERNAME} is:pr is:merged is:public {exclude}".strip()
    for page in range(1, MAX_PAGES + 1):
        params = urllib.parse.urlencode(
            {"q": query, "per_page": PER_PAGE, "page": page, "sort": "updated", "order": "desc"}
        )
        data = gh(f"{API}/search/issues?{params}")
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < PER_PAGE:
            break
    return items


def repo_meta(full_name: str, cache: dict) -> dict:
    if full_name in cache:
        return cache[full_name]
    try:
        cache[full_name] = gh(f"{API}/repos/{full_name}")
    except urllib.error.HTTPError as e:
        print(f"warn: repo {full_name} fetch failed ({e.code})", file=sys.stderr)
        cache[full_name] = {}
    return cache[full_name]


def _fmt_stars(n: int) -> str:
    return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k" if n >= 1000 else str(n)


def _fmt_date(iso: str) -> str:
    if not iso:
        return ""
    try:
        return dt.datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%b %d, %Y")
    except ValueError:
        return iso[:10]


def render_markdown(contributions: list[dict], generated_at: str) -> str:
    if not contributions:
        return "_No merged contributions yet — check back soon._\n"

    grouped: dict[str, dict] = {}
    for c in contributions:
        repo = c["repo"]
        g = grouped.setdefault(
            repo,
            {
                "repo": repo,
                "repo_url": c["repo_url"],
                "repo_description": c.get("repo_description", ""),
                "stars": c.get("stars", 0),
                "language": c.get("language", ""),
                "prs": [],
            },
        )
        g["prs"].append(c)

    repos = list(grouped.values())
    for r in repos:
        r["prs"].sort(key=lambda p: p.get("merged_at") or "", reverse=True)
        r["latest"] = r["prs"][0].get("merged_at") or ""
    repos.sort(key=lambda r: r["latest"], reverse=True)

    total_prs = sum(len(r["prs"]) for r in repos)
    lines: list[str] = [
        f"**{total_prs} merged PR{'s' if total_prs != 1 else ''} across "
        f"{len(repos)} repo{'s' if len(repos) != 1 else ''}** · "
        f"last refreshed {_fmt_date(generated_at)}",
        "",
    ]

    for r in repos:
        pr_count = len(r["prs"])
        meta_bits = [f"⭐ {_fmt_stars(r['stars'])}"]
        if r.get("language"):
            meta_bits.append(r["language"])
        meta_bits.append(f"{pr_count} merged PR{'s' if pr_count != 1 else ''}")
        meta = " · ".join(meta_bits)

        lines.append(
            f"<details>\n  <summary><a href=\"{r['repo_url']}\"><b>{r['repo']}</b></a> "
            f"— <sub>{meta}</sub></summary>\n"
        )
        if r.get("repo_description"):
            lines.append(f"  > {r['repo_description']}\n")
        lines.append("")
        for pr in r["prs"]:
            lines.append(
                f"  - [#{pr['number']} {pr['title']}]({pr['url']}) "
                f"<sub>· {_fmt_date(pr.get('merged_at') or '')}</sub>"
            )
        lines.append("</details>\n")

    return "\n".join(lines)


def update_readme(markdown_block: str) -> bool:
    if not README.exists():
        print(f"warn: {README} not found; skipping README update", file=sys.stderr)
        return False

    text = README.read_text()
    if README_START not in text or README_END not in text:
        print(
            f"warn: {README} missing {README_START}/{README_END} markers; skipping",
            file=sys.stderr,
        )
        return False

    before, rest = text.split(README_START, 1)
    _, after = rest.split(README_END, 1)
    new_text = (
        f"{before}{README_START}\n"
        f"<!-- This section is auto-generated nightly by "
        f".github/workflows/update-contributions.yml — do not edit by hand. -->\n\n"
        f"{markdown_block.rstrip()}\n\n"
        f"{README_END}{after}"
    )
    if new_text == text:
        return False
    README.write_text(new_text)
    return True


def main() -> int:
    if not USERNAME:
        print("error: GH_USERNAME env var is required", file=sys.stderr)
        return 1

    print(f"Fetching merged PRs by {USERNAME} (excluding owners: {EXCLUDE_OWNERS})")
    prs = search_merged_prs()
    print(f"Found {len(prs)} merged PRs")

    cache: dict = {}
    contributions: list[dict] = []
    for pr in prs:
        full = pr["repository_url"].split("/repos/", 1)[1]
        repo = repo_meta(full, cache)
        if repo.get("private") or repo.get("fork"):
            continue
        stars = int(repo.get("stargazers_count") or 0)
        if stars < MIN_STARS:
            continue
        contributions.append(
            {
                "title": pr["title"],
                "url": pr["html_url"],
                "number": pr["number"],
                "repo": full,
                "repo_url": f"https://github.com/{full}",
                "repo_description": (repo.get("description") or "").strip(),
                "stars": stars,
                "language": repo.get("language") or "",
                "merged_at": (pr.get("pull_request") or {}).get("merged_at")
                or pr.get("closed_at"),
            }
        )

    contributions.sort(key=lambda c: c.get("merged_at") or "", reverse=True)

    payload = {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "username": USERNAME,
        "count": len(contributions),
        "contributions": contributions,
    }

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"Wrote {len(contributions)} contributions to {OUTPUT}")

    if update_readme(render_markdown(contributions, payload["generated_at"])):
        print(f"Updated {README}")
    else:
        print(f"No changes to {README}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
