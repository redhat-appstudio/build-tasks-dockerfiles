import argparse
import hashlib
import logging
import os
import os.path
import shutil
import stat
import subprocess
import tarfile
import json
import unittest
import zipfile
from unittest.mock import patch, MagicMock, Mock
from typing import Final
from subprocess import CalledProcessError
from dataclasses import dataclass
from tempfile import mkdtemp, mkstemp
from pathlib import Path

import source_build
from source_build import BuildResult, DescriptorT, SourceImageBuildDirectories, BSILayer
from test_utils import BlobTypeString, create_simple_oci_image

import pytest


FAKE_BSI: Final = "/testing/bsi"
OUTPUT_BINARY_IMAGE: Final = "registry/ns/app:v1"
REPO_NAME: Final = "sourcebuildapp"
REGISTRY_ALLOWLIST: Final = """
registry.example.io
registry.access.example.com
"""
DISALLOWED_REGISTRY: Final = "registry.someone-hosted.io"


@dataclass
class AppSourceDirs:
    root_dir: str
    origin_dir: str
    cloned_dir: str


def create_fake_bsi_bin() -> str:
    fd, bsi = mkstemp(suffix="-fake-bsi")
    os.chmod(fd, stat.S_IXUSR)
    os.close(fd)
    return bsi


def init_app_source_repo_dir() -> AppSourceDirs:
    """Initialize application source repository directory

    Structure:
    + root (a temporary directory)
      + origin repo (as a repository hosed in remote)
      + cloned repo (as the one cloned into workspace)
    """
    nonlatin_filename: Final = "𞤀𞤣𞤤𞤢𞤥 𞤆𞤵𞤤𞤢𞤪.txt"
    repos_root = mkdtemp()
    origin_path = os.path.join(repos_root, REPO_NAME)
    os.mkdir(origin_path)
    cmds = [
        ["git", "init"],
        ["git", "config", "user.name", "tester"],
        ["git", "config", "user.email", "tester@example.com"],
        ["git", "add", "README.md", "main.py", nonlatin_filename],
        ["git", "commit", "-m", "first commit for testing"],
    ]
    with open(os.path.join(origin_path, "README.md"), "w") as f:
        f.write("Testing repo")
    with open(os.path.join(origin_path, "main.py"), "w") as f:
        f.write("import this")
    with open(os.path.join(origin_path, nonlatin_filename), "w") as f:
        f.write("test: file name includes nonlatin characters")
    for cmd in cmds:
        subprocess.run(cmd, check=True, cwd=origin_path)

    cloned_path = mkdtemp(prefix="local-clone-", dir=repos_root)
    subprocess.run(["git", "clone", origin_path, cloned_path], check=True)

    return AppSourceDirs(root_dir=repos_root, origin_dir=origin_path, cloned_dir=cloned_path)


def create_bsi_cli_parser():
    """Helper to verify the BuildSourceImage.sh command line arguments

    The idea is, tests do not expect the order of the arguments specified in the implementation.
    """

    def _clean_drivers(value: str) -> str:
        cleaned = []
        for item in value.split(","):
            if not item:
                continue
            if item != "sourcedriver_extra_src_dir" and item != "sourcedriver_rpm_dir":
                raise argparse.ArgumentTypeError(item)
            cleaned.append(item)
        return ",".join(cleaned)

    parser = argparse.ArgumentParser(description="For verifying bsi command line")
    parser.add_argument("-D", action="store_true", dest="debug_mode")
    parser.add_argument("-b", metavar="PATH", dest="base_path")
    parser.add_argument("-o", metavar="PATH", dest="output_path")
    parser.add_argument("-s", metavar="PATH", dest="srpms_dir")
    parser.add_argument("-e", metavar="PATH", action="append", dest="extra_src_dirs")
    parser.add_argument("-d", metavar="DRIVERS", dest="drivers", type=_clean_drivers)

    return parser


def create_skopeo_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="used for testing")
    subparsers = parser.add_subparsers(description="subcommands")
    copy_parser = subparsers.add_parser("copy")
    copy_parser.add_argument("--digestfile", dest="digest_file")
    copy_parser.add_argument("--retry-times")
    copy_parser.add_argument("--insecure-policy", action="store_true")
    copy_parser.add_argument("--remove-signatures", action="store_true")
    copy_parser.add_argument("src")
    copy_parser.add_argument("dest")
    return parser


def create_fake_dep_packages(cachi2_output_dir: str, deps: list[str]) -> None:
    """Create fake prefetched dependency packages

    :param str cachi2_output_dir: path to cachi2 output directory.
    :param deps: list of dependency packages. Each of them is in form package_manager/package,
        e.g. pip/requests-1.0.0.tar.gz
    :type cachi2_output_dir: list[str]
    """
    for package in deps:
        # pip/, npm/, or gomod/.../.../.../...
        pkg_mgr_path, filename = os.path.split(package)
        packages_dir = os.path.join(cachi2_output_dir, "deps", pkg_mgr_path)
        os.makedirs(packages_dir, exist_ok=True)
        if filename.endswith(".tar.gz") or filename.endswith(".tgz"):
            with tarfile.open(os.path.join(packages_dir, filename), "w:gz"):
                pass
        elif filename.endswith(".zip"):
            with zipfile.ZipFile(os.path.join(packages_dir, filename), "w"):
                pass
        else:
            with open(os.path.join(packages_dir, filename), "w") as f:
                f.write("any data")


def create_fake_dep_packages_with_content(cachi2_output_dir: str, deps: dict[str, bytes]) -> None:
    for package, content in deps.items():
        pkg_mgr_path, filename = os.path.split(package)
        packages_dir = os.path.join(cachi2_output_dir, "deps", pkg_mgr_path)
        os.makedirs(packages_dir, exist_ok=True)
        if filename.endswith(".rpm"):
            with open(os.path.join(packages_dir, filename), "wb") as f:
                f.write(b"\xed\xab\xee\xdb" + content)
        else:
            with open(os.path.join(packages_dir, filename), "wb") as f:
                f.write(content)


def create_blob(root_dir: str, data: list[bytes], type_: BlobTypeString) -> DescriptorT:
    if type_ == "config":
        media_type = "application/vnd.oci.image.config.v1+json"
    elif type_ == "manifest":
        media_type = "application/vnd.oci.image.manifest.v1+json"
    elif type_ == "layer":
        media_type = "application/vnd.oci.image.layer.v1.tar+gzip"
    else:
        raise ValueError(f"Unknown type: {type_}")

    blob_dir = Path(root_dir, "blobs", "sha256")
    blob_dir.mkdir(parents=True, exist_ok=True)

    # Create a temporary blob file firstly, then rename it after checksum is calculated.
    blob_file = Path(root_dir, "blobs", "sha256", "temp_blob_file")
    if media_type.endswith("+json"):
        blob_file.write_bytes(data[0])
    else:
        temp_data_files: list[str] = []
        with tarfile.open(blob_file, "w:gz") as tar:
            for item in data:
                fd, temp_data_file = mkstemp(dir=blob_dir, prefix="temp-data-file-")
                os.write(fd, item)
                os.close(fd)
                temp_data_files.append(temp_data_file)
                tar.add(temp_data_file, arcname=os.path.basename(temp_data_file))
        for item in temp_data_files:
            os.unlink(item)

    with blob_file.open("rb") as f:
        checksum = hashlib.sha256(f.read()).hexdigest()

    blob_file = blob_file.rename(blob_dir.joinpath(checksum))

    return {
        "mediaType": media_type,
        "digest": "sha256:" + checksum,
        "size": blob_file.stat().st_size,
    }


class TestGetRepoInfo(unittest.TestCase):
    """Test get_repo_info"""

    def setUp(self):
        self.repo_path = mkdtemp()

    def tearDown(self):
        os.rmdir(self.repo_path)

    @patch("source_build.run")
    def test_get_the_info(self, run):
        remote_urls = [
            "https://github.com/org/app.git",
            "https://github.com/org/app",
            "https://github.com/org/app/",
        ]
        for remote_url in remote_urls:
            run.side_effect = [Mock(stdout="bd2f4e5"), Mock(stdout=remote_url)]
            info = source_build.get_repo_info("/path/to/repo")
            expected_info = {
                "name": "app",
                "last_commit_sha": "bd2f4e5",
            }
            self.assertEqual(expected_info, info)

    def test_git_process_failure(self):
        with self.assertRaises(CalledProcessError):
            source_build.get_repo_info(self.repo_path)


class TestMakeSourceArchive(unittest.TestCase):
    app_source_dirs = AppSourceDirs("", "", "")
    invalid_source_dir = ""

    @classmethod
    def setUpClass(cls):
        cls.invalid_source_dir = mkdtemp()
        cls.app_source_dirs = init_app_source_repo_dir()

    @classmethod
    def tearDownClass(cls):
        os.rmdir(cls.invalid_source_dir)
        shutil.rmtree(cls.app_source_dirs.root_dir)

    def setUp(self):
        self.work_dir = mkdtemp(suffix="-workdir")

    def tearDown(self):
        subprocess.run(["git", "restore", "."], check=True, cwd=self.app_source_dirs.cloned_dir)
        shutil.rmtree(self.work_dir)

    def test_make_the_archive_and_append_as_extra_src(self):
        sib_dirs = SourceImageBuildDirectories()
        source_build.make_source_archive(self.work_dir, self.app_source_dirs.cloned_dir, sib_dirs)

        expected_dir = os.path.join(self.work_dir, "source_archive")
        self.assertListEqual([expected_dir], sib_dirs.extra_src_dirs)

        archives = os.listdir(sib_dirs.extra_src_dirs[0])
        self.assertEqual(1, len(archives), "should have only one archive")
        self.assertRegex(archives[0], rf"{REPO_NAME}-[0-9a-z]+\.tar\.gz")

    def test_make_the_archive_without_changes_made_to_repo(self):
        readme = os.path.join(self.app_source_dirs.cloned_dir, "README.md")
        with open(readme, "r") as f:
            origin_content = f.read()
        with open(readme, "w") as f:
            f.write(f"Test {self.__class__.__name__}")

        self.test_make_the_archive_and_append_as_extra_src()

        archive_dir = os.path.join(self.work_dir, "source_archive")
        archive = os.listdir(archive_dir)[0]
        with tarfile.open(os.path.join(archive_dir, archive), "r") as tar:
            member = os.path.join(archive.split(".")[0], "README.md")
            content = tar.extractfile(member).read()
        self.assertEqual(origin_content, content.decode())

    def test_git_process_fail(self):
        with self.assertRaises(CalledProcessError):
            source_build.make_source_archive(
                self.work_dir, self.invalid_source_dir, SourceImageBuildDirectories()
            )


class TestBuildSourceInLocal(unittest.TestCase):
    """Test build_source_image_in_local"""

    def setUp(self) -> None:
        self.work_dir = mkdtemp()
        self.sib_dirs = SourceImageBuildDirectories(rpm_dir="", extra_src_dirs=[])

        self.expected_bsi_base_path = os.path.join(self.work_dir, "bsi_build")
        self.expected_bsi_output_path = os.path.join(self.work_dir, "bsi_output")

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir)
        if self.sib_dirs.rpm_dir:
            shutil.rmtree(self.sib_dirs.rpm_dir)
        for item in self.sib_dirs.extra_src_dirs:
            shutil.rmtree(item)

    @patch("source_build.run")
    def test_build_with_all_kind_of_sources(self, run: MagicMock):
        # Compose SRPMs and extra sources
        self.sib_dirs.rpm_dir = mkdtemp()
        fd, _ = mkstemp(dir=self.sib_dirs.rpm_dir, suffix=".src.rpm")
        os.close(fd)

        extra_src_dir0 = mkdtemp()
        self.sib_dirs.extra_src_dirs.append(extra_src_dir0)

        source_build.build_source_image_in_local(FAKE_BSI, self.work_dir, self.sib_dirs)

        bsi_cmd = run.mock_calls[0].args[0]
        self.assertEqual(FAKE_BSI, bsi_cmd[0])

        args = create_bsi_cli_parser().parse_args(bsi_cmd[1:])
        self.assertEqual(
            sorted([source_build.BSI_DRV_RPM_DIR, source_build.BSI_DRV_EXTRA_SRC_DIR]),
            sorted(args.drivers.split(",")),
        )
        self.assertEqual(self.sib_dirs.rpm_dir, args.srpms_dir)
        self.assertEqual([extra_src_dir0], args.extra_src_dirs)

    @patch("source_build.run")
    def test_build_with_srpms_only(self, run: MagicMock):
        self.sib_dirs.rpm_dir = mkdtemp()
        fd, _ = mkstemp(dir=self.sib_dirs.rpm_dir, suffix=".src.rpm")
        os.close(fd)
        # extra_src_dirs is empty, which indicates that no extra source will be composed.

        source_build.build_source_image_in_local(FAKE_BSI, self.work_dir, self.sib_dirs)

        bsi_cmd = run.mock_calls[0].args[0]
        self.assertEqual(FAKE_BSI, bsi_cmd[0])

        args = create_bsi_cli_parser().parse_args(bsi_cmd[1:])
        self.assertEqual([source_build.BSI_DRV_RPM_DIR], [args.drivers])
        self.assertEqual(self.sib_dirs.rpm_dir, args.srpms_dir)
        self.assertTrue(os.path.exists(args.base_path))
        self.assertEqual(self.expected_bsi_base_path, args.base_path)
        self.assertTrue(os.path.exists(args.output_path))
        self.assertEqual(self.expected_bsi_output_path, args.output_path)
        self.assertIsNone(args.extra_src_dirs, "should not add extra sources")

    @patch("source_build.run")
    def test_build_with_extra_sources_only(self, run: MagicMock):
        # rpm_dir is empty, which indicates that no SRPMs will be composed.
        extra_src_dir0 = mkdtemp()
        self.sib_dirs.extra_src_dirs.append(extra_src_dir0)

        source_build.build_source_image_in_local(FAKE_BSI, self.work_dir, self.sib_dirs)

        bsi_cmd = run.mock_calls[0].args[0]
        self.assertEqual(FAKE_BSI, bsi_cmd[0])

        args = create_bsi_cli_parser().parse_args(bsi_cmd[1:])
        self.assertTrue(os.path.exists(args.base_path))
        self.assertEqual(self.expected_bsi_base_path, args.base_path)
        self.assertTrue(os.path.exists(args.output_path))
        self.assertEqual(self.expected_bsi_output_path, args.output_path)
        self.assertEqual([source_build.BSI_DRV_EXTRA_SRC_DIR], [args.drivers])
        self.assertIsNone(args.srpms_dir, "should not add SRPMs")
        self.assertListEqual([extra_src_dir0], args.extra_src_dirs)

    def test_raise_error_when_bsi_process_fails(self):
        # fake_bsi fail the process
        fd, fake_bsi = mkstemp()
        os.close(fd)
        os.unlink(fake_bsi)

        with self.assertRaises(FileNotFoundError):
            source_build.build_source_image_in_local(fake_bsi, self.work_dir, self.sib_dirs)

    @patch("source_build.run")
    @patch.dict("os.environ", {"BSI_DEBUG": "1"})
    def test_enable_bsi_debug_mode(self, run: MagicMock):
        source_build.build_source_image_in_local(FAKE_BSI, self.work_dir, self.sib_dirs)

        bsi_cmd = run.mock_calls[0].args[0]
        args = create_bsi_cli_parser().parse_args(bsi_cmd[1:])
        self.assertTrue(args.debug_mode)


class TestPushToRegistry(unittest.TestCase):
    """Test push_to_registry"""

    DEST_IMAGE: Final = "registry/org/app:sha256-1234567.src"

    def _parse_skopeo_copy_cmd(self, cmd):
        return create_skopeo_cli_parser().parse_args(cmd)

    def _assert_skopeo_copy(self, run: MagicMock, dest_images: list[str]) -> None:
        self.assertEqual(len(run.mock_calls), len(dest_images))
        for run_call, dest_image in zip(run.mock_calls, dest_images):
            skopeo_cmd = run_call.args[0]
            args = self._parse_skopeo_copy_cmd(skopeo_cmd[1:])
            self.assertEqual("oci:/path/to/image_output:latest-source", args.src)
            self.assertEqual(f"docker://{dest_image}", args.dest)

    def _skopeo_copy_run(self, cmd, **kwargs) -> None:
        self.assertListEqual(["skopeo", "copy"], cmd[0:2])
        args = self._parse_skopeo_copy_cmd(cmd[1:])
        self.assertIsNotNone(args.digest_file, "Missing digest file")
        with open(args.digest_file, "w", encoding="utf-8") as f:
            f.write("1234567")

    @patch("source_build.run")
    def test_push_to_registry(self, run: MagicMock):
        run.side_effect = self._skopeo_copy_run
        dest_images = [self.DEST_IMAGE]
        digest = source_build.push_to_registry("/path/to/image_output", dest_images)
        self._assert_skopeo_copy(run, dest_images)
        self.assertEqual(digest, "1234567")

    @patch("source_build.run")
    def test_push_multiple_images_to_registry(self, run: MagicMock):
        run.side_effect = self._skopeo_copy_run
        dest_images = [self.DEST_IMAGE, self.DEST_IMAGE + ".src"]
        source_build.push_to_registry("/path/to/image_output", dest_images)
        self._assert_skopeo_copy(run, dest_images)


class TestGatherPrefetchedSources(unittest.TestCase):
    """Test gather_prefetched_sources"""

    def setUp(self):
        self.work_dir = mkdtemp()
        self.cachi2_dir = mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.work_dir)
        shutil.rmtree(self.cachi2_dir)

    def _mark_cachi2_has_run(self):
        self.cachi2_output_dir = os.path.join(self.cachi2_dir, "output")
        os.mkdir(self.cachi2_output_dir)

    def test_prefetch_did_not_run(self):
        sib_dirs = SourceImageBuildDirectories()
        result = source_build.gather_prefetched_sources(self.work_dir, self.cachi2_dir, sib_dirs)
        self.assertFalse(result)
        self.assertListEqual([], sib_dirs.extra_src_dirs)

    def test_no_deps_in_prefetch_output_dir(self):
        sib_dirs = SourceImageBuildDirectories()
        self._mark_cachi2_has_run()
        result = source_build.gather_prefetched_sources(self.work_dir, self.cachi2_dir, sib_dirs)
        self.assertFalse(result)
        self.assertListEqual([], sib_dirs.extra_src_dirs)

    def test_include_cachi2_env_file(self):
        """
        Not all cachi2 package manager generates cachi2.env, but if there is,
        it should be included as an extra source.
        """
        sib_dirs = SourceImageBuildDirectories()
        self._mark_cachi2_has_run()
        with open(os.path.join(self.cachi2_dir, "cachi2.env"), "w") as f:
            f.write("no matter what the content is")

        result = source_build.gather_prefetched_sources(self.work_dir, self.cachi2_dir, sib_dirs)

        self.assertFalse(result)
        self.assertListEqual([os.path.join(self.work_dir, "cachi2_env")], sib_dirs.extra_src_dirs)

    def _test_gather_deps_by_package_manager(self, fetched_deps: list[str], dep_exts: list[str]):
        self._mark_cachi2_has_run()
        create_fake_dep_packages(self.cachi2_output_dir, fetched_deps)

        sib_dirs = SourceImageBuildDirectories()
        result = source_build.gather_prefetched_sources(self.work_dir, self.cachi2_dir, sib_dirs)
        self.assertTrue(result)

        def _has_known_file_ext(filename: str) -> bool:
            return (
                filename.endswith(".tar.gz")
                or filename.endswith(".tgz")
                or filename.endswith(".zip")
            )

        # Remove the noise introduced by test on gomod package manager
        deps_with_known_file_ext = [item for item in fetched_deps if _has_known_file_ext(item)]

        prefetched_sources_dir = os.path.join(self.work_dir, "prefetched_sources")

        # Check the expected number of constructed directories work_dir/prefetched_sources/src-N
        expected_src_dirs = [
            os.path.join(prefetched_sources_dir, f"src-{count}")
            for count in range(len(deps_with_known_file_ext))
        ]
        self.assertListEqual(sorted(expected_src_dirs), sorted(sib_dirs.extra_src_dirs))

        gathered_deps = []  # collect the dep packages gathered by the method
        for dir_path, subdir_names, file_names in os.walk(prefetched_sources_dir):
            gathered_deps.extend(file_names)
        self.assertListEqual(
            sorted([os.path.basename(dep) for dep in deps_with_known_file_ext]),
            sorted(gathered_deps),
        )

    def _test_gather_srpm_deps(self, fetched_deps: dict[str, bytes], expected_deps: list[str]):
        self._mark_cachi2_has_run()
        create_fake_dep_packages_with_content(self.cachi2_output_dir, fetched_deps)

        sib_dirs = SourceImageBuildDirectories()
        sib_dirs.rpm_dir = mkdtemp()
        result = source_build.gather_prefetched_sources(self.work_dir, self.cachi2_dir, sib_dirs)
        self.assertTrue(result)

        gathered_srpm_deps = []  # collect the srpm dep packages gathered by the method
        for dir_path, subdir_names, file_names in os.walk(sib_dirs.rpm_dir):
            gathered_srpm_deps.extend(item for item in file_names if item.endswith(".src.rpm"))
        self.assertListEqual(
            sorted(expected_deps),
            sorted(gathered_srpm_deps),
        )

    def test_gather_pip_deps(self):
        pip_deps = ["pip/requests-1.0.0.tar.gz", "pip/Flask-1.2.3.tar.gz"]
        self._test_gather_deps_by_package_manager(pip_deps, [".tar.gz"])

    def test_gather_npm_deps(self):
        npm_deps = ["npm/has-1.0.3.tgz", "npm/express-4.18.1.tgz", "npm/bytes-3.1.2.tgz"]
        self._test_gather_deps_by_package_manager(npm_deps, [".tgz"])

    def test_gather_go_deps(self):
        gomod_deps = [
            "gomod/pkg/mod/cache/download/gopkg.in/yaml.v2/@v/v2.4.0.zip",
            "gomod/pkg/mod/cache/download/gopkg.in/yaml.v2/@v/v2.4.0.info",
            "gomod/pkg/mod/cache/download/github.com/go-logr/logr/@v/v1.2.3.lock",
        ]
        self._test_gather_deps_by_package_manager(gomod_deps, [".zip"])

    def test_gather_srpm_deps_unique(self):
        srpm_deps = {
            "output/sources/x86_64/fedora-source/gpm-1.20.7-42.fc38.src.rpm": os.urandom(4),
            "output/sources/x86_64/updates-source/vim-9.1.113-1.fc38.src.rpm": os.urandom(4),
            "output/.build-config.json": os.urandom(4),
            "output/bom.json": os.urandom(4),
            "output/x86_64/updates/vim-common-9.1.113-1.fc38.x86_64.rpm": os.urandom(4),
            "output/x86_64/updates/vim-filesystem-9.1.113-1.fc38.noarch.rpm": os.urandom(4),
            "output/x86_64/updates/vim-enhanced-9.1.113-1.fc38.x86_64.rpm": os.urandom(4),
            "output/x86_64/fedora/gpm-libs-1.20.7-42.fc38.x86_64.rpm": os.urandom(4),
        }

        self._test_gather_srpm_deps(
            srpm_deps, ["gpm-1.20.7-42.fc38.src.rpm", "vim-9.1.113-1.fc38.src.rpm"]
        )

    def test_gather_srpm_deps_collision_same_content(self):
        srpm_deps = {
            "output/sources/x86_64/fedora-source/gpm-1.20.7-42.fc38.src.rpm": os.urandom(4),
            "output/sources/x86_64/updates-source/vim-9.1.113-1.fc38.src.rpm": b"\xfd\xab\xfe\xdb",
            "output/sources/s390x/updates-source/vim-9.1.113-1.fc38.src.rpm": b"\xfd\xab\xfe\xdb",
            "output/.build-config.json": os.urandom(4),
            "output/bom.json": os.urandom(4),
            "output/x86_64/updates/vim-common-9.1.113-1.fc38.x86_64.rpm": os.urandom(4),
            "output/x86_64/updates/vim-filesystem-9.1.113-1.fc38.noarch.rpm": os.urandom(4),
            "output/x86_64/updates/vim-enhanced-9.1.113-1.fc38.x86_64.rpm": os.urandom(4),
            "output/x86_64/fedora/gpm-libs-1.20.7-42.fc38.x86_64.rpm": os.urandom(4),
        }

        self._test_gather_srpm_deps(
            srpm_deps, ["gpm-1.20.7-42.fc38.src.rpm", "vim-9.1.113-1.fc38.src.rpm"]
        )

    def test_gather_srpm_deps_collision_unique_content(self):
        srpm_deps = {
            "output/sources/x86_64/fedora-source/gpm-1.20.7-42.fc38.src.rpm": os.urandom(4),
            "output/sources/x86_64/updates-source/vim-9.1.113-1.fc38.src.rpm": b"\xfd\xab\xfe\xdb",
            "output/sources/s390x/updates-source/vim-9.1.113-1.fc38.src.rpm": b"\xcd\xab\xfe\xdb",
            "output/.build-config.json": os.urandom(4),
            "output/bom.json": os.urandom(4),
            "output/x86_64/updates/vim-common-9.1.113-1.fc38.x86_64.rpm": os.urandom(4),
            "output/x86_64/updates/vim-filesystem-9.1.113-1.fc38.noarch.rpm": os.urandom(4),
            "output/x86_64/updates/vim-enhanced-9.1.113-1.fc38.x86_64.rpm": os.urandom(4),
            "output/x86_64/fedora/gpm-libs-1.20.7-42.fc38.x86_64.rpm": os.urandom(4),
        }

        self._test_gather_srpm_deps(
            srpm_deps,
            [
                "gpm-1.20.7-42.fc38.src.rpm",
                "vim-9.1.113-1.fc38.src.rpm",
                "66ab0431683e4f376291ebd90d9f1f5e579063"
                + "e185e77f7260c341d5d3c77ff8-vim-9.1.113-1.fc38.src.rpm",
            ],
        )

    def test_all_package_managers_are_present(self):
        deps = [
            "gomod/pkg/mod/cache/download/github.com/go-logr/logr/@v/v1.2.3.lock",
            "gomod/pkg/mod/cache/download/gopkg.in/yaml.v2/@v/v2.4.0.info",
            "gomod/pkg/mod/cache/download/gopkg.in/yaml.v2/@v/v2.4.0.zip",
            "npm/bytes-3.1.2.tgz",
            "npm/express-4.18.1.tgz",
            "npm/has-1.0.3.tgz",
            "pip/requests-1.0.0.tar.gz",
        ]
        self._test_gather_deps_by_package_manager(deps, [".tar.gz", ".tgz", ".zip"])


class TestBuildProcess(unittest.TestCase):
    """Test build process primarily but not the details of every portion"""

    BINARY_IMAGE_MANIFEST_DIGEST: Final = "sha256:87e8e87"
    FAKE_IMAGE_DIGEST: Final = "40b2a5f7e477"
    PIP_PKG: Final = "requests-1.2.3.tar.gz"
    app_source_dirs = AppSourceDirs("", "", "")
    cachi2_dir = ""

    @classmethod
    def setUpClass(cls):
        cls.app_source_dirs = init_app_source_repo_dir()

        cls.cachi2_dir = mkdtemp("-cachi2")
        cachi2_output_dir = os.path.join(cls.cachi2_dir, "output")
        os.mkdir(cachi2_output_dir)
        create_fake_dep_packages(cachi2_output_dir, [os.path.join("pip", cls.PIP_PKG)])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.cachi2_dir)
        shutil.rmtree(cls.app_source_dirs.root_dir)

    def setUp(self):
        self.work_dir = mkdtemp("-test-build-process-work-dir")
        self.bsi = create_fake_bsi_bin()
        fd, self.result_file = mkstemp("-test-build-process-result-file")
        os.close(fd)

    def tearDown(self):
        shutil.rmtree(self.work_dir)
        os.unlink(self.bsi)
        os.unlink(self.result_file)

    @patch("source_build.run")
    def test_not_write_build_result_to_file(self, run):
        def run_side_effect(cmd, **kwargs):
            """Make the make_source_archive work"""
            completed_proc = Mock()
            git_cmd = cmd[:2]
            if git_cmd == ["git", "rev-parse"]:
                completed_proc.stdout = "1234567"
            elif git_cmd == ["git", "config"]:
                completed_proc.stdout = "https://githost/org/app.git"
            else:
                completed_proc.stdout = ""  # No other calls depend on the stdout
            return completed_proc

        run.side_effect = run_side_effect

        cli_cmd = [
            "source_build.py",
            "--workspace",
            self.work_dir,
            "--bsi",
            self.bsi,
            "--source-dir",
            self.app_source_dirs.cloned_dir,
            "--output-binary-image",
            OUTPUT_BINARY_IMAGE,
            "--registry-allowlist",
            REGISTRY_ALLOWLIST,
        ]
        with patch("sys.argv", cli_cmd):
            with self.assertLogs(source_build.logger) as logs:
                rc = source_build.main()

        self.assertEqual(0, rc)
        self.assertIn("Skip writing build result into a file", "\n".join(logs.output))

    def test_failure_happens_during_build(self):
        """Ensure any failure will be recorded inside build result"""

        invalid_git_repo = mkdtemp(suffix="-invalid-git-repo")
        with open(os.path.join(invalid_git_repo, "README.md"), "w") as f:
            f.write("This repo fails the build.")

        def _remove_the_invalid_git_repo():
            shutil.rmtree(invalid_git_repo)

        self.addCleanup(_remove_the_invalid_git_repo)

        cli_cmd = [
            "source_build.py",
            "--workspace",
            self.work_dir,
            "--bsi",
            self.bsi,
            "--write-result-to",
            self.result_file,
            "--source-dir",
            invalid_git_repo,
            "--output-binary-image",
            OUTPUT_BINARY_IMAGE,
            "--registry-allowlist",
            REGISTRY_ALLOWLIST,
        ]
        with patch("sys.argv", cli_cmd):
            rc = source_build.main()

        self.assertEqual(1, rc)

        with open(self.result_file, "r") as f:
            build_result: BuildResult = json.loads(f.read())

        self.assertEqual("failure", build_result["status"])
        self.assertRegex(build_result["message"], r"Command .+git.+ 128")

    def _assert_all_sources_are_merged(self, src: str, dest: str) -> None:
        """Check if all sources are merged from one image into another

        There are four check points:
        * Config ``.history``
        * Config ``.rootfs.diff_ids``
        * Manifest ``.layers``
        * Layer blobs are inside the ``dest`` image

        :param src: str, directory path to an OCI image, whose sources are merged into the
            ``dest`` image.
        :param dest: str, directory path to an OCI image, into where the sources are merged.
        """
        source_image = source_build.OCIImage(src)
        dest_image = source_build.OCIImage(dest)

        source_manifest = source_image.index.manifests()[0]
        source_config = source_manifest.config
        dest_manifest = dest_image.index.manifests()[0]
        dest_config = dest_manifest.config

        n = len(source_config.diff_ids)
        self.assertListEqual(dest_config.diff_ids[0:n], source_config.diff_ids)

        n = len(source_config.history)
        self.assertListEqual(dest_config.history[0:n], source_config.history)

        n = len(source_manifest.layers)
        self.assertListEqual(dest_manifest.layers[0:n], source_manifest.layers)

        # Ensure the blob files are copied to the destination image layout
        for layer in source_manifest.layers:
            digest = layer.descriptor["digest"]
            dest_blob = Path(dest_image.path, "blobs", *digest.split(":"))
            self.assertTrue(dest_blob.exists())

    def _test_include_sources(
        self,
        include_prefetched_sources: bool = False,
        parent_images: str = "",
        expect_parent_image_sources_included: bool = False,
        mock_nonexisting_source_image: bool = False,
        source_image_is_resolved_by_version_release: bool = True,
    ):
        """Test include various sources and app source will always be included"""

        # For checking the backward-compatible tag is pushed
        # Once the backward-compatible tag is removed, this can be removed as well.
        pushed_images: list[str] = []

        def run_side_effect(cmd, **kwargs):
            run_cmd = cmd[:2]
            if run_cmd == ["git", "rev-parse"]:
                completed_proc = Mock()
                completed_proc.stdout = "1234567"
                return completed_proc

            if run_cmd == ["git", "config"]:
                completed_proc = Mock()
                completed_proc.stdout = "https://githost/org/app.git"
                return completed_proc

            if run_cmd == ["git", "ls-files"]:
                completed_proc = Mock()
                completed_proc.stdout = "file.txt"
                return completed_proc

            if run_cmd == ["git", "show"]:
                completed_proc = Mock()
                completed_proc.stdout = "2024-03-20T21:57:06-04:00"
                return completed_proc

            if run_cmd == ["skopeo", "inspect"]:
                if parent_images:
                    dest_image = run_cmd[-1]
                    self.assertNotIn(":9.3-1", dest_image, "tag is not removed from image pullspec")

                if cmd[2] == "--config":
                    # Get image config
                    if source_image_is_resolved_by_version_release:
                        config = {"config": {"Labels": {"version": "9.3", "release": "1"}}}
                    else:
                        config = {"config": {"Labels": {}}}
                    return Mock(stdout=json.dumps(config))

                if cmd[2] == "--raw":
                    if not source_image_is_resolved_by_version_release:
                        dest_image = cmd[-1]
                        source_tag = self.BINARY_IMAGE_MANIFEST_DIGEST.replace(":", "-") + ".src"
                        self.assertTrue(dest_image.endswith(source_tag))

                    # Indicate the source image of parent image exists
                    if mock_nonexisting_source_image:
                        return Mock(returncode=1)
                    else:
                        return Mock(returncode=0)

                if cmd[2] == "--format":
                    # Get image manifest
                    return Mock(stdout=self.BINARY_IMAGE_MANIFEST_DIGEST)

            if run_cmd == ["skopeo", "copy"]:
                args = create_skopeo_cli_parser().parse_args(cmd[1:])

                if args.digest_file:
                    # copy for pushing the source image to registry
                    with open(args.digest_file, "w") as f:
                        f.write(self.FAKE_IMAGE_DIGEST)
                    pushed_images.append(args.dest.removeprefix("docker://"))
                    return

                # copy for downloading parent sources container
                if args.remove_signatures:
                    self.assertTrue(
                        args.dest.startswith("oci:"),
                        "oci: transport is not used for downloading parent sources",
                    )
                    image_download_dir = args.dest.removeprefix("oci:")
                    layers_data = [("libxml2-2.0-1.el9.src.rpm", b"1010101", "rpm_dir")]
                    create_simple_oci_image(image_download_dir, layers_data)

                return

            if run_cmd[0] == self.bsi:
                parser = create_bsi_cli_parser().parse_args(cmd[1:])

                for dir_path in parser.extra_src_dirs:
                    if dir_path.strip("/").endswith("source_archive"):
                        break
                else:
                    self.fail("app source is not gathered.")

                if include_prefetched_sources:
                    self.assertEqual(2, len(parser.extra_src_dirs))
                    for dir_path in parser.extra_src_dirs:
                        if os.path.exists(os.path.join(dir_path, "deps", "pip", self.PIP_PKG)):
                            break
                    else:
                        self.fail(f"Expected pip dependency {self.PIP_PKG} is not included.")

                # Write an OCI image as the result of bsi execution.
                layers_data = [(self.PIP_PKG, b"0101", "extra_src_dir")]
                create_simple_oci_image(parser.output_path, layers_data)

        cli_cmd = [
            "source_build.py",
            "--workspace",
            self.work_dir,
            "--bsi",
            self.bsi,
            "--source-dir",
            self.app_source_dirs.cloned_dir,
            "--output-binary-image",
            OUTPUT_BINARY_IMAGE,
            "--write-result-to",
            self.result_file,
            "--registry-allowlist",
            REGISTRY_ALLOWLIST,
        ]
        if include_prefetched_sources:
            cli_cmd.append("--cachi2-artifacts-dir")
            cli_cmd.append(self.cachi2_dir)

        if parent_images:
            cli_cmd.append("--base-images")
            cli_cmd.append(parent_images)

        with patch("sys.argv", cli_cmd):
            with patch("source_build.run") as mock_run:
                mock_run.side_effect = run_side_effect
                rc = source_build.main()

        if expect_parent_image_sources_included:
            # Check if parent sources are merged into the local source build
            parent_sources_dir = ""
            local_source_build_dir = ""
            for run_call in mock_run.mock_calls:
                cmd = run_call.args[0]
                if cmd[0] == self.bsi:
                    args = create_bsi_cli_parser().parse_args(cmd[1:])
                    local_source_build_dir = args.output_path
                elif cmd[:2] == ["skopeo", "copy"]:
                    args = create_skopeo_cli_parser().parse_args(cmd[1:])
                    if args.remove_signatures:
                        parent_sources_dir = args.dest.removeprefix("oci:")
            self.assertTrue(os.path.isdir(parent_sources_dir))
            self.assertTrue(os.path.isdir(local_source_build_dir))
            self._assert_all_sources_are_merged(parent_sources_dir, local_source_build_dir)

        self.assertEqual(0, rc)

        build_result: BuildResult
        with open(self.result_file, "r") as f:
            build_result = json.loads(f.read())
        self.assertEqual("success", build_result["status"])
        self.assertEqual(
            expect_parent_image_sources_included, build_result["base_image_source_included"]
        )
        self.assertEqual(include_prefetched_sources, build_result["dependencies_included"])
        self.assertEqual(self.FAKE_IMAGE_DIGEST, build_result["image_digest"])
        self.assertNotIn(
            "message",
            build_result,
            "this test is for successful run, result should not include message field.",
        )

        image_repo = OUTPUT_BINARY_IMAGE.split(":")[0]
        image_tag = f"{self.BINARY_IMAGE_MANIFEST_DIGEST.replace(':', '-')}.src"
        expected_source_image = f"{image_repo}:{image_tag}"
        self.assertEqual(expected_source_image, build_result["image_url"])

        self.assertListEqual([f"{OUTPUT_BINARY_IMAGE}.src", expected_source_image], pushed_images)

    def test_just_include_app_source(self):
        self._test_include_sources()

    def test_include_prefetched_sources(self):
        """Include prefetched pip dependencies"""
        self._test_include_sources(include_prefetched_sources=True)

    def test_include_parent_image_sources(self):
        """
        Include sources from parent image. Like another test for gathering sources from parent
        image, go through the layers, but do not do real extraction from a tarball.
        """
        self._test_include_sources(
            parent_images="\ngolang:2\n\nregistry.access.example.com/ubi9/ubi:9.3-1\n",
            expect_parent_image_sources_included=True,
        )

    def test_include_parent_image_sources_2(self):
        self._test_include_sources(
            parent_images="\ngolang:2\n\nregistry.access.example.com/ubi9/ubi:9.3-1@sha256:123\n",
            expect_parent_image_sources_included=True,
        )

    def test_registry_does_not_have_source_image(self):
        self._test_include_sources(
            parent_images="\ngolang:2\n\nregistry.access.example.com/ubi9/ubi:9.3-1@sha256:123\n",
            expect_parent_image_sources_included=False,
            mock_nonexisting_source_image=True,
        )

    def test_include_all_kinds_of_sources(self):
        self._test_include_sources(
            include_prefetched_sources=True,
            parent_images="\ngolang:2\n\nregistry.access.example.com/ubi9/ubi:9.3-1@sha256:123\n",
            expect_parent_image_sources_included=True,
        )

    def test_resolve_konflux_source_image(self):
        self._test_include_sources(
            # the registry is not in the allow list. Konflux source image will be resolved.
            parent_images="user-registry.io/ubi9/ubi:9.3-1@sha256:123\n",
            expect_parent_image_sources_included=True,
        )

    @patch("source_build.run")
    def test_create_a_temp_dir_as_workspace(self, run):
        def run_side_effect(cmd, **kwargs):
            """Make the make_source_archive work"""
            completed_proc = Mock()
            git_cmd = cmd[:2]
            if git_cmd == ["git", "rev-parse"]:
                completed_proc.stdout = "1234567"
            elif git_cmd == ["git", "config"]:
                completed_proc.stdout = "https://githost/org/app.git"
            else:
                completed_proc.stdout = ""  # No other calls depend on the stdout
            return completed_proc

        run.side_effect = run_side_effect

        cli_cmd = [
            "source_build.py",
            "--bsi",
            self.bsi,
            "--source-dir",
            self.app_source_dirs.cloned_dir,
            "--output-binary-image",
            OUTPUT_BINARY_IMAGE,
            "--registry-allowlist",
            REGISTRY_ALLOWLIST,
        ]
        with patch("sys.argv", cli_cmd):
            with self.assertLogs(source_build.logger, level=logging.DEBUG) as logs:
                rc = source_build.main()

        self.assertEqual(0, rc)

        for log in logs.output:
            if "workspace directory " in log:
                self.assertRegex(log, r"workspace directory .+-source-build-workspace")
                break
        else:
            self.fail("Cannot find log line logging created temporary workspace directory.")

    def test_bsi_script_has_to_exist(self):
        fd, bsi = mkstemp()
        os.close(fd)
        os.unlink(bsi)

        cli_cmd = [
            "source_build.py",
            "--workspace",
            self.work_dir,
            "--bsi",
            bsi,
            "--write-result-to",
            self.result_file,
            "--source-dir",
            self.app_source_dirs.cloned_dir,
            "--output-binary-image",
            OUTPUT_BINARY_IMAGE,
        ]
        with patch("sys.argv", cli_cmd):
            with self.assertRaises(SystemExit):
                source_build.main()

    def test_bsi_script_has_to_be_executable(self):
        fd, bsi = mkstemp()
        os.close(fd)

        def _remove_bsi():
            os.unlink(bsi)

        self.addCleanup(_remove_bsi)

        cli_cmd = [
            "source_build.py",
            "--workspace",
            self.work_dir,
            "--bsi",
            bsi,
            "--write-result-to",
            self.result_file,
            "--source-dir",
            self.app_source_dirs.cloned_dir,
            "--output-binary-image",
            OUTPUT_BINARY_IMAGE,
        ]
        with patch("sys.argv", cli_cmd):
            with self.assertRaises(SystemExit):
                source_build.main()

    def test_specified_work_dir_has_to_exist(self):
        work_dir = mkdtemp()
        os.rmdir(work_dir)

        cli_cmd = [
            "source_build.py",
            "--workspace",
            work_dir,
            "--bsi",
            self.bsi,
            "--write-result-to",
            self.result_file,
            "--source-dir",
            self.app_source_dirs.cloned_dir,
            "--output-binary-image",
            OUTPUT_BINARY_IMAGE,
        ]
        with patch("sys.argv", cli_cmd):
            with self.assertRaises(SystemExit):
                source_build.main()


class TestResolveSourceImageByVersionRelease(unittest.TestCase):
    """Test resolve_source_image_by_version_release"""

    @patch("source_build.run")
    def test_binary_image_has_no_version_or_release_label(self, run: MagicMock):
        tests = [{}, {"version": "9.3"}, {"release": "11"}]
        for labels in tests:
            mock_config = {"config": {"Labels": labels}}
            skopeo_inspect_rv = Mock()
            skopeo_inspect_rv.stdout = json.dumps(mock_config)
            run.side_effect = [skopeo_inspect_rv]

            with self.assertLogs(f"{source_build.logger.name}.resolve_source_image") as logs:
                result = source_build.resolve_source_image_by_version_release(OUTPUT_BINARY_IMAGE)
            self.assertIsNone(result)
            self.assertIn("is not labelled with version and release", "\n".join(logs.output))

    @patch("source_build.run")
    def test_image_does_not_have_source_image(self, run: MagicMock):
        skopeo_inspect_config_rv = Mock()
        skopeo_inspect_config_rv.stdout = json.dumps(
            {"config": {"Labels": {"version": "9.3", "release": "1"}}}
        )
        skopeo_inspect_raw_rv = Mock()
        skopeo_inspect_raw_rv.returncode = 1
        run.side_effect = [skopeo_inspect_config_rv, skopeo_inspect_raw_rv]

        result = source_build.resolve_source_image_by_version_release(OUTPUT_BINARY_IMAGE)
        self.assertIsNone(result)

    @patch("source_build.run")
    def test_source_image_is_resolved(self, run: MagicMock):
        skopeo_inspect_config_rv = Mock()
        skopeo_inspect_config_rv.stdout = json.dumps(
            {"config": {"Labels": {"version": "9.3", "release": "1"}}}
        )
        skopeo_inspect_raw_rv = Mock()
        skopeo_inspect_raw_rv.returncode = 0
        run.side_effect = [skopeo_inspect_config_rv, skopeo_inspect_raw_rv]

        source_image = source_build.resolve_source_image_by_version_release(OUTPUT_BINARY_IMAGE)

        expected_source_image = OUTPUT_BINARY_IMAGE.split(":")[0] + ":9.3-1-source"
        self.assertEqual(expected_source_image, source_image)


@pytest.mark.parametrize(
    "image_pullspec,expected",
    [
        ["ubuntu", ("ubuntu", "", "")],
        ["reg:3000", ("reg", "3000", "")],
        ["reg:3000/img:9.3", ("reg:3000/img", "9.3", "")],
        ["reg:3000/img:9.3@sha256:123", ("reg:3000/img", "9.3", "sha256:123")],
        ["reg/org/img:9.3@sha256:123", ("reg/org/img", "9.3", "sha256:123")],
        ["reg/org/path/img:9.3", ("reg/org/path/img", "9.3", "")],
        ["reg/org/path/img:9.3@sha256:123", ("reg/org/path/img", "9.3", "sha256:123")],
    ],
)
def test_parse_image_name(image_pullspec, expected):
    assert expected == source_build.parse_image_name(image_pullspec)


class TestDeduplicateSources(unittest.TestCase):
    """Test deduplicate_sources"""

    def setUp(self):
        self.parent_sources_dir = mkdtemp(prefix="parent-sources-")
        self.local_build_dir = mkdtemp(prefix="local-source-build-")

    def tearDown(self):
        shutil.rmtree(self.parent_sources_dir)
        shutil.rmtree(self.local_build_dir)

    def test_no_duplicate_there(self):
        create_simple_oci_image(
            self.parent_sources_dir,
            [
                ("requests-1.23-1.src.rpm", b"0101010", "rpm_dir"),
                ("flask-2.0.tar.gz", b"0001111", "extra_src_dir"),
            ],
        )

        create_simple_oci_image(
            self.local_build_dir,
            [
                ("libxml2-2.3-10.src.rpm", b"11001100", "rpm_dir"),
                ("gawk-5.1.0-6.el9.src.rpm", b"01100110", "rpm_dir"),
                ("flask-2.1.tar.gz", b"01000111", "extra_src_dir"),
            ],
        )

        source_build.deduplicate_sources(self.parent_sources_dir, self.local_build_dir)

        local_build = source_build.OCIImage(self.local_build_dir)
        manifest = local_build.index.manifests()[0]
        self.assertEqual(3, len(manifest.layers))

        parent_sources = source_build.OCIImage(self.parent_sources_dir)
        manifest = parent_sources.index.manifests()[0]
        self.assertEqual(2, len(manifest.layers))

    def test_deduplicate(self):
        create_simple_oci_image(
            self.parent_sources_dir,
            [
                ("requests-1.23-1.src.rpm", b"0101010", "rpm_dir"),
                ("flask-2.0.tar.gz", b"0001111", "extra_src_dir"),
            ],
        )

        create_simple_oci_image(
            self.local_build_dir,
            [
                ("libxml2-2.3-10.src.rpm", b"11001100", "rpm_dir"),
                ("pcre2-10.40-2.el9.src.rpm", b"100100100", "rpm_dir"),
                # this is the duplicate one to be removed
                ("flask-2.0.tar.gz", b"0001111", "extra_src_dir"),
            ],
        )

        source_build.deduplicate_sources(self.parent_sources_dir, self.local_build_dir)

        local_build: Final = source_build.OCIImage(self.local_build_dir)
        local_source_manifest: Final = local_build.index.manifests()[0]

        self.assertEqual(2, len(local_source_manifest.layers))

        expected = sorted(["libxml2-2.3-10.src.rpm", "pcre2-10.40-2.el9.src.rpm"])
        remains_in_local_build: Final = sorted(
            os.path.basename(BSILayer(layer).symlink_member.name)
            for layer in local_source_manifest.layers
        )
        self.assertListEqual(expected, remains_in_local_build)

        # Ensure parent sources remain without change
        parent_sources: Final = source_build.OCIImage(self.parent_sources_dir)
        parent_manifest: Final = parent_sources.index.manifests()[0]

        self.assertEqual(2, len(local_source_manifest.layers))

        remains_in_parent: list[str] = []
        for layer in parent_manifest.layers:
            bsi_layer = BSILayer(layer)
            if bsi_layer.extra_source:
                name = bsi_layer.extra_source.name
            else:
                name = bsi_layer.symlink_member.name
            remains_in_parent.append(os.path.basename(name))

        expected = sorted(["requests-1.23-1.src.rpm", "flask-2.0.tar.gz"])
        self.assertListEqual(expected, sorted(remains_in_parent))


class TestResolveSourceImageByManifest(unittest.TestCase):
    """Test resolve_source_image_by_manifest"""

    @patch("source_build.run")
    def test_source_image_is_resolved(self, mock_run: MagicMock):
        manifest_digest = "sha256:123456"
        skopeo_inspect_digest_rv = Mock(stdout=manifest_digest)
        skopeo_inspect_raw_rv = Mock(returncode=0)
        mock_run.side_effect = [skopeo_inspect_digest_rv, skopeo_inspect_raw_rv]

        source_image = source_build.resolve_source_image_by_manifest("registry.io:3000/ns/app:1.0")

        self.assertEqual("registry.io:3000/ns/app:sha256-123456.src", source_image)

    @patch("source_build.run")
    def test_source_image_does_not_exist(self, mock_run: MagicMock):
        manifest_digest = "sha256:123456"
        skopeo_inspect_digest_rv = Mock(stdout=manifest_digest)
        skopeo_inspect_raw_rv = Mock(returncode=1)
        mock_run.side_effect = [skopeo_inspect_digest_rv, skopeo_inspect_raw_rv]

        source_image = source_build.resolve_source_image_by_manifest("registry.io:3000/ns/app:1.0")

        self.assertIsNone(source_image)
