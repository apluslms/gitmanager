'''
Utility functions for exercise files.

'''
import fcntl
from pathlib import Path
import os
import shutil
import tempfile
import time
from types import TracebackType
from typing import Dict, Generator, Iterable, Optional, Tuple, Type, Union

from django.conf import settings

from util.typing import PathLike


META_PATH = os.path.join(settings.SUBMISSION_PATH, "meta")
if not os.path.exists(META_PATH):
    os.makedirs(META_PATH)


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


def is_subpath(child: str, parent: str):
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
        for _, _, tmp in done:
            if tmp is not None:
                rm_path(tmp)


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
    def __init__(self, path: PathLike, timeout: Optional[int] = None):
        self.path = os.fspath(path) + ".lock"
        self.timeout = timeout

    def __enter__(self):
        self.lockfile = open(self.path, "w")

        if self.timeout is None:
            fcntl.lockf(self.lockfile, fcntl.LOCK_EX)
        else:
            # we would use a signal to timeout but it can only be used on the main thread
            e = _try_lockf(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if e:
                for _ in range(self.timeout):
                    time.sleep(1)
                    e = _try_lockf(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    if not e:
                        break
                else:
                    raise e


        return self.lockfile

    def __exit__(self, etype:  Optional[Type[Exception]], e: Optional[Exception], traceback: TracebackType):
        try:
            os.unlink(self.path)
        except:
            pass

        fcntl.lockf(self.lockfile, fcntl.LOCK_UN)

        self.lockfile.close()
