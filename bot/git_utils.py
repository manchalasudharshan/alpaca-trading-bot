"""
bot/git_utils.py

Shared "commit and push these specific files" helper, factored out of
bot/auto_tune.py so bot/tuner_agent.py can reuse the exact same
pull-rebase -> commit -> push -> retry-once-on-conflict pattern that
scripts/vps_tick.sh and bot/auto_tune.py already use, instead of a second
copy drifting out of sync with it.
"""

import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import List

logger = logging.getLogger(__name__)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def commit_and_push_files(paths: List[str], commit_message: str) -> bool:
    """
    Stages exactly `paths` (relative to the repo root), and if that produces
    a real diff, commits with `commit_message` and pushes to main -- pulling
    with --rebase first, and retrying the push once after another
    pull --rebase if the first push is rejected (e.g. a concurrent VPS tick
    commit landed first). Mirrors scripts/vps_tick.sh's own git dance.

    Returns True if a commit was made (pushed or not -- a failed push still
    leaves the commit local; the caller's logs will show which), False if
    there was nothing to commit.
    """
    def run(cmd):
        return subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)

    run(["git", "pull", "--rebase", "origin", "main", "--quiet"])
    run(["git", "add", "-f", *paths])

    staged_diff = run(["git", "diff", "--cached", "--quiet"])
    if staged_diff.returncode == 0:
        logger.info("No git changes to commit for %s.", paths)
        return False

    commit = run(["git", "commit", "-m", commit_message, "--quiet"])
    if commit.returncode != 0:
        logger.error("git commit failed: %s", commit.stderr.strip())
        return False

    push = run(["git", "push", "origin", "HEAD:main", "--quiet"])
    if push.returncode != 0:
        logger.warning("git push failed, retrying once after pull --rebase: %s", push.stderr.strip())
        run(["git", "pull", "--rebase", "origin", "main", "--quiet"])
        push2 = run(["git", "push", "origin", "HEAD:main", "--quiet"])
        if push2.returncode != 0:
            logger.error("git push failed again, giving up: %s", push2.stderr.strip())
        else:
            logger.info("git push succeeded on retry.")
    else:
        logger.info("Committed and pushed %s.", paths)

    return True


def timestamped_commit_message(prefix: str) -> str:
    return f"{prefix} {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} [skip ci]"
