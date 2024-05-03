#!/usr/bin/python3.11

import argparse
import filetype
import functools
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile

from dataclasses import dataclass, field
from pathlib import Path
from subprocess import run
from tarfile import TarInfo
from typing import Any, TypedDict, NotRequired, Literal, Final
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

StrPath = str | os.PathLike


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


def skopeo_copy(
    src: str,
    dest: str,
    digest_file: str = "",
    insecure_policy: bool = False,
    remove_signatures: bool = False,
) -> None:
    """Execute skopeo-copy

    :param src: str, same as source-image argument.
    :param dest: str, same as destination-image argumnet.
    :param digest_file: bool, map to the ``--digestfile`` argument.
    :param insecure_policy: bool, map to the ``--insecure-policy`` argument.
    :param remove_signatures: bool, map to the ``--remove-signatures`` argument.
    """
    flags = ["--retry-times", str(MAX_RETRIES)]
    if digest_file:
        flags.append("--digestfile")
        flags.append(digest_file)
    if insecure_policy:
        flags.append("--insecure-policy")
    if remove_signatures:
        flags.append("--remove-signatures")
    cmd = ["skopeo", "copy", *flags, src, dest]
    logger.debug("copy image: %r", cmd)
    run(cmd, check=True)


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

    sib_dirs.rpm_dir = create_dir(work_dir, "bsi_rpms_dir")
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

    ls_cmd = ["git", "ls-files", "--recurse-submodules", "-z"]
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
        "--null",
        "-T-",
    ]
    log.debug("Generate source archive %r", tar_cmd)
    run(tar_cmd, input=git_process.stdout.encode("utf-8"), check=True, cwd=source_dir)

    pop_cmd = ["git", "stash", "pop"]
    log.debug("Popping any stashed changes to working repo %r", pop_cmd)
    run(pop_cmd, cwd=source_dir)

    log.info("add source archive directory to sources for bsi: %s", source_archive_dir)
    sib_dirs.extra_src_dirs.append(source_archive_dir)


def build_source_image_in_local(
    bsi_script: str, work_dir: str, sib_dirs: SourceImageBuildDirectories
) -> str:
    bsi_build_base_dir = create_dir(work_dir, "bsi_build")
    image_output_dir = create_dir(work_dir, "bsi_output")

    bsi_src_drivers = []
    bsi_cmd = [bsi_script, "-b", str(bsi_build_base_dir), "-o", str(image_output_dir)]
    if sib_dirs.rpm_dir and len(os.listdir(sib_dirs.rpm_dir)) > 0:
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

    logger.debug("build source image %r", bsi_cmd)
    run(bsi_cmd, check=True)
    return image_output_dir


def push_to_registry(image_build_output_dir: str, dest_images: list[str]) -> str:
    fd, digest_file = tempfile.mkstemp()
    os.close(fd)
    src = f"oci:{image_build_output_dir}:latest-source"
    for dest_image in dest_images:
        logger.debug("push source image %r", dest_image)
        skopeo_copy(src, f"docker://{dest_image}", digest_file=digest_file)
    with open(digest_file, "r") as f:
        return f.read().strip()


def generate_konflux_source_image(image: str) -> str:
    # in format: sha256:1234567
    digest = fetch_image_manifest_digest(image)
    return f"{image.rsplit(':', 1)[0]}:{digest.replace(':', '-')}.src"


def generate_source_images(image: str) -> list[str]:
    """Generate source container images from the built binary image

    :param image: str, represent the built image.
    :return: list of generated source container images.
    """
    # For backward-compatibility. It will be removed in near future.
    deprecated_image = f"{image}.src"
    source_image = generate_konflux_source_image(image)
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
        return None
    # Remove possible tag or digest from binary image
    source_image = f"{name}:{version}-{release}-source"
    if registry_has_image(source_image):
        return source_image
    else:
        log.info("Source container image %s does not exist.", source_image)


def resolve_source_image_by_manifest(image: str) -> str | None:
    """Resolve source image by following Konflux source image scheme

    :param image: str, a binary image whose source image is resolved.
    :return: the resolved source image URL. If no one is resolved, None is returned.
    """
    source_image = generate_konflux_source_image(image)
    if registry_has_image(source_image):
        return source_image
    else:
        log = logging.getLogger(f"{logger.name}.resolve_source_image")
        log.info("Source container image %s does not exist.", source_image)


def resolve_source_image(binary_image: str, registries_allow_list: list[str]) -> str | None:
    allowed = urlparse("docker://" + binary_image).netloc in registries_allow_list
    if allowed:
        return resolve_source_image_by_version_release(binary_image)
    else:
        logger.info("Image %s does not come from supported allowed registry.", binary_image)
    return resolve_source_image_by_manifest(binary_image)


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


def download_parent_image_sources(source_image: str, work_dir: str) -> str:
    """Download parent sources that stored in OCI image layout

    :return: the directory holding the downloaded sources in the OCI image layout.
    :rtype: str
    """
    sources_dir = create_dir(work_dir, "parent_image_sources")
    logger.info("Copy source image %s into directory %s", source_image, sources_dir)
    # skopeo can not copy signatures to oci image layout
    # TODO - remove this invocation of `insecure_policy=True` once
    # https://issues.redhat.com/browse/CLOUDDST-22731 is resolved.
    skopeo_copy(
        f"docker://{source_image}",
        f"oci:{sources_dir}",
        insecure_policy=True,
        remove_signatures=True,
    )
    return sources_dir


class DescriptorT(TypedDict):
    mediaType: str
    digest: str
    size: int
    annotations: NotRequired[dict[str, str]]


class IndexT(TypedDict):
    schemaVersion: int
    manifests: list[DescriptorT]


class HistoryT(TypedDict):
    created: NotRequired[str]
    created_by: NotRequired[str]


class Blob:
    """Represent a blob inside an OCI image layout

    A blob object consists of a descriptor and the raw content read from the
    underlying blob file within the OCI image directory. The descriptor describes
    the type of the blob and is also used for pointing to the blob file storage
    in file system.

    This implementation so far supports two kinds of blobs, JSON blob and raw
    blob. The former relates to the image config and manifest, and the latter
    relates to the layers.
    """

    def __init__(self, layout: "OCIImage", descriptor: DescriptorT) -> None:
        """Initialize this blob

        :param layout: ``OCIImage``, the OCI image this blob belongs to.
        :param descriptor: dict, the OCI Descriptor describing this blob.
            The blob file is pointed out by the digest.
        """
        self._layout = layout
        self._descriptor = descriptor

        # Before accessing the raw content of a blob, this variable keeps None.
        # The content is read only once.
        self._raw_content: bytes | None = None

    def __eq__(self, that: object) -> bool:
        if not isinstance(that, self.__class__):
            return False
        return self.descriptor == that.descriptor

    @property
    def path(self) -> Path:
        return Path(self._layout.path, "blobs", *self._descriptor["digest"].split(":"))

    @property
    def descriptor(self) -> DescriptorT:
        return self._descriptor

    @property
    def raw_content(self) -> bytes:
        if self._raw_content is None:
            self.read()
        return self._raw_content  # type: ignore

    @raw_content.setter
    def raw_content(self, value: bytes) -> None:
        """Return the raw content read from corresponding blob file"""
        self._raw_content = value

    def read(self) -> None:
        """Read blob content from file"""
        self._raw_content = self.path.read_bytes()

    def delete(self) -> None:
        """Delete this blob from filesystem"""
        self.path.unlink()

    @property
    def to_python(self) -> bytes:
        """Subclass overrides this to return a Python object in specific type

        A blob can contain data in different types. This method converts the
        raw content to the specific Python type for read and write.

        :return: return the raw content by default.
        :rtype: bytes
        """
        return self.raw_content

    def save(self) -> "Blob":
        """Save updates to storage

        Subclass can override this method to provide custom save for specific
        type of data.

        :return: a new Blob object is returned to represent the written blob
            file. The new blob descriptor is copied from the original one but
            with updated digest and size. If nothing is changed to the content,
            return this blob itself.
        :rtype: Blob
        """
        if self._raw_content is None:
            return self

        checksum = hashlib.sha256(self._raw_content).hexdigest()
        cur_checksum = self.descriptor["digest"].removeprefix("sha256:")
        if cur_checksum == checksum:
            return self

        origin_d = self.descriptor
        new_d: DescriptorT = {
            "mediaType": origin_d["mediaType"],
            "digest": f"sha256:{checksum}",
            "size": 0,  # will be set later
        }
        if "annotations" in origin_d:
            new_d["annotations"] = origin_d["annotations"]

        new_blob = self.__class__(self._layout, new_d)
        new_blob.path.write_bytes(self._raw_content)
        new_blob.descriptor["size"] = new_blob.path.stat().st_size
        return new_blob


class Layer(Blob):
    """Represent an image layer"""


class JSONBlob(Blob):
    """A blob whose content is encoded in JSON"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._python_obj: dict | None = None

    @staticmethod
    def compact_json_dumps(data: Any) -> bytes:
        return json.dumps(data, separators=(",", ":")).encode("utf-8")

    def save(self) -> Blob:
        """Write JSON string in text mode"""
        self.raw_content = self.compact_json_dumps(self.to_python)
        return super().save()

    @property
    def to_python(self) -> dict[str, Any]:
        if self._python_obj is None:
            self._python_obj = json.loads(super().raw_content)
        return self._python_obj


class Config(JSONBlob):
    """Image config"""

    @property
    def history(self) -> list[HistoryT]:
        """Return .history"""
        return self.to_python["history"]

    @property
    def diff_ids(self) -> list[str]:
        """Return .rootfs.diff_ids"""
        return self.to_python["rootfs"]["diff_ids"]


class Manifest(JSONBlob):
    """Image manifest"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._config: Config | None = None
        self._layers: list[Layer] | None = None

    @property
    def config(self) -> Config:
        if self._config is None:
            self._config = Config(self._layout, self.to_python["config"])
        return self._config

    @property
    def layers(self) -> list[Layer]:
        if self._layers is None:
            self._layers = [Layer(self._layout, d) for d in self.to_python["layers"]]
        return self._layers

    def prepend_layer(self, layer: Layer) -> None:
        layers: list[DescriptorT] = self.to_python["layers"]
        layers.insert(0, layer.descriptor)

    def _find_layer(self, layer: Layer) -> int:
        """Find layer by descriptor from internal JSON raw manifest

        :param layer: a layer to find.
        :type layer: Layer
        :return: the index in the ``.layers``.
        :rtype: int
        """
        for idx, item in enumerate(self.to_python["layers"]):
            if item == layer.descriptor:
                return idx
        return -1

    def remove_layer(self, layer: Layer) -> tuple[DescriptorT, str, HistoryT]:
        """Remove a layer

        Layer descriptor is removed from this manifest, and associated diff_id
        and history are also removed from the config.

        :param layer: remove this layer.
        :type layer: Layer
        :return: return a 3 elements tuple about removed layer, that are the
            descriptor, diff_id and history.
        :rtype: tuple[dict, str, dict]
        """
        idx = self._find_layer(layer)
        if idx < 0:
            digest = layer.descriptor["digest"]
            raise ValueError(f"Layer with digest {digest} does not exist")
        layer.path.unlink()
        del self.to_python["layers"][idx]
        diff_id = self.config.diff_ids[idx]
        del self.config.diff_ids[idx]
        history = self.config.history[idx]
        del self.config.history[idx]
        return layer.descriptor, diff_id, history

    def save(self) -> Blob:
        """Save this manifest"""

        new_config = self.config.save()
        if new_config != self.config:
            self.to_python["config"] = new_config.descriptor

        layer_descriptors: list[DescriptorT] = self.to_python["layers"]
        for layer in self.layers:
            idx = self._find_layer(layer)
            if idx < 0:
                # deleted already, do nothing.
                continue
            if not layer.path.exists():
                raise ValueError(f"layer {str(layer.path)} does not exist.")
            new_layer = layer.save()
            if new_layer != layer:
                layer_descriptors[idx] = new_layer.descriptor
                layer.delete()

        return super().save()


class Index:
    """Represent the index.json of an OCI image"""

    def __init__(self, layout: "OCIImage"):
        self._layout = layout
        self._content: IndexT | None = None
        self._manifests: list[Manifest] | None = None

    @property
    def path(self) -> Path:
        return self._layout.path / "index.json"

    @property
    def content(self) -> IndexT:
        if self._content is None:
            self._content = json.loads(self.path.read_text())
        return self._content

    def manifests(self) -> list[Manifest]:
        """Return .manifests as a list of Manifest objects"""
        if not self._manifests:
            self._manifests = [Manifest(self._layout, d) for d in self.content["manifests"]]
        return self._manifests

    def save(self) -> None:
        """Save this index"""
        updated = False
        for idx, manifest in enumerate(self.manifests()):
            new_manifest = manifest.save()
            if new_manifest != manifest:
                self.content["manifests"][idx] = new_manifest.descriptor
                updated = True
        if updated:
            self._manifests = None
            self.path.write_text(json.dumps(self.content))


class OCIImage:
    """Represent an OCI image"""

    def __init__(self, path: StrPath):
        """Initialize this OCI image object

        :param path: a path to an OCI image.
        """
        self._path = Path(path)
        self._index: Index | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def index(self) -> Index:
        if self._index is None:
            self._index = Index(self)
        return self._index


class BSILayer:
    """Wrapper of a layer generated by BuildSourceImage for equality determination"""

    def __init__(self, layer: Layer) -> None:
        self._layer = layer
        self._symlink_member: TarInfo | None = None
        self._blob_member: TarInfo | None = None
        self._extra_source: TarInfo | None = None
        self._extract()
        self._check()

    @property
    def symlink_member(self) -> TarInfo:
        """Return symlink member

        :return: a ``tarfile.TarInfo`` representing the symlink member.
        """
        return self._symlink_member  # type: ignore

    @property
    def blob_member(self) -> TarInfo:
        """Return blob member

        :return: a ``tarfile.TarInfo`` representing the blob member.
        """
        return self._blob_member  # type: ignore

    @property
    def extra_source(self) -> TarInfo:
        """Return included extra source"""
        return self._extra_source  # type: ignore

    @staticmethod
    def is_extra_src(member: TarInfo) -> bool:
        """Check if an archive member is a link of extra source archive

        Example arcname: ./extra_src_dir/extra-src-100.tar
        """
        dirname, basename = os.path.split(member.name)
        regex: Final = r"^extra-src-[0-9a-f]+\.tar$"
        return (
            member.issym()
            and dirname == "./extra_src_dir"
            and re.match(regex, basename) is not None
        )

    @staticmethod
    def is_rpm_src(member: TarInfo) -> bool:
        """Check if an archive member is a link of RPM source

        Example arcname: ./rpm_dir/foo-1.0.src.rpm
        """
        dirname, basename = os.path.split(member.name)
        return member.issym() and dirname == "./rpm_dir" and basename.endswith(".src.rpm")

    def _is_blob_file(self, member: TarInfo) -> bool:
        """Check if an archive member is a blob file"""
        regex: Final = r"\./blobs/sha256/[0-9a-f]+"
        return member.isreg() and re.fullmatch(regex, member.name) is not None

    def _extract(self) -> None:
        """Extract symlink and blob members"""
        with tarfile.open(self._layer.path, "r") as tar:
            for member in tar:
                if self.is_rpm_src(member):
                    self._symlink_member = member
                elif self.is_extra_src(member):
                    self._symlink_member = member
                    fo = tar.extractfile(member)
                    try:
                        with tarfile.open(fileobj=fo, mode="r") as extra_src_tar:
                            files = [m for m in extra_src_tar.getmembers() if m.isreg()]
                            self._extra_source = files[0]
                    finally:
                        fo.close()  # type: ignore
                elif self._is_blob_file(member):
                    self._blob_member = member

    def _check(self) -> None:
        """Check if expected members are found

        A layer generated by BSI must have symlink and blob members, and the
        symlink member links to the blob one.
        """
        err_prefix = "Invalid layer generated by BuildSourceImage."
        if self.symlink_member is None:
            raise ValueError(f"{err_prefix} No symlink member is found.")
        if self.is_extra_src(self.symlink_member) and self.extra_source is None:
            raise ValueError(f"{err_prefix} Missing extra source.")
        if self.blob_member is None:
            raise ValueError(f"{err_prefix} No blob member is found.")

        dir_name, _ = os.path.split(self.symlink_member.name)
        normalized_link_path = os.path.normpath(
            os.path.join(dir_name, self.symlink_member.linkname)
        )
        if normalized_link_path != os.path.normpath(self.blob_member.name):
            raise ValueError(
                f"{err_prefix} Symlink {self.symlink_member.name} does not link to the blob."
            )

    def hash_key(self):
        if self.extra_source:
            artifact_name = self.extra_source.name
        else:
            artifact_name = self.symlink_member.name
        return self.blob_member.name, artifact_name

    def __hash__(self):
        return hash(self.hash_key())

    def __eq__(self, other: object) -> bool:
        """Check if this layer contains same content of the other"""
        if not isinstance(other, BSILayer):
            return False
        return self.hash_key() == other.hash_key()


def merge_image(parent_sources_dir: str, local_source_build: str) -> None:
    """Merge parent sources into the local source build

    Layers and associated data are prepended into the local source build and
    kept the same order as in the parent source container.

    :param parent_sources_dir: str, merge sources from this image to another.
    :param local_source_build: str, sources are merged into this image. Both of
        ``parent_sources_dir`` and ``local_source_build`` are directory paths
        holding sources in OCI image layout format.
    """

    parent_image = OCIImage(parent_sources_dir)
    local_build = OCIImage(local_source_build)

    parent_image_manifest = parent_image.index.manifests()[0]
    local_build_manifest = local_build.index.manifests()[0]
    for layer in reversed(parent_image_manifest.layers):
        copy_dest_path = Path(local_build.path, "blobs", *layer.descriptor["digest"].split(":"))
        shutil.copyfile(layer.path, copy_dest_path)
        logger.debug("copy layer %s to %s", layer.path, copy_dest_path)
        local_build_manifest.prepend_layer(layer)

    parent_image_config = parent_image_manifest.config
    local_build_config = local_build_manifest.config

    for diff_id in reversed(parent_image_config.diff_ids):
        local_build_config.diff_ids.insert(0, diff_id)

    n = len(parent_image_config.diff_ids)
    logger.debug("write diff_ids into local source build:\n%r", local_build_config.diff_ids[0:n])

    for history in reversed(parent_image_config.history):
        local_build_config.history.insert(0, history)

    n = len(parent_image_config.history)
    logger.debug("write history into local source build:\n%r", local_build_config.history[0:n])

    local_build.index.save()


def deduplicate_sources(parent_sources_dir: StrPath, image_output_dir: StrPath) -> None:
    """Remove duplicate sources from local build

    BuildSourceImage generates a layer blob from a tar archive that consists of
    a symlink and linked the real SRPM or extra source tar archive. For example:

    ./blobs/sha256/checksum-computed-from-SRPM-file
    ./rpm_dir/requests-version.src.rpm

    Then, if both layer blobs have the same symlink name and the blob filename,
    they are treated as the same. Note, the comparison is not based on the
    layer digest.

    :param parent_sources_dir: str, parent sources are downloaded into this directory.
    :param image_output_dir: str, local source build output directory.
    """

    parent_source_image = OCIImage(parent_sources_dir)
    local_source_build = OCIImage(image_output_dir)

    parent_image_manifest = parent_source_image.index.manifests()[0]
    local_build_manifest = local_source_build.index.manifests()[0]

    parent_bsi_layers = set(BSILayer(layer) for layer in parent_image_manifest.layers)

    for local_build_layer in local_build_manifest.layers:
        if BSILayer(local_build_layer) not in parent_bsi_layers:
            continue
        d, diff_id, history = local_build_manifest.remove_layer(local_build_layer)
        logger.debug(
            "parent sources include source %r, remove it from local source build. "
            "diff_id: %s, history: %r",
            d,
            diff_id,
            history,
        )
        break

    local_source_build.index.save()


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

    parent_sources_dir = ""
    if args.base_images:
        base_images: list[str] = args.base_images.splitlines()
        if len(base_images) > 1:
            logger.info("Multiple base images are specified: %r", base_images)
        base_image = base_images[-1]

        source_image = resolve_source_image(base_image, args.registry_allowlist)
        if source_image:
            parent_sources_dir = download_parent_image_sources(source_image, work_dir)
        else:
            logger.info("Source image is not resolved for image %s", base_image)
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

    image_output_dir = build_source_image_in_local(args.bsi, work_dir, sib_dirs)
    if parent_sources_dir:
        if build_result["dependencies_included"]:
            deduplicate_sources(parent_sources_dir, image_output_dir)
        merge_image(parent_sources_dir, image_output_dir)
        build_result["base_image_source_included"] = True

    image_digest = push_to_registry(image_output_dir, dest_images)
    build_result["image_digest"] = image_digest
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
