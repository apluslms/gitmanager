'''
Utility functions for exercise files.

'''
from contextlib import ExitStack
import fcntl
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import time
from types import TracebackType
from typing import Dict, Generator, Iterable, List, Optional, Set, Tuple, Type, Union

from django.conf import settings
from django.http.response import FileResponse as DjangoFileResponse, HttpResponse
from huey.contrib.djhuey import task

from util.typing import PathLike


def read_meta(file_path: PathLike) -> Dict[str,str]:
    '''
    Reads a meta file comprised of lines in format: key = value.

    @type file_path: C{str}
    @param file_path: a path to meta file
    @rtype: C{dict}
    @return: meta keys and values
    '''
    meta: Dict[str,str] = {}
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            for key,val in [l.split('=') for l in f.readlines() if '=' in l]:
                meta[key.strip()] = val.strip()
    return meta


def rm_path(path: Union[str, Path]) -> None:
    path = Path(path)
    if path.is_symlink():
        path.unlink()
    elif not path.exists():
        return
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def rm_paths(paths: Iterable[Union[str, Path]]) -> None:
    for path in paths:
        if path is not None:
            rm_path(path)


def rm_except(dir: PathLike, exclude: PathLike) -> None:
    """Remove directory contents except for <exclude>"""
    def inner(dir: PathLike, exclude: PathLike, exclude_parents: Set[str]):
        with os.scandir(dir) as it:
            for direntry in it:
                if exclude == direntry.path:
                    continue
                elif direntry.path in exclude_parents:
                    inner(direntry.path, exclude, exclude_parents)
                elif direntry.is_symlink():
                    Path(direntry.path).unlink()
                elif direntry.is_dir():
                    shutil.rmtree(direntry.path)
                else:
                    Path(direntry.path).unlink()

    if not os.path.exists(dir):
        return

    exclude_parents = {str(p) for p in Path(exclude).parents}
    inner(dir, exclude, exclude_parents)


@task(retries=2, retry_delay=3)
def rm_paths_async(paths: List[Union[str, Path]]) -> None:
    """Removes paths asynchronously."""
    rm_paths(paths)


@task()
def copys_async(
        pairs: List[Tuple[PathLike, PathLike]],
        *,
        read_lock_path: Optional[PathLike] = None,
        write_lock_path: Optional[PathLike] = None,
        ) -> None:
    """Copies a list of files and directories asynchronously.

    Note that the copying might fail, and the caller wont know about it
    due to the asynchronousity"""
    with ExitStack() as stack:
        if write_lock_path is not None:
            stack.enter_context(FileLock(write_lock_path, write=True))
        if read_lock_path is not None:
            stack.enter_context(FileLock(read_lock_path))
        for src, dst in pairs:
            if os.path.isdir(src):
                copytree(src, dst)
            else:
                copyfile(src, dst)


def rsync(src: PathLike, dst: PathLike) -> int:
    """
    Uses rsync command to copy a directory tree for speed and to preserve hard- and symlinks.
    """
    src, dst = os.fspath(src), os.fspath(dst)
    if not os.path.isdir(src):
        raise NotADirectoryError(f"'{src}' is not a directory")

    if src[-1] != "/":
        src = src + "/"
    if dst[-1] != "/":
        dst = dst + "/"

    process = subprocess.run(
        ["rsync", "-crlH", "--delete", "--out-format", "%n", os.fspath(src), os.fspath(dst)],
        #["rsync", "-trlH", "--delete-excluded", "--include-from", "-", "--exclude", "**", "--out-format", "%n", os.fspath(src), os.fspath(dst)],
        #input="\n".join("/" + f for f in files),
        #text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8"
    )
    if process.returncode != 0:
        raise RuntimeError(f"Failed to copy built course files: {process.stdout}")

    return process.stdout.count("\n")


def copyfile(src: PathLike, dst: PathLike) -> None:
    shutil.copyfile(src, dst)
    shutil.copystat(src, dst)


def copytree(src: PathLike, dst: PathLike) -> None:
    """
    Uses cp command to copy a directory tree in order to preserve hard- and symlinks.
    """
    process = subprocess.run(
        ["cp", "-a", os.fspath(src), os.fspath(dst)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8"
    )
    if process.returncode != 0:
        raise RuntimeError(f"Failed to copy built course files: {process.stdout}")


def is_subpath(child: PathLike, parent: Optional[PathLike] = None) -> bool:
    """
    If parent is not None, returns whether child is a subpath of (contained in)
    parent.
    If parent is None, check that child is a relative subpath i.e.
    is_subpath(Path(<location>, child), <location>) is True for every <location>.
    """
    child = os.path.normpath(child)

    if parent is None:
        return not os.path.isabs(child) and not child.startswith("../")

    parent = os.path.normpath(parent)

    if child == parent:
        return True

    return len(child) > len(parent) and child[len(parent)] == "/" and child.startswith(parent)


def file_mappings(root: Path, mappings_in: Iterable[Tuple[str,str]]) -> Generator[Tuple[str, Path], None, None]:
    """
    Resolves (name, path) tuples into (name, file) tuples.
    E.g. if path is a folder, we get an entry for each file in it.

    Raises ValueError if a name has multiple different files.
    """
    mappings = sorted((name, root / path) for name, path in mappings_in)

    def in_course_dir_check(path: Path):
        nonlocal root
        try:
            path.resolve().relative_to(root)
        except:
            raise ValueError(f"{path} links outside the course directory")

    def expand_dir(name: str, path: Path) -> Generator[Tuple[str,Path], None, None]:
        for child in path.iterdir():
            child_name = name / child.relative_to(path)
            yield str(child_name), child

    def expand_full(name: str, path: Path) -> Generator[Tuple[str,Path], None, None]:
        if path.is_file():
            in_course_dir_check(path)
            yield name, path
        elif path.is_dir():
            for root, _, files in os.walk(path, followlinks=True):
                root = Path(root)
                rootname = name / root.relative_to(path)
                for file in files:
                    in_course_dir_check(root / file)
                    yield str(rootname / file), root / file

    while mappings:
        while len(mappings) > 1 and is_subpath(mappings[1][0], mappings[0][0]):
            map = mappings.pop(0)
            if map[1].is_file():
                if map[0] != mappings[0][0]:
                    raise ValueError(f"{map[0]} is mapped to a file ({map[1]}) but {mappings[0][0]} is under it")
                elif map[1] != mappings[0][1]:
                    raise ValueError(f"{map[0]} is mapped to a file {map[1]} and the path {mappings[0][1]}")
            elif map[1].is_dir():
                mappings.extend(expand_dir(*map))
                mappings.sort()


        if not is_subpath(str(mappings[0][1]), str(root)):
            raise ValueError(f"{mappings[0][0]} is mapped to a file ({mappings[0][1]}) outside the root ({root})")
        elif os.path.isabs(mappings[0][0]):
            raise ValueError(f"tar filename {mappings[0][0]} is absolute")

        yield from expand_full(*mappings.pop(0))


def _tmp_path(path) -> str:
    """
    returns a path to a temporary file/directory (same as <path>) in the same
    place as <path> with the name prefixed with <path>s name.
    """
    dir, name = os.path.split(path)
    if os.path.isdir(path):
        tmp = tempfile.mkdtemp(prefix=name, dir=dir)
    else:
        fd, tmp = tempfile.mkstemp(prefix=name, dir=dir)
        os.close(fd)
    return tmp


def rename(src: PathLike, dst: PathLike, keep_tmp=False) -> Optional[str]:
    """
    renames a file or directory while making sure that the destination will only be removed if successful.
    returns None if keep_tmp = False. Otherwise, returns a tmp path to dst (None if dst does not exist).
    """
    tmpdst = None

    src, dst = os.fspath(src), os.fspath(dst)
    if not os.path.exists(dst) or (os.path.isfile(dst) and os.path.isfile(src)):
        if keep_tmp and os.path.exists(dst):
            tmpdst = _tmp_path(dst)
            os.rename(dst, tmpdst)

        try:
            os.rename(src, dst)
        except:
            if tmpdst:
                os.rename(tmpdst, dst)
            raise
    else:
        tmpdst = _tmp_path(dst)
        try:
            os.rename(dst, tmpdst)
            os.rename(src, dst)
        except:
            os.rename(tmpdst, dst)
            raise
        else:
            if not keep_tmp:
                rm_path(tmpdst)

    return tmpdst


def renames(pairs: Iterable[Tuple[PathLike, PathLike]]) -> None:
    """
    Renames multiple files and directories while making sure that either all or none succeed.
    Removes temporary files asynchronously using Huey.
    """
    done = set()
    try:
        for src, dst in pairs:
            tmpdst = rename(src, dst, True)
            done.add((src, dst, tmpdst))
    except:
        for src, dst, tmp in done:
            rename(dst, src)
            if os.path.exists(tmp):
                rename(tmp, dst)
        raise
    else:
        rm_paths_async([tmp for _, _, tmp in done if tmp is not None])


def readfile(path: PathLike) -> str:
    with open(path) as file:
        return file.read()


def _try_lockf(lockfile, flags) -> Optional[OSError]:
    try:
        fcntl.lockf(lockfile, flags)
    except OSError as e:
        return e
    return None

class FileLock:
    """
    A context manager for acquiring a file lock on a file or directory.
    __enter__ may raise OSError for non-blocking access on a locked file
    or TimeoutError if obtaining a lock takes too long.
    """
    def __init__(self, path: PathLike, write: bool = False, timeout: Optional[int] = None):
        self.path = os.fspath(path) + ".lock"
        self.timeout = timeout
        self.lock_flag = fcntl.LOCK_EX if write else fcntl.LOCK_SH

    def __enter__(self):
        self.lockfile = open(self.path, "w+")

        if self.timeout is None:
            fcntl.lockf(self.lockfile, self.lock_flag)
        else:
            # we would use a signal to timeout but it can only be used on the main thread
            e = _try_lockf(self.lockfile, self.lock_flag | fcntl.LOCK_NB)
            if e:
                for _ in range(self.timeout):
                    time.sleep(1)
                    e = _try_lockf(self.lockfile, self.lock_flag | fcntl.LOCK_NB)
                    if not e:
                        break
                else:
                    raise e

        return self.lockfile

    def __exit__(self, etype: Optional[Type[BaseException]], e: Optional[BaseException], tb: Optional[TracebackType]):
        try:
            os.unlink(self.path)
        except:
            pass

        fcntl.lockf(self.lockfile, fcntl.LOCK_UN)

        self.lockfile.close()


class StreamingFileResponse(DjangoFileResponse):
    def __init__(self, path: str):
        super().__init__(open(os.path.join(settings.COURSES_PATH, path), "rb"))


class XSendFileResponse(HttpResponse):
    def __init__(self, path: str):
        super().__init__()
        self["X-Accel-Redirect"] = os.path.join("/authorized_static", path)


if settings.USE_X_SENDFILE:
    FileResponse = XSendFileResponse
else:
    FileResponse = StreamingFileResponse
