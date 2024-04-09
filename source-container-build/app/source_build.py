#!/usr/bin/python3.11

import argparse
import functools
import json
import itertools
import os
import shutil
import logging
import stat
import sys
import tarfile
import tempfile
import filetype
import hashlib
from dataclasses import dataclass, field
from subprocess import run
from typing import TypedDict, NotRequired, Literal, Final
from urllib.parse import urlparse

"""
Requires: git, skopeo, tar, BuildSourceImage
"""

logging.basicConfig(level=logging.DEBUG, format="%(asctime)s:%(name)s:%(levelname)s:%(message)s")
logger = logging.getLogger("source-build")

BSI: Final = "/opt/BuildSourceImage/bsi"
BSI_DRV_RPM_DIR: Final = "sourcedriver_rpm_dir"
BSI_DRV_EXTRA_SRC_DIR: Final = "sourcedriver_extra_src_dir"

ARCHIVE_MIMETYPES = (
    "application/gzip",
    "application/x-bzip2",
    "application/x-compress",
    "application/x-tar",
    "application/x-xz",
    "application/zip",
)

MAX_RETRIES: Final = 5


class BuildResult(TypedDict):
    status: Literal["failure", "success"]
    message: NotRequired[str]
    dependencies_included: bool
    base_image_source_included: bool
    image_url: str
    image_digest: str


@dataclass
class SourceImageBuildDirectories:
    rpm_dir: str = ""
    extra_src_dirs: list[str] = field(default_factory=list)


class RepoInfo(TypedDict):
    name: str
    last_commit_sha: str


def arg_type_path(value):
    if not os.path.exists(value):
        raise argparse.ArgumentTypeError(f"No file or directory exists at specified path {value}")
    return value


def arg_type_bsi_script(value):
    if not os.path.exists(value):
        raise argparse.ArgumentTypeError(f"BuildSourceImage executable {value} does not exist")
    fstat = os.stat(value)
    if not stat.S_IXUSR & fstat.st_mode:
        raise argparse.ArgumentTypeError(f"BuildSourceImage script {value} is not executable")
    return value


def arg_type_base_images(value):
    return value.strip()


def arg_type_registry_allowlist(value: str) -> list[str]:
    return [line for line in value.splitlines() if line]


def get_repo_info(repo_path: str) -> RepoInfo:
    _run = functools.partial(run, check=True, text=True, capture_output=True, cwd=repo_path)
    commit_sha = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    repo_url = _run(["git", "config", "--get", "remote.origin.url"]).stdout.strip().strip("/")
    # The url could look like https://github.com/namespace/app.git
    # where, app is required.
    repo_name, _ = os.path.splitext(repo_url.rsplit("/", maxsplit=1)[-1])
    return {
        "name": repo_name,
        "last_commit_sha": commit_sha,
    }


def parse_cli_args():
    parser = argparse.ArgumentParser(description="Source image build task")
    parser.add_argument(
        "-w",
        "--workspace",
        type=arg_type_path,
        metavar="PATH",
        dest="workspace_dir",
        help="The workspace directory",
    )
    parser.add_argument(
        "--bsi",
        type=arg_type_bsi_script,
        default=BSI,
        help="Path to the BuildSourceImage executable. "
        "Defaults to %(default)s that is installed in the execution container.",
    )
    parser.add_argument(
        "-s",
        "--source-dir",
        required=True,
        dest="source_dir",
        metavar="PATH",
        type=arg_type_path,
        help="Path to the directory holding source code from which to build the binary image.",
    )
    parser.add_argument(
        "--output-binary-image",
        required=True,
        metavar="IMAGE",
        help="The output binary image used to generate source image.",
    )
    parser.add_argument(
        "--base-images",
        metavar="IMAGES",
        type=arg_type_base_images,
        default="",
        help="Base images used to build the binary image, from which to get the sources. "
        "Each image per line and only the last (bottom) one is handled, which is the "
        "single image or the last image specified in a multistage Dockerfile. If omitted, "
        "skip handling sources of base image.",
    )
    parser.add_argument(
        "--cachi2-artifacts-dir",
        metavar="PATH",
        help="Path to Cachi2 directory which is the output directory populated by fetch-deps "
        "command and the generated environment file.",
    )
    parser.add_argument(
        "--write-result-to",
        metavar="FILE",
        dest="result_file",
        help="Write execution result into this file.",
    )
    parser.add_argument(
        "--registry-allowlist",
        type=arg_type_registry_allowlist,
        required=True,
        help="Resolve source images for parent images pulled from the registry listed here. "
        "One registry per line.",
    )
    return parser.parse_args()


def registry_has_image(image: str) -> bool:
    cmd = ["skopeo", "inspect", "--raw", "--retry-times", str(MAX_RETRIES), f"docker://{image}"]
    return run(cmd, capture_output=True).returncode == 0


def fetch_image_config(image: str) -> str:
    cmd = ["skopeo", "inspect", "--config", "--retry-times", str(MAX_RETRIES), f"docker://{image}"]
    return run(cmd, check=True, text=True, capture_output=True).stdout.strip()


def fetch_image_manifest_digest(image: str) -> str:
    cmd = [
        "skopeo",
        "inspect",
        "--format",
        "{{.Digest}}",
        "--no-tags",
        "--retry-times",
        str(MAX_RETRIES),
        f"docker://{image}",
    ]
    return run(cmd, check=True, text=True, capture_output=True).stdout.strip()


# produces an artifact name that includes artifact's architecture
# and repository id in the name
def unique_srpm_artifact_name(file: str) -> str:
    root, filename = os.path.split(file)
    with open(file, "rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
        return f"{digest}-{filename}"


def create_dir(*components) -> str:
    path = os.path.join(*components)
    os.makedirs(path)
    return path


def extract_blob_member(
    tar_archive: str, member: str, dest_dir: str, rename_to: str, work_dir: str, log: logging.Logger
) -> None:
    """Extract a blob member and rename it."""
    log.debug("extract from %s/%s: %s -> %s/%s", work_dir, tar_archive, member, dest_dir, rename_to)
    with tarfile.open(os.path.join(work_dir, tar_archive)) as tar:
        content = tar.extractfile(member)
        if not content:
            raise ValueError(f"Could not etract {member} from {work_dir}/{tar_archive}")
        with open(os.path.join(dest_dir, rename_to), "wb") as f:
            shutil.copyfileobj(content, f)


def prepare_base_image_sources(
    image: str, work_dir: str, sib_dirs: SourceImageBuildDirectories
) -> bool:
    log = logging.getLogger("source-build.base-image-sources")

    base_image_sources_dir = create_dir(work_dir, "base_image_sources")
    base_sources_extraction_dir = create_dir(base_image_sources_dir, "extraction_dir")

    source_image_name = resolve_source_image_by_version_release(image)

    if not source_image_name:
        logger.warning(
            "The registry does not have corresponding source image %s", source_image_name
        )
        return False

    cmd = [
        "skopeo",
        "copy",
        "--retry-times",
        str(MAX_RETRIES),
        f"docker://{source_image_name}",
        f"dir:{base_sources_extraction_dir}",
    ]
    log.info(
        "Copy source image %s into directory %s",
        source_image_name,
        str(base_sources_extraction_dir),
    )
    run(cmd, check=True)

    # bsi reads source RPMs from this directory
    bsi_rpms_dir = create_dir(base_image_sources_dir, "bsi_rpms_dir")
    sib_dirs.rpm_dir = str(bsi_rpms_dir)  # save this directory for executing bsi

    # bsi reads extra sources from this directory
    # each source is in its own directory, for instance, subdir/source_a.tar.gz
    extra_src_dir = create_dir(base_sources_extraction_dir, "extra_src_dir")

    # extract layers, primarily they are RPMs
    with open(f"{base_sources_extraction_dir}/manifest.json", "r") as f:
        manifest_data = json.load(f)

    gathered = False

    for layer in manifest_data["layers"]:
        digest = layer["digest"].split(":")[-1]
        log.debug("untar layer %s", digest)

        blob_member = ""
        symlink_member = ""
        with tarfile.open(f"{base_sources_extraction_dir}/{digest}", "r:gz") as tar:
            for member in tar:
                if member.isfile():
                    blob_member = member.name
                elif member.issym():
                    symlink_member = member.name

        if symlink_member.startswith("./rpm_dir/"):
            dest_dir = sib_dirs.rpm_dir
            log.debug("Prepare SRPM %s", symlink_member)

            extract_blob_member(
                digest,
                blob_member,
                dest_dir,
                rename_to=os.path.basename(symlink_member),
                work_dir=base_sources_extraction_dir,
                log=log,
            )

            gathered = True

        elif symlink_member.startswith("./extra_src_dir/"):
            extra_src_archive = os.path.basename(symlink_member)
            log.debug("Prepare extra source %s", extra_src_archive)
            # one extra source archive per directory, no matter what the directory name is.
            dest_dir = create_dir(extra_src_dir, extra_src_archive)
            sib_dirs.extra_src_dirs.append(dest_dir)  # save this directory for bsi

            extract_blob_member(
                digest,
                blob_member,
                str(dest_dir),
                rename_to=extra_src_archive,
                work_dir=base_sources_extraction_dir,
                log=log,
            )

            gathered = True

            # FIXME: perhaps the dependency archive (the Cachi2) might be handled differently
            run(["tar", "xvf", extra_src_archive], check=True, cwd=dest_dir)
            os.unlink(f"{dest_dir}/{extra_src_archive}")

        else:
            log.warning("No known operation happened on layer %s", digest)

    return gathered


def gather_prefetched_sources(
    work_dir: str, cachi2_dir: str, sib_dirs: SourceImageBuildDirectories
) -> bool:
    log = logging.getLogger("source-build.prefetched-sources")
    gathered = False

    # Guess if hermetic build is enabled
    # NOTE: this guess does depend on how cachi2 runs inside prefetch-dependencies task.
    cachi2_output_dir = f"{cachi2_dir}/output"

    if not os.path.isdir(cachi2_output_dir):
        log.info("Cannot find cachi2 output directory at %s", cachi2_output_dir)
        return gathered

    def _find_prefetch_source_archives():
        guess_mime = filetype.guess_mime
        for root, _, files in os.walk(cachi2_output_dir):
            for filename in files:
                mimetype = guess_mime(os.path.join(root, filename))
                if mimetype and mimetype in ARCHIVE_MIMETYPES:
                    yield root, filename

    def _find_prefetch_srpm_archives():
        guess_mime = filetype.guess_mime
        for root, dirs, files in os.walk(cachi2_output_dir):
            dirs.sort()
            for filename in sorted(files):
                if filename.endswith(".src.rpm"):
                    mimetype = guess_mime(os.path.join(root, filename))
                    if mimetype and mimetype == "application/x-rpm":
                        yield root, filename

    source_counter = itertools.count()
    prepared_sources_dir = create_dir(work_dir, "prefetched_sources")
    relative_to = os.path.relpath

    for root, filename in _find_prefetch_source_archives():
        src_dir = f"src-{next(source_counter)}"
        copy_dest_dir = f"{prepared_sources_dir}/{src_dir}/{relative_to(root, cachi2_output_dir)}"
        os.makedirs(copy_dest_dir)

        src = f"{root}/{filename}"
        dest = f"{copy_dest_dir}/{filename}"
        log.debug("copy prefetched source %s to %s", src, dest)
        shutil.copy(src, dest)
        sib_dirs.extra_src_dirs.append(f"{prepared_sources_dir}/{src_dir}")

    srpm_counter = itertools.count()
    for root, filename in _find_prefetch_srpm_archives():
        next(srpm_counter)
        src = os.path.join(root, filename)
        dest = os.path.join(sib_dirs.rpm_dir, filename)
        if os.path.isfile(dest):
            unique_src_name = unique_srpm_artifact_name(src)
            unique_dest_name = unique_srpm_artifact_name(dest)
            if unique_src_name != unique_dest_name:
                dest = os.path.join(sib_dirs.rpm_dir, unique_src_name)
            else:
                log.debug("identical artifacts found for %s. skipping %s", dest, src)
                continue
        log.debug("copy prefetched rpm source %s to %s", src, dest)
        shutil.copy(src, dest)

    gathered = next(source_counter) + next(srpm_counter) > 0
    if not gathered:
        log.info("There is no prefetched source archive.")

    cachi2_env = f"{cachi2_dir}/cachi2.env"
    if os.path.exists(cachi2_env):
        prepared_env_dir = create_dir(work_dir, "cachi2_env")
        src = cachi2_env
        dest = f"{prepared_env_dir}/cachi2.env"
        log.debug("copy cachi2 env file %s to %s", src, dest)
        shutil.copy(src, dest)
        sib_dirs.extra_src_dirs.append(prepared_env_dir)

    return gathered


def make_source_archive(
    work_dir: str, source_dir: str, sib_dirs: SourceImageBuildDirectories
) -> None:
    log = logging.getLogger("build-source.source-archive")
    source_archive_dir = create_dir(work_dir, "source_archive")
    repo_info = get_repo_info(source_dir)
    name_sha = f"{repo_info['name']}-{repo_info['last_commit_sha']}"
    output_archive = f"{source_archive_dir}/{name_sha}.tar.gz"

    stash_cmd = ["git", "stash"]
    log.debug("Stashing any changes to working repo %r", stash_cmd)
    run(stash_cmd, check=True, cwd=source_dir)

    mtime_cmd = ["git", "show", "-s", "--format=%cI"]
    log.debug("Collecting timestamp of the commit at HEAD %r", mtime_cmd)
    mtime_process = run(mtime_cmd, check=True, cwd=source_dir, capture_output=True, text=True)
    mtime = mtime_process.stdout.strip()

    ls_cmd = ["git", "ls-files", "--recurse-submodules"]
    log.debug("Generate source repo file list %r", ls_cmd)
    git_process = run(ls_cmd, check=True, cwd=source_dir, capture_output=True, text=True)

    tar_cmd = [
        "tar",
        "caf",
        output_archive,
        "--mtime",
        mtime,
        "--transform",
        f"s,^,{name_sha}/,",
        "-T-",
    ]
    log.debug("Generate source archive %r", tar_cmd)
    run(tar_cmd, input=git_process.stdout.encode("utf-8"), check=True, cwd=source_dir)

    pop_cmd = ["git", "stash", "pop"]
    log.debug("Popping any stashed changes to working repo %r", pop_cmd)
    run(pop_cmd, cwd=source_dir)

    log.info("add source archive directory to sources for bsi: %s", source_archive_dir)
    sib_dirs.extra_src_dirs.append(source_archive_dir)


def build_and_push(
    work_dir: str,
    sib_dirs: SourceImageBuildDirectories,
    bsi_script: str,
    dest_images: list[str],
    build_result: BuildResult,
) -> None:
    log = logging.getLogger("source-build.build-and-push")
    bsi_build_base_dir = create_dir(work_dir, "bsi_build")
    image_output_dir = create_dir(work_dir, "bsi_output")

    bsi_src_drivers = []
    bsi_cmd = [bsi_script, "-b", str(bsi_build_base_dir), "-o", str(image_output_dir)]
    if sib_dirs.rpm_dir:
        bsi_src_drivers.append(BSI_DRV_RPM_DIR)
        bsi_cmd.append("-s")
        bsi_cmd.append(str(sib_dirs.rpm_dir))
    if sib_dirs.extra_src_dirs:
        bsi_src_drivers.append(BSI_DRV_EXTRA_SRC_DIR)
        for dir_path in sib_dirs.extra_src_dirs:
            bsi_cmd.append("-e")
            bsi_cmd.append(str(dir_path))
    bsi_cmd.append("-d")
    bsi_cmd.append(",".join(bsi_src_drivers))
    if os.environ.get("BSI_DEBUG"):
        bsi_cmd.append("-D")

    log.debug("build source image %r", bsi_cmd)
    run(bsi_cmd, check=True)

    # push to registry
    fd, digest_file = tempfile.mkstemp()
    os.close(fd)
    for dest_image in dest_images:
        push_cmd = [
            "skopeo",
            "copy",
            "--digestfile",
            digest_file,
            "--retry-times",
            str(MAX_RETRIES),
            f"oci://{image_output_dir}:latest-source",
            f"docker://{dest_image}",
        ]
        log.debug("push source image %r", push_cmd)
        run(push_cmd, check=True)
    with open(digest_file, "r") as f:
        build_result["image_digest"] = f.read().strip()


def generate_source_images(image: str) -> list[str]:
    """Generate source container images from the built binary image

    :param image: str, represent the built image.
    :return: list of generated source container images.
    """
    # For backward-compatibility. It will be removed in near future.
    deprecated_image = f"{image}.src"

    # in format: sha256:1234567
    digest = fetch_image_manifest_digest(image)
    source_image = f"{image.rsplit(':', 1)[0]}:{digest.replace(':', '-')}.src"

    return [deprecated_image, source_image]


def resolve_source_image_by_version_release(binary_image: str) -> str | None:
    """Resolve source image by inspecting version and release of binary image

    :param binary_image: str, resolve source image for this binary image. A valid image reference
        should be passed, which then is inspected from the specified registry.
    :return: the resolved source image. If no source image is resolved, None is returned.
    """
    log = logging.getLogger(f"{logger.name}.resolve_source_image")
    name, _, digest = parse_image_name(binary_image)
    image_config = fetch_image_config(f"{name}@{digest}")
    config_data = json.loads(image_config)
    version = config_data["config"]["Labels"].get("version")
    release = config_data["config"]["Labels"].get("release")
    if not (version and release):
        log.warning("Image %s is not labelled with version and release.", binary_image)
        return
    # Remove possible tag or digest from binary image
    source_image = f"{name}:{version}-{release}-source"
    if registry_has_image(source_image):
        return source_image


def parse_image_name(image: str) -> tuple[str, str, str]:
    """Rough image name parser

    This does not aim to be a generic image name parser and just handle the
    base image names generated by the build-container task.
    """
    name, tag, digest = "", "", ""
    parts = image.split("@")
    name = parts[0]
    if len(parts) > 1:
        digest = parts[1]
    parts = name.rsplit(":", 1)
    name = parts[0]
    if len(parts) > 1:
        tag = parts[1]
    return name, tag, digest


def build(args) -> BuildResult:
    build_result: BuildResult = {
        "status": "success",
        "dependencies_included": False,
        "base_image_source_included": False,
        "image_url": "",
        "image_digest": "",
    }

    workspace_dir = args.workspace_dir
    if workspace_dir is None:
        workspace_dir = tempfile.mkdtemp(suffix="-source-build-workspace")
    else:
        workspace_dir = os.path.realpath(args.workspace_dir)
    logger.debug("workspace directory %s", workspace_dir)

    work_dir = create_dir(workspace_dir, "source-build")
    logger.debug("working directory %s", work_dir)

    sib_dirs = SourceImageBuildDirectories()

    make_source_archive(work_dir, args.source_dir, sib_dirs)

    # Handle base image sources
    if args.base_images:
        base_images: list[str] = args.base_images.splitlines()
        if len(base_images) > 1:
            logger.info("Multiple base images are specified: %r", base_images)
        base_image = base_images[-1]

        allowed = urlparse("docker://" + base_image).netloc in args.registry_allowlist
        if allowed:
            prepared = prepare_base_image_sources(base_image, work_dir, sib_dirs)
            build_result["base_image_source_included"] = prepared
        else:
            logger.info(
                "Image %s does not come from supported allowed registry. "
                "Skip handling the sources for it.",
                base_image,
            )
    else:
        logger.info("No base image is specified. Skip handling sources of base image.")

    if args.cachi2_artifacts_dir:
        included = gather_prefetched_sources(work_dir, args.cachi2_artifacts_dir, sib_dirs)
        build_result["dependencies_included"] = included
    else:
        logger.info(
            "Cachi2 artifacts directory is not specified. Skip handling the prefetched sources."
        )

    dest_images = generate_source_images(args.output_binary_image)
    build_result["image_url"] = dest_images[-1]

    build_and_push(work_dir, sib_dirs, args.bsi, dest_images, build_result)
    return build_result


def main() -> int:
    build_args = parse_cli_args()
    try:
        build_result = build(build_args)
    except Exception as e:
        build_result = {"status": "failure", "message": str(e)}
        logger.exception("failed to build source image")

    logger.info("build result %s", json.dumps(build_result))
    if build_args.result_file:
        logger.info("write build result into file %s", build_args.result_file)
        with open(build_args.result_file, "w") as f:
            json.dump(build_result, f)
    else:
        logger.info("no result file is specified. Skip writing build result into a file.")

    if build_result["status"] == "success":
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
