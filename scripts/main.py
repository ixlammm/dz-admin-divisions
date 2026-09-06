"""Main driver for the Algeria administrative divisions update pipeline.

Commands:
  detect    List available Geofabrik snapshots and already-released versions.
  extract   Extract a single local .pbf into JSON (+ changelog) without publishing.
  run       Full pipeline: detect new snapshots, download, extract, diff and release.

The `run` command is driven from GitHub Actions; the `extract` command is for local
testing (e.g. against test/algeria-260901.osm.pbf).

Environment:
  GITHUB_REPOSITORY  owner/repo (auto-detected from git or gh).
  GITHUB_TOKEN       used by `gh` to authenticate release/tag operations.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from extract import extract_pbf, write_json  # noqa: E402
from diff import compute_changelog, compute_summary, load_json  # noqa: E402

BASE = "https://download.geofabrik.de/africa"
INDEX_URL = f"{BASE}/algeria.html"
PBF_RE = re.compile(r"algeria-(\d{6})\.osm\.pbf")


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def parse_date(code: str) -> dt.date:
    yy, mm, dd = int(code[0:2]), int(code[2:4]), int(code[4:6])
    year = 2000 + yy if yy < 70 else 1900 + yy
    return dt.date(year, mm, dd)


def human_date(code: str) -> str:
    return parse_date(code).strftime("%Y-%m-%d")


def repo_name() -> str:
    env = os.environ.get("GITHUB_REPOSITORY")
    if env:
        return env
    proc = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True
    )
    url = proc.stdout.strip()
    if not url:
        return ""
    url = url.replace("git@github.com:", "").replace("https://github.com/", "")
    url = url.rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    return url


def fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "dz-admin-divisions-pipeline/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_text(url: str) -> str:
    return fetch(url).decode("utf-8", errors="replace")


def download(url: str, dest: Path, timeout: int = 3600) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(
        url, headers={"User-Agent": "dz-admin-divisions-pipeline/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        dl = 0
        total = int(resp.headers.get("Content-Length") or 0)
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            dl += len(chunk)
    tmp.replace(dest)


def md5_of(path: Path) -> str:
    import hashlib

    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


# --- gh wrappers ---

def gh(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["gh", *args]
    # `gh api` takes the repo in the endpoint URL and does not accept --repo.
    if repo and (not args or args[0] != "api"):
        cmd += ["--repo", repo]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"gh command failed (exit {proc.returncode}):\n  $ {' '.join(cmd)}\n{proc.stderr.strip()}"
        )
    return proc


def released_tags(repo: str) -> set[str]:
    try:
        proc = gh(repo, "api", "repos/" + repo + "/releases", "--paginate")
    except RuntimeError as exc:
        print(f"  [warn] could not query releases via gh ({exc}); assuming none", file=sys.stderr)
        return set()
    try:
        releases = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return set()
    tags: set[str] = set()
    for rel in releases:
        tag = rel.get("tag_name") or ""
        if tag.startswith("algeria-"):
            tags.add(tag)
    return tags


def available_dates() -> list[str]:
    html = fetch_text(INDEX_URL)
    dates = sorted({m for m in PBF_RE.findall(html)})
    return dates


def pbf_url(date: str) -> str:
    return f"{BASE}/algeria-{date}.osm.pbf"


# --- pipeline helpers ---

def build_document(data: dict[str, Any], version: str, source: str) -> dict[str, Any]:
    counts = {
        "provinces": len(data["provinces"]),
        "communes": len(data["communes"]),
    }
    return {
        "version": version,
        "date": human_date(version),
        "source": source,
        "generated_at": utcnow().isoformat(),
        "counts": counts,
        "provinces": data["provinces"],
        "communes": data["communes"],
    }


def write_artifacts(doc: dict[str, Any], workdir: Path, version: str) -> dict[str, Path]:
    combined = workdir / f"algeria-admin-divisions-{version}.json"
    provinces = workdir / f"provinces-{version}.json"
    communes = workdir / f"communes-{version}.json"
    changelog = workdir / f"changelog-{version}.md"

    write_json(doc, combined)
    write_json(
        {"version": doc["version"], "date": doc["date"],
         "generated_at": doc["generated_at"],
         "counts": {"provinces": doc["counts"]["provinces"]},
         "provinces": doc["provinces"]},
        provinces,
    )
    write_json(
        {"version": doc["version"], "date": doc["date"],
         "generated_at": doc["generated_at"],
         "counts": {"communes": doc["counts"]["communes"]},
         "communes": doc["communes"]},
        communes,
    )
    # changelog is written separately (needs previous doc)
    return {"combined": combined, "provinces": provinces, "communes": communes}


def fetch_previous_doc(
    prev_date: str | None, repo: str, store: Path, workdir: Path
) -> dict[str, Any] | None:
    if not prev_date:
        return None
    local = workdir / f"algeria-admin-divisions-{prev_date}.json"
    if local.exists():
        return load_json(local)
    # Try downloading from the previous release asset.
    asset = f"algeria-admin-divisions-{prev_date}.json"
    url = f"https://github.com/{repo}/releases/download/algeria-{prev_date}/{asset}"
    try:
        blob = fetch(url)
        doc = json.loads(blob)
        write_json(doc, workdir / asset)
        return doc
    except Exception as exc:  # noqa: BLE001
        print(f"  [warn] could not fetch previous doc {asset}: {exc}", file=sys.stderr)
        return None


# --- commands ---

def cmd_detect(args: argparse.Namespace) -> int:
    repo = args.repo or repo_name()
    avail = available_dates()
    released = released_tags(repo) if repo else set()
    pending = [d for d in avail if f"algeria-{d}" not in released]
    print(f"Available snapshots: {len(avail)}")
    if avail:
        print(f"  oldest {avail[0]} ({human_date(avail[0])})")
        print(f"  newest {avail[-1]} ({human_date(avail[-1])})")
    print(f"Already released: {len(released)}")
    print(f"Pending: {len(pending)}")
    for d in pending:
        print(f"  {d} ({human_date(d)})")
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir).resolve()
    outdir = Path(args.output).resolve()
    pbf = Path(args.input).resolve()

    data = extract_pbf(pbf, workdir, outdir, keep_intermediate=args.keep_intermediate)
    version = args.version or (pbf.stem.split("-")[-1] if re.match(r".*-(\d{6})$", pbf.stem) else "local")
    doc = build_document(data, version, pbf.name)
    write_json(doc, outdir / f"algeria-admin-divisions-{version}.json")
    write_json(
        {"version": doc["version"], "date": doc["date"], "generated_at": doc["generated_at"],
         "counts": {"provinces": doc["counts"]["provinces"]}, "provinces": doc["provinces"]},
        outdir / f"provinces-{version}.json",
    )
    write_json(
        {"version": doc["version"], "date": doc["date"], "generated_at": doc["generated_at"],
         "counts": {"communes": doc["counts"]["communes"]}, "communes": doc["communes"]},
        outdir / f"communes-{version}.json",
    )
    print(f"Extracted {doc['counts']['provinces']} provinces, "
          f"{doc['counts']['communes']} communes -> {outdir}")

    if args.compare_with:
        old = load_json(Path(args.compare_with).resolve())
        changelog = compute_changelog(old, doc, show_geometry=args.show_geometry)
    else:
        changelog = compute_changelog(None, doc, show_geometry=args.show_geometry)
    (outdir / f"changelog-{version}.md").write_text(changelog, encoding="utf-8")
    print(changelog)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    repo = args.repo or os.environ.get("GH_REPO") or repo_name()
    if not repo:
        print("error: could not determine the GitHub repository (set GITHUB_REPOSITORY or --repo).",
              file=sys.stderr)
        return 1

    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    store = workdir  # reuse workdir for artifact storage

    avail = available_dates()
    if not avail:
        print("error: no dated snapshots found on Geofabrik.", file=sys.stderr)
        return 1

    released = released_tags(repo)
    pending = [d for d in avail if f"algeria-{d}" not in released]
    if args.from_date:
        pending = [d for d in pending if d >= args.from_date]
    if args.limit and args.limit > 0:
        pending = pending[: args.limit]

    if not pending:
        print("No new snapshots to process; nothing to do.")
        return 0

    print(f"Processing {len(pending)} snapshot(s): {', '.join(pending)}")

    # Determine the base previous doc: the latest already-released date before `pending`.
    released_dates = {t.removeprefix("algeria-") for t in released}
    base_prev_date = None
    for d in avail:
        if d in released_dates and (not pending or d < pending[0]):
            base_prev_date = d
    prev = fetch_previous_doc(base_prev_date, repo, store, workdir)

    for i, version in enumerate(pending):
        print(f"\n=== {version} ({human_date(version)}) [{i + 1}/{len(pending)}] ===")
        tag = f"algeria-{version}"
        if tag in released_tags(repo) and not args.force:
            print(f"  already released; skipping.")
            continue

        pbf = workdir / f"algeria-{version}.osm.pbf"
        if not pbf.exists():
            url = pbf_url(version)
            print(f"  downloading {url}")
            download(url, pbf)
        else:
            print(f"  using cached {pbf.name}")

        # verify md5 when Geofabrik publishes one for this snapshot
        try:
            md5_blob = fetch_text(f"{BASE}/algeria-{version}.osm.pbf.md5")
            want = md5_blob.split()[0]
            have = md5_of(pbf)
            if want.lower() != have.lower():
                print(f"  [error] md5 mismatch for {pbf.name}: "
                      f"expect {want}, got {have}", file=sys.stderr)
                return 1
        except Exception:
            pass  # no md5 file published for older snapshots

        data = extract_pbf(pbf, workdir, workdir, keep_intermediate=args.keep_intermediate)
        doc = build_document(data, version, pbf_url(version))
        artifacts = write_artifacts(doc, workdir, version)

        changelog = compute_changelog(prev, doc, show_geometry=args.show_geometry)
        cl_path = workdir / f"changelog-{version}.md"
        cl_path.write_text(changelog, encoding="utf-8")

        print(f"  extracted {doc['counts']['provinces']} provinces / "
              f"{doc['counts']['communes']} communes")

        if args.dry_run:
            print(f"  [dry-run] skipping release for {tag}")
        else:
            title = f"Algeria Admin Divisions {human_date(version)}"
            # GitHub release bodies are limited to 125k chars; ship the full changelog
            # as an asset and only inline it if it is small enough.
            body_path = cl_path if len(changelog) <= 125000 else workdir / f"release-summary-{version}.md"
            if body_path != cl_path:
                body_path.write_text(compute_summary(prev, doc, show_geometry=args.show_geometry),
                                     encoding="utf-8")
            files = [
                str(artifacts["combined"]),
                str(artifacts["provinces"]),
                str(artifacts["communes"]),
                str(cl_path),
            ]
            cmd = ["release", "create", tag, "--title", title,
                   "--notes-file", str(body_path)]
            cmd += files
            print(f"  creating release: gh release create {tag} ({len(files)} assets)")
            gh(repo, *cmd)
            print(f"  released {tag}")

        prev = doc

        # Free disk space during a (potentially long) backfill.
        try:
            pbf.unlink()
        except OSError:
            pass

    print("\nDone.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Algeria admin divisions pipeline")
    parser.add_argument("--workdir", default=".", help="root working directory (default .)")
    parser.add_argument("--repo", default=None, help="owner/repo (override auto-detection)")
    parser.add_argument("--keep-intermediate", action="store_true")
    parser.add_argument("--show-geometry", action="store_true",
                        help="list geometry-only changes by name in the changelog")

    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("detect", help="list available vs released snapshots")
    d.set_defaults(func=cmd_detect)

    e = sub.add_parser("extract", help="extract a local .pbf into JSON")
    e.add_argument("--input", required=True)
    e.add_argument("--output", default="out")
    e.add_argument("--version", default=None)
    e.add_argument("--compare-with", default=None, help="previous JSON for a fake changelog")
    e.add_argument("--show-geometry", action="store_true")
    e.set_defaults(func=cmd_extract)

    r = sub.add_parser("run", help="full pipeline (detect/download/extract/diff/release)")
    r.add_argument("--from-date", default=None, help="only process dates >= YYMMDD")
    r.add_argument("--limit", type=int, default=0, help="max snapshots to process (0=all)")
    r.add_argument("--force", action="store_true", help="re-create even if tag exists")
    r.add_argument("--dry-run", action="store_true", help="do everything except publishing")
    r.set_defaults(func=cmd_run)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
