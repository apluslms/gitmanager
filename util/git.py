from logging import Logger, getLogger
from pathlib import Path
import os
import subprocess
from typing import List, Optional, Tuple

from django.conf import settings

from util.files import rm_path
from util.typing import PathLike


default_logger = getLogger("util.git")

# Copy the environment for use in git calls. In particular, the HOME variable is needed to find the .gitconfig file
# in case it contains something necessary (like safe.directories)
git_env = os.environ.copy()
git_env["GIT_SSH_COMMAND"] = f"ssh -i {settings.SSH_KEY_PATH}"


def git_call(path: str, command: str, cmd: List[str], include_cmd_string: bool = True) -> Tuple[bool, str]:
    global git_env

    if include_cmd_string:
        cmd_str = " ".join(["git", *cmd]) + "\n"
    else:
        cmd_str = ""

    response = subprocess.run(["git", "-C", path, *settings.GIT_OPTIONS] + cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding='utf-8', env=git_env)
    if response.returncode != 0:
        return False, f"{cmd_str}Git {command}: returncode: {response.returncode}\nstdout: {response.stdout}\n"

    return True, cmd_str + response.stdout


def clone(path: str, remote_url: str, branch: str, *, logger: Logger = default_logger) -> bool:
    Path(path).mkdir(parents=True, exist_ok=True)

    success, logstr = git_call(".", "clone", ["clone", "-b", branch, "--recursive", remote_url, path])
    logger.info(logstr)
    return success


def checkout(path: str, remote_url: str, branch: str, *, logger: Logger = default_logger) -> bool:
    success = True
    # set the path beforehand, and handle logging
    def git(command: str, cmd: List[str]):
        nonlocal success
        if not success: # dont run the other commands if one fails
            return
        success, output = git_call(path, command, cmd)
        logger.info(output)

    git("fetch", ["fetch", "origin", branch])
    git("reset", ["reset", "-q", "--hard", f"origin/{branch}"])
    git("submodule sync", ["submodule", "sync", "--recursive"])
    git("submodule reset", ["submodule", "foreach", "--recursive", "git", "reset", "-q", "--hard"])
    git("submodule update", ["submodule", "update", "--init", "--recursive"])

    return success


def clean(path: str, origin: str, branch: str, exclude_patterns: List[str] = [], *, logger: Logger = default_logger) -> bool:
    success = True
    # set the path beforehand, and handle logging
    def git(command: str, cmd: List[str]):
        nonlocal success
        if not success: # dont run the other commands if one fails
            return
        success, output = git_call(path, command, cmd)
        logger.info(output)

    git("clean", ["clean", "-xfd"] + [e for f in exclude_patterns for e in ["-e", f]])
    git("submodule clean", ["submodule", "foreach", "--recursive", "git", "clean", "-xfd"])

    return success


def has_remote_url(path: str, remote_url: str) -> bool:
    success, origin_url = git_call(path, "remote", ["remote", "get-url", "origin"], include_cmd_string=False)
    return remote_url == origin_url.strip()


def repo_exists_at(path: PathLike) -> bool:
    success, true_or_error = git_call(os.fspath(path), "rev-parse", ["rev-parse", "--is-inside-work-tree"], include_cmd_string = False)
    return success and true_or_error.strip() == "true"


def clone_if_doesnt_exist(path: str, remote_url: str, branch: str, *, logger: Logger = default_logger) -> Optional[bool]:
    """
    Clones a repo to <path> if it hasnt been already.

    Returns None if the repo already exists, otherwise returns whether the clone was successful.
    """
    success = False
    if repo_exists_at(path):
        if has_remote_url(path, remote_url):
            return None

        logger.info("Wrong origin in repo, recloning\n\n")

    rm_path(path)
    success = clone(path, remote_url, branch, logger=logger)

    return success and (Path(path) / ".git").exists()


def get_diff_names(path: PathLike, sha1: str, sha2: Optional[str] = None) -> Tuple[Optional[str], Optional[List[str]]]:
    """Gets the changed files between commits <sha1> and <sha2> (or HEAD if None). Returns (error, files)-tuple, where either error or files is None"""
    if sha2 is None:
        sha2 = "HEAD"
    success, files_or_error = git_call(os.fspath(path), "diff", ["diff", "--name-only", sha1, sha2], include_cmd_string = False)
    if success:
        return None, [f for f in files_or_error.split("\n") if f]
    else:
        return files_or_error, None


def _get_commit_hash(path: PathLike) -> Tuple[bool, str]:
    """Returns (success, hash_or_error) where the hash has a newline at the end"""
    return git_call(os.fspath(path), "rev-parse", ["rev-parse", "HEAD"], include_cmd_string = False)


def get_commit_hash_or_none(path: PathLike) -> Optional[str]:
    success, hash_or_error = _get_commit_hash(path)
    return hash_or_error.strip() if success else None


def get_commit_hash(path: PathLike) -> str:
    success, hash_or_error = _get_commit_hash(path)
    if success:
        return hash_or_error.strip()
    else:
        raise RuntimeError(hash_or_error)


def get_commit_metadata(path: PathLike) -> Tuple[bool, str]:
    return git_call(os.fspath(path), "log", ["--no-pager", "log", '--pretty=format:------------\nCommit metadata\n\nHash:\n%H\nSubject:\n%s\nBody:\n%b\nCommitter:\n%ai\n%ae\nAuthor:\n%ci\n%cn\n%ce\n------------\n', "-1"], include_cmd_string=False)
