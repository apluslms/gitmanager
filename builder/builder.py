import importlib
from io import StringIO
import json
from json.decoder import JSONDecodeError
import logging
from pathlib import Path
import os
import random
import shlex
import shutil
import string
import sys
import traceback
from types import ModuleType
from typing import List, Optional, Set, Tuple
import urllib.parse

from django.conf import settings
from django.db.models.functions import Now
from huey.contrib.djhuey import db_task, lock_task
from huey.exceptions import RetryTask, TaskLockedException
from pydantic.error_wrappers import ValidationError

from aplus_auth.payload import Permission, Permissions
from aplus_auth.requests import post

from access.config import ConfigSource, CourseConfig, load_meta, META
from access.parser import ConfigError
from builder.configure import configure_graders, publish_graders
from util.files import (
    copyfile,
    copys_async,
    is_subpath,
    renames,
    rm_except,
    FileLock,
    rsync
)
from util.git import checkout, clean, clone_if_doesnt_exist, get_diff_names, get_commit_hash_or_none, get_commit_metadata
from util.perfmonitor import PerfMonitor
from util.pydantic import validation_error_str, validation_warning_str
from util.static import static_url, static_url_path, symbolic_link
from util.typing import PathLike
from .models import Course, CourseUpdate


logger = logging.getLogger("builder.builder")

build_logger = logging.getLogger("builder.build")
build_logger.setLevel(logging.DEBUG)


def _import_path(path: str) -> ModuleType:
    """Imports an attribute (e.g. class or function) from a module from a specified path"""
    spec = importlib.util.spec_from_file_location("builder_module", path)
    if spec is None:
        raise ImportError(f"Couldn't find {path}")

    module = importlib.util.module_from_spec(spec)
    if module is None:
        raise ImportError(f"Couldn't import {path}")
    sys.modules["builder_module"] = module
    spec.loader.exec_module(module)

    return module

build_module = _import_path(settings.BUILD_MODULE)
if not hasattr(build_module, "build"):
    raise AttributeError(f"{settings.BUILD_MODULE} does not have a build function")
if not callable(getattr(build_module, "build")):
    raise AttributeError(f"build attribute in {settings.BUILD_MODULE} is not callable")


def _get_version_id() -> str:
    return "".join(random.choices(string.ascii_letters + string.digits, k=30))


def update_from_git(build_path: str, course: Course) -> Tuple[bool, Optional[Set[str]]]:
    """Updates course directory at <build_path> using git. Returns tuple of whether it was successful and
     a list of files that were changed since the last succesfull update (or None if the files are unknown)"""
    changed_files = None

    clone_status = clone_if_doesnt_exist(build_path, course.git_origin, course.git_branch, logger=build_logger)
    if clone_status is None:
        checkout_status = checkout(build_path, course.git_origin, course.git_branch, logger=build_logger)
        if not checkout_status:
            build_logger.info("------------\nFailed to checkout repository\n------------\n\n")
            return False, None

        # Get changed files since last successful update.
        # A failed update may mess up an output file, so we also need to include any changes that
        # were part of failed updates but may have been reverted later. This is why we need to
        # call get_diff_names for each consecutive update pair instead of just comparing to the last
        # successful one.

        updates = CourseUpdate.objects.filter(
                course=course,
                status__in=(CourseUpdate.Status.SUCCESS, CourseUpdate.Status.FAILED)
            ).order_by("-request_time")

        changed_files = set()
        last_commit_hash = None
        for update in updates:
            # If any update in the chain doesn't have a commit hash, we can't reliably detect the changed files
            if update.commit_hash is None:
                changed_files = None
                break

            diff_error, changed = get_diff_names(build_path, update.commit_hash, last_commit_hash)
            if diff_error:
                build_logger.error(diff_error)
                changed_files = None
                break
            changed_files.update(changed)

            last_commit_hash = update.commit_hash

            if update.status == CourseUpdate.Status.SUCCESS:
                break
        else:
            # None of the previous updates were successful: cannot detect changes since last successful update
            changed_files = None
    elif not clone_status:
        build_logger.info("------------\nFailed to clone repository\n------------\n\n")
        return False, None

    log_status, logstr = get_commit_metadata(build_path)
    if log_status:
        build_logger.info(logstr)
    else:
        build_logger.error(f"Failed to get commit metadata: \n{logstr}")

    return True, changed_files


def log_progress_update(update: CourseUpdate, log_stream: StringIO) -> None:
    update.log = log_stream.getvalue() + "\n\n..."
    update.save(update_fields=["log"])


def build(
        course: Course,
        path: Path,
        image: Optional[str] = None,
        command: Optional[str] = None,
        changed_files: Set[str] = ["*"],
        ) -> bool:
    meta = load_meta(path)

    if image is not None:
        build_image = image
        build_command = command
        build_logger.info(f"Build image and command overridden: {build_image}, {build_command}\n\n")
    else:
        build_image = settings.DEFAULT_IMAGE
        build_command = command
        if meta:
            if "build_image" in meta:
                build_image = meta["build_image"]
                build_logger.info(f"Using build image: {build_image}")
            else:
                build_logger.info(f"No build_image in {META}, using the default: {build_image}")

            if "build_command" in meta:
                build_command = meta["build_command"]
                build_logger.info(f"Using build command: {build_command}\n\n")
            elif build_command is not None:
                build_logger.info(f"Build command overriden: {build_command}\n\n")
            elif not "build_image" in meta:
                build_command = settings.DEFAULT_CMD
                build_logger.info(f"No build_command in {META}, using the default: {build_command}\n\n")
            else:
                build_logger.info(f"No build_command in {META} or service settings, using the image default\n\n")
        else:
            build_logger.info(f"No {META} file, using the default build image: {build_image}\n\n")

    build_image = build_image.strip()

    if not build_image:
        build_logger.info(f"Build image is empty. Assuming no build is needed\n\n")
        return True

    env = {
        "COURSE_KEY": course.key,
        "COURSE_ID": str(course.remote_id),
        "STATIC_URL_PATH": static_url_path(course.key),
        "STATIC_CONTENT_HOST": static_url(course.key),
        "CHANGED_FILES": "\n".join(changed_files),
    }

    if build_command is not None:
        build_command = shlex.split(build_command)

    return build_module.build(
        logger=build_logger,
        course_key=course.key,
        path=path,
        image=build_image,
        cmd=build_command,
        env=env,
        settings=settings.BUILD_MODULE_SETTINGS,
    )


def send_error_mail(course: Course, subject: str, message: str) -> bool:
    """Send an error email to the course staff using A+ API"""
    if course.remote_id is None:
        build_logger.error(f"Remote id not set: cannot send error email")
        return False

    email_url = urllib.parse.urljoin(settings.FRONTEND_URL, f"api/v2/courses/{course.remote_id}/send_mail/")
    permissions = Permissions()
    permissions.instances.add(Permission.WRITE, id=course.remote_id)
    data = {
        "subject": subject,
        "message": message,
    }
    try:
        response = post(email_url, permissions=permissions, data=data, headers={"Accept": "application/json, application/*"})
    except:
        logger.exception(f"Failed to send email for {course.key}")
        build_logger.exception(f"Failed to send error email")
        return False

    if response.status_code != 200 or response.text:
        logger.error(f"Sending email for {course.key} failed: {response.status_code} {response.text}")
        build_logger.error(f"API failed to send the error email: {response.status_code} {response.text}")
        return False

    return True


def notify_update(course: Course):
    """Send an update notification to A+. This initiates the course import on A+."""
    errors = []
    success = False
    try:
        notification_url = urllib.parse.urljoin(settings.FRONTEND_URL, f"api/v2/courses/{course.remote_id}/notify_update/")
        permissions = Permissions()
        permissions.instances.add(Permission.WRITE, id=course.remote_id)
        response = post(notification_url, permissions=permissions, data={"email_on_error": course.email_on_error}, headers={"Accept": "application/json, application/*"})
    except Exception as e:
        logger.exception(f"Failed to notify_update for course id {course.remote_id}")
        errors.append(str(e))
    else:
        if response.status_code != 200:
            logger.error(f"notify_update returned {response.reason}: {response.text}")
            errors.append(response.reason)
        else:
            try:
                data = json.loads(response.text)
            except JSONDecodeError:
                logger.exception("Failed to load notify_update response JSON")
                errors.append("Failed to load notify_update response JSON")
            else:
                success = data.get("success", True)
                response_errors = data.get("errors", [])
                if not isinstance(response_errors, list):
                    response_errors = [str(response_errors)]

                errors.extend(response_errors)
    finally:
        errorstr = "\n".join(errors)
        if success and not errorstr:
            build_logger.info("Success.")
        elif success and errorstr:
            build_logger.warn("Success with warnings:")
            build_logger.warn(errorstr)
        else:
            build_logger.error("Failed:")
            build_logger.error(errorstr)

            if course.email_on_error:
                send_error_mail(
                    course,
                    f"Failed to notify update of {course.key}",
                    f"Build succeeded but notifying the frontend of the update failed:\n{errorstr}"
                )


def is_self_contained(path: PathLike) -> Tuple[bool, Optional[str]]:
    spath = os.fspath(path)
    for root, _, files in os.walk(spath):
        rpath = Path(root)
        for file in files:
            if not is_subpath(str((rpath / file).resolve()), spath):
                return False, f"{rpath / file} links to a path outside the course directory"
            if os.path.islink(rpath / file) and os.path.isabs(os.readlink(rpath / file)):
                return False, f"{rpath / file} is an absolute symlink: this will break the course"

    return True, None


def store(perfmonitor: PerfMonitor, config: CourseConfig) -> bool:
    """
    Stores the built course files and sends the configs to the graders.

    Returns False on failure and True on success.

    May raise an exception (due to FileLock timing out).
    """
    course_key = config.key

    build_logger.info("Configuring graders...")
    # Send configs to graders' stores
    exercise_defaults, errors = configure_graders(config)
    # Abort the build if any errors happened
    if errors:
        for e in errors:
            build_logger.error(e)
        return False

    perfmonitor.checkpoint("Configure graders")

    store_path, store_defaults_path, store_version_path = CourseConfig.file_paths(course_key, source=ConfigSource.STORE)

    build_logger.info("Acquiring file lock...")
    # Lock the course folder in store so that no other process modifies it at the same time.
    # If any other process already has a lock to the course folder, this will block until
    # the lock is released or BUILD_FILELOCK_TIMEOUT seconds has passed (in which case
    # the build fails). The likely situation for this blocking is that the copys_async function
    # called from the publish function has the lock.
    with FileLock(store_path, write=True, timeout=settings.BUILD_FILELOCK_TIMEOUT):
        build_logger.info("File lock acquired.")

        build_logger.info("Copying the built materials")

        static_dir = config.data.static_dir or ""

        # Remove all stored files (for the course) exept the static directory which we will rsync over.
        rm_except(store_path, CourseConfig.path_to(course_key, static_dir, source=ConfigSource.STORE))

        perfmonitor.checkpoint("Remove old stored files")

        dst = CourseConfig.path_to(course_key, static_dir, source=ConfigSource.STORE)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        # rsync the static directory
        num_changed = rsync(
            CourseConfig.path_to(course_key, static_dir, source=ConfigSource.BUILD),
            dst,
        )

        build_logger.info(f"Rsync: {num_changed} files in {static_dir} changed")

        perfmonitor.checkpoint("Copy static files")

        grader_config_dir = str(Path(config.grader_config_dir).relative_to(config.dir))

        copy_files = set()

        # Add the metafile to the files to be copied if it exists
        if os.path.exists(CourseConfig.path_to(course_key, META, source=ConfigSource.BUILD)):
            copy_files.add(META)

        # Find other (not static directory) required files
        for exercise in config.data.exercises():
            config_file_info = exercise.config_file_info(
                "",
                grader_config_dir
            )
            if config_file_info:
                copy_files.add(os.path.join(*config_file_info))

            if exercise._config_obj:
                for lang_data in exercise._config_obj.data.values():
                    if "template_files" in lang_data:
                        copy_files.update(lang_data["template_files"])
                    if "model_files" in lang_data:
                        copy_files.update(lang_data["model_files"])

                    for include_data in lang_data.get("include", []):
                        copy_files.add(include_data["file"])

        copy_files = {
            file[1:] if file.startswith("/") else file
            for file in copy_files
        }

        index_file = str(Path(config.file).relative_to(config.dir))
        dst = CourseConfig.path_to(course_key, index_file, source=ConfigSource.STORE)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        copyfile(config.file, dst)

        # Copy the other files
        for file in copy_files:
            src = CourseConfig.path_to(course_key, file, source=ConfigSource.BUILD)
            if not os.path.exists(src):
                build_logger.warning(f"Couldn't find file '{file}'")
                continue

            dst = CourseConfig.path_to(course_key, file, source=ConfigSource.STORE)
            Path(dst).parent.mkdir(parents=True, exist_ok=True)

            copyfile(src, dst)

        # Copy exercise defaults
        with open(store_defaults_path, "w") as f:
            json.dump(exercise_defaults, f)

        # Copy version file
        if config.version_id is not None:
            with open(store_version_path, "w") as f:
                f.write(config.version_id)

        perfmonitor.checkpoint("Copy other files")

    # Save the config to the store cache
    config.save_to_cache(ConfigSource.STORE)

    return True


def publish(course_key: str, source: ConfigSource, version_id: Optional[str]) -> List[str]:
    """
    Publishes the stored course files and tells graders to publish too.

    Returns a list of errors if something was published.

    Raises an exception if an error occurs before anything could be published.
    """
    prod_path, prod_defaults_path, prod_version_path = CourseConfig.file_paths(course_key, source=ConfigSource.PUBLISH)
    store_path, store_defaults_path, store_version_path = CourseConfig.file_paths(course_key, source=ConfigSource.STORE)

    config = None
    errors = []
    # Try loading from store first. Skip if the stored version has already been published
    if source == ConfigSource.STORE:
        with FileLock(store_path, timeout=settings.APLUS_JSON_FILELOCK_TIMEOUT):
            try:
                config = CourseConfig.get(course_key, source=ConfigSource.STORE)
            except ConfigError as e:
                errors.append(f"Failed to load newly built course for this reason: {e}")
                logger.warn(f"Failed to load newly built course for this reason: {e}")
            else:
                if config.version_id == version_id:
                    with FileLock(prod_path, write=True, timeout=settings.APLUS_JSON_FILELOCK_TIMEOUT):
                        renames([
                            (store_path, prod_path),
                            (store_defaults_path, prod_defaults_path),
                            (store_version_path, prod_version_path),
                        ])

                    config.save_to_cache(ConfigSource.PUBLISH)
                    # Copy files back to store so that rsync has files to compare against.
                    # This is done asyncronously: the copying might still be ongoing even
                    # after the publishing is done.
                    copys_async([
                            (prod_path, store_path),
                            (prod_defaults_path, store_defaults_path),
                            (prod_version_path, store_version_path),
                        ],
                        read_lock_path=prod_path,
                        write_lock_path=store_path,
                    )
    elif source == ConfigSource.PUBLISH:
        with FileLock(prod_path, timeout=settings.APLUS_JSON_FILELOCK_TIMEOUT):
            try:
                config = CourseConfig.get(course_key, source=ConfigSource.PUBLISH)
            except ConfigError as e:
                errors.append(f"Failed to load already published config: {e}")
                logger.error(f"Failed to load already published config: {e}")
    else:
        raise Exception("Publishing from the build directory is not allowed")

    if config is None:
        if errors:
            raise Exception("\n".join(errors))
        else:
            raise Exception(f"Course directory not found for {course_key} - the course probably has not been built")
    elif config.version_id != version_id:
        errors.append(
            "Config version doesn't match the given version. Was the course updated while A+ "
            "was processing the config? Try importing the course again."
        )
        raise Exception("\n".join(errors))

    # Create symbolic links to the course files
    symbolic_link(config)

    # Publish version on graders
    errors = errors + publish_graders(config)

    return errors


# the task locks can get stuck if the program suddenly shuts down,
# so flush them when debugging. We dont want to flush otherwise because
# there may be a queued task left that would also get flushed.
if settings.DEBUG:
    from huey.contrib.djhuey import HUEY
    try:
        HUEY.flush()
    except:
        logger.error("Failed to flush HUEY storage")


@db_task(retry_delay=settings.BUILD_RETRY_DELAY)
def push_event(
        course_key: str,
        skip_git: bool = False,
        skip_build: bool = False,
        skip_notify: bool = False,
        rebuild_all: bool = False,
        build_image: Optional[str] = None,
        build_command: Optional[str] = None,
        ) -> None:
    logger.debug(f"push_event: {course_key}")

    try:
        # lock_task to make sure that two updates don't happen at the
        # same time.
        with lock_task(f"build-{course_key}"):
            build_course(
                course_key,
                skip_git,
                skip_build,
                skip_notify,
                rebuild_all,
                build_image,
                build_command,
            )
    except TaskLockedException:
        raise RetryTask()

def build_course(
        course_key: str,
        skip_git: bool = False,
        skip_build: bool = False,
        skip_notify: bool = False,
        rebuild_all: bool = False,
        build_image: Optional[str] = None,
        build_command: Optional[str] = None,
        ) -> None:
    course: Course = Course.objects.get(key=course_key)

    # delete all but latest 10 updates
    updates = CourseUpdate.objects.filter(course=course).order_by("-request_time")[10:]
    for update in updates:
        update.delete()
    # get pending updates
    updates = CourseUpdate.objects.filter(course=course, status=CourseUpdate.Status.PENDING).order_by("request_time").all()

    updates = list(updates)
    if len(updates) == 0:
        return

    # skip all but the most recent update
    for update in updates[:-1]:
        update.status = CourseUpdate.Status.SKIPPED
        update.save()

    update = updates[-1]

    perfmonitor = PerfMonitor()

    log_stream = StringIO()
    log_handler = logging.StreamHandler(log_stream)
    build_logger.addHandler(log_handler)
    try:
        update.status = CourseUpdate.Status.RUNNING
        update.save()

        if course.skip_build_failsafes:
            build_config_source = ConfigSource.PUBLISH
        else:
            build_config_source = ConfigSource.BUILD

        build_path = CourseConfig.path_to(course_key, source=build_config_source)

        changed_files = None
        if skip_git:
            build_logger.info("Skipping git update.")
        elif course.git_origin:
            success, changed_files = update_from_git(build_path, course)
            if not success:
                return
        elif settings.LOCAL_COURSE_SOURCE_PATH:
            path = CourseConfig.local_source_path_to(course_key)
            build_logger.debug(f"Course origin not set: copying the course sources from {path} to the build directory.")

            shutil.rmtree(build_path, ignore_errors=True)
            shutil.copytree(path, build_path, symlinks=True)
        else:
            build_logger.warning(f"Course origin not set: skipping git update\n")

        update.commit_hash = get_commit_hash_or_none(build_path)
        update.save(update_fields=["commit_hash"])

        log_progress_update(update, log_stream)

        perfmonitor.checkpoint("Git clone/checkout")

        if not skip_build:
            if rebuild_all:
                build_logger.info("Rebuild all specified: setting CHANGED_FILES to *\n\n")
                changed_files = set(["*"])
            elif changed_files is None:
                build_logger.info("Failed to detect changed files: setting CHANGED_FILES to *\n\n")
                changed_files = set(["*"])
            elif len(changed_files) > 10:
                build_logger.info(f"Detected over 10 changed files (too many to show)\n\n")
            else:
                build_logger.info(f"Detected changed files: {', '.join(changed_files)}\n\n")

            # build in build_path folder
            build_status = build(course, Path(build_path), image = build_image, command = build_command, changed_files = changed_files)
            if not build_status:
                return
        else:
            build_logger.info("Skipping build.")

        log_progress_update(update, log_stream)

        perfmonitor.checkpoint("Course build script")

        value, error = is_self_contained(build_path)
        if not value:
            build_logger.error(f"Course {course_key} is not self contained: {error}")
            return

        log_progress_update(update, log_stream)

        perfmonitor.checkpoint("Symlink containment check")

        id_path = CourseConfig.version_id_path(course_key, source=build_config_source)
        with open(id_path, "w") as f:
            f.write(_get_version_id())

        # try loading the configs to validate them
        try:
            config = CourseConfig.get(course_key, build_config_source)
            config.get_exercise_list()
        except ConfigError as e:
            build_logger.warning("Failed to load config")
            raise
        except ValidationError as e:
            build_logger.error(validation_error_str(e))
            return

        log_progress_update(update, log_stream)

        perfmonitor.checkpoint("Load config")

        warning_str = validation_warning_str(config.data)
        if warning_str:
            build_logger.warning(warning_str + "\n")

        log_progress_update(update, log_stream)

        # copy the course material to store
        if not course.skip_build_failsafes:
            if not store(perfmonitor, config):
                build_logger.error("Failed to store built course")
                return

        log_progress_update(update, log_stream)

        # all went well
        update.status = CourseUpdate.Status.SUCCESS
    except:
        build_logger.error("Build failed.\n")
        build_logger.error(traceback.format_exc() + "\n")
    else:
        if not course.update_automatically:
            build_logger.info("Configured to not update automatically.")
        elif course.remote_id is None:
            build_logger.warning("Remote id not set. Not doing an automatic update.")
        elif skip_notify:
            build_logger.info("Skipping automatic update.")
        elif settings.FRONTEND_URL is None:
            build_logger.warning("FRONTEND_URL not set. Not doing an automatic update.")
        else:
            build_logger.info("Doing an automatic update...")
            notify_update(course)

            perfmonitor.checkpoint("Automatic update")
    finally:
        if update.status != CourseUpdate.Status.SUCCESS:
            update.status = CourseUpdate.Status.FAILED

            if course.email_on_error:
                send_error_mail(course, f"Course {course_key} build failed", log_stream.getvalue())

        update.log = log_stream.getvalue()

        update.updated_time = Now()
        update.save()

        try:
            meta = load_meta(build_path)
            exclude_patterns = shlex.split(meta.get("exclude_patterns", ""))

            clean_status = clean(build_path, course.git_origin, course.git_branch, exclude_patterns, logger=build_logger)
            if not clean_status:
                build_logger.info("------------\nFailed to clean repository\n------------\n\n")
                return

            perfmonitor.checkpoint("Git clean")
        except:
            build_logger.error("Clean failed.\n")
            build_logger.error(traceback.format_exc() + "\n")
        finally:
            build_logger.info("\nTime taken for each step in seconds:")
            build_logger.info(perfmonitor.formatted())

            update.log = log_stream.getvalue()
            update.save(update_fields=["log"])

            build_logger.removeHandler(log_handler)
