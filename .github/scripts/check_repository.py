#!/usr/bin/env python3
"""Check the intentional distribution tree. Stdlib and Git, no private checkout."""
from pathlib import Path
import re
import subprocess

ALLOWED = {
    "README.md", "LICENSE", "docs/img/dspark-studio-logo.png",
    "docs/img/dspark-studio-brand.png", "docs/img/dspark-studio-gh.png",
    ".github/workflows/check.yml", ".github/workflows/verify-release.yml",
    ".github/scripts/distribution.py", ".github/scripts/test_distribution.py",
    ".github/scripts/check_repository.py",
}


def main():
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    files = set(filter(None, subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")))
    if files != ALLOWED:
        raise SystemExit("Unexpected distribution tree: " + repr(sorted(files ^ ALLOWED)))
    readme = (root / "README.md").read_text(encoding="utf-8")
    if "DAW for AI Agents" not in readme or "https://github.com/CristianMoresi/DSPark" not in readme:
        raise SystemExit("Product definition or DSPark attribution is missing")
    if "DSParkStudio-Development" in readme:
        raise SystemExit("Distribution README exposes a private repository reference")
    if not (root / "LICENSE").read_bytes().startswith(b"DSPark Studio License Agreement\n"):
        raise SystemExit("Studio license is missing")
    links = re.findall(r'\]\(([^)]+)\)|src="([^"]+)"', readme)
    for markdown, image in links:
        target = markdown or image
        if target.startswith(("https://", "http://", "mailto:", "#")):
            continue
        local = (root / target.split("#", 1)[0]).resolve()
        if root not in local.parents or not local.is_file():
            raise SystemExit("Broken or escaping local README link: " + target)
    print("PASS distribution-only tree, product identity, license and local README links")


if __name__ == "__main__":
    main()
