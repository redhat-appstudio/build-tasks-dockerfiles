import argparse
import io
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
from unittest.mock import call, patch, MagicMock, Mock
from typing import Final
from subprocess import CalledProcessError
from dataclasses import dataclass
from tempfile import mkdtemp, mkstemp

import source_build
from source_build import BuildResult, SourceImageBuildDirectories

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
    repos_root = mkdtemp()
    origin_path = os.path.join(repos_root, REPO_NAME)
    os.mkdir(origin_path)
    cmds = [
        ["git", "init"],
        ["git", "config", "user.name", "tester"],
        ["git", "config", "user.email", "tester@example.com"],
        ["git", "add", "README.md", "main.py"],
        ["git", "commit", "-m", "first commit for testing"],
    ]
    with open(os.path.join(origin_path, "README.md"), "w") as f:
        f.write("Testing repo")
    with open(os.path.join(origin_path, "main.py"), "w") as f:
        f.write("import this")
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


def write_dummy_srpm(rpms_dir: str) -> None:
    """Write a dummy SRPM to the rpms_dir to make sure it's not empty."""
    with open(os.path.join(rpms_dir, "dummy.src.rpm"), "wb") as f:
        f.write(b"\xed\xab\xee\xdb")


class TestExtractBlobMember(unittest.TestCase):
    """Test extract_blob_member method"""

    tar_archive = ""
    data_file = ""
    file_content: Final = b"hello source build"

    @classmethod
    def setUpClass(cls) -> None:
        fd, cls.data_file = mkstemp("-data-file")
        os.write(fd, cls.file_content)
        os.close(fd)

        fd, cls.tar_archive = mkstemp(suffix="-tar-archive")
        os.close(fd)
        with tarfile.open(cls.tar_archive, "w:gz") as tar:
            tar.add(cls.data_file, arcname="./blobs/sha256/hash_file")

    @classmethod
    def tearDownClass(cls) -> None:
        os.unlink(cls.data_file)
        os.unlink(cls.tar_archive)

    def setUp(self) -> None:
        self.work_dir = mkdtemp()
        self.dest_dir = mkdtemp()
        self.logger = logging.getLogger("test")

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir)
        shutil.rmtree(self.dest_dir)

    def test_extract_blob_member(self):
        member = "./blobs/sha256/hash_file"
        source_build.extract_blob_member(
            self.tar_archive,
            member,
            self.dest_dir,
            "my-data-file",
            self.work_dir,
            self.logger,
        )

        extracted_file = os.path.join(self.dest_dir, "my-data-file")
        self.assertTrue(os.path.exists(extracted_file))
        with open(extracted_file, "rb") as f:
            self.assertEqual(self.file_content, f.read())

    def test_raise_error_if_extract_with_unknown_member(self):
        member = "hash_file"
        with self.assertRaises(KeyError):
            source_build.extract_blob_member(
                self.tar_archive,
                member,
                self.dest_dir,
                "my-data-file",
                self.work_dir,
                self.logger,
            )

    def test_raise_error_if_tar_archive_does_not_exist(self):
        tar_archive = "/not/exist"  # make the call fail
        with self.assertRaises(FileNotFoundError):
            source_build.extract_blob_member(
                tar_archive,
                "/member",
                "/path/to/dest",
                "new name",
                self.work_dir,
                self.logger,
            )


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


class TestBuildAndPush(unittest.TestCase):
    """Test build_and_push"""

    DEST_IMAGE: Final = "registry/org/app:sha256-1234567.src"

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

    def _assert_skopeo_copy(self, mock_run: MagicMock) -> None:
        skopeo_cmd = mock_run.mock_calls[1].args[0]
        self.assertListEqual(["skopeo", "copy"], skopeo_cmd[:2])

        skopeo_copy_parser = argparse.ArgumentParser()
        skopeo_copy_parser.add_argument("--digestfile", required=True, dest="digest_file")
        skopeo_copy_parser.add_argument("--retry-times")
        skopeo_copy_parser.add_argument("src")
        skopeo_copy_parser.add_argument("dest")
        try:
            args = skopeo_copy_parser.parse_args(skopeo_cmd[2:])
        except argparse.ArgumentTypeError:
            self.fail("skopeo-copy command format is incorrect.")
        self.assertEqual(f"oci://{self.expected_bsi_output_path}:latest-source", args.src)
        self.assertEqual(f"docker://{self.DEST_IMAGE}", args.dest)

    @patch("source_build.run")
    def test_build_and_push_all_kind_of_sources(self, run):
        build_result: BuildResult = {}
        # Compose SRPMs and extra sources
        self.sib_dirs.rpm_dir = mkdtemp()
        write_dummy_srpm(self.sib_dirs.rpm_dir)
        extra_src_dir0 = mkdtemp()
        self.sib_dirs.extra_src_dirs.append(extra_src_dir0)

        source_build.build_and_push(
            self.work_dir, self.sib_dirs, FAKE_BSI, [self.DEST_IMAGE], build_result
        )

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
    def test_build_with_srpms_only(self, run):
        build_result: BuildResult = {}
        self.sib_dirs.rpm_dir = mkdtemp()
        write_dummy_srpm(self.sib_dirs.rpm_dir)
        # extra_src_dirs is empty, which indicates that no extra source will be composed.

        source_build.build_and_push(
            self.work_dir, self.sib_dirs, FAKE_BSI, [self.DEST_IMAGE], build_result
        )

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

        self._assert_skopeo_copy(run)

    @patch("source_build.run")
    def test_build_with_extra_sources_only(self, run):
        build_result: BuildResult = {}
        # rpm_dir is empty, which indicates that no SRPMs will be composed.
        extra_src_dir0 = mkdtemp()
        self.sib_dirs.extra_src_dirs.append(extra_src_dir0)

        source_build.build_and_push(
            self.work_dir, self.sib_dirs, FAKE_BSI, [self.DEST_IMAGE], build_result
        )

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

        self._assert_skopeo_copy(run)

    @patch("source_build.run")
    def test_source_image_digest_is_included_in_result(self, run):
        digest: Final = "1234567"
        digest_file = ""

        def run_side_effect(cmd, **kwargs):
            if cmd[0] != "skopeo":
                return
            # Write image digest to digest file for verification later
            digest_file = cmd[3]
            with open(digest_file, "w") as f:
                f.write(digest)

        run.side_effect = run_side_effect
        build_result: BuildResult = {}
        self.sib_dirs.extra_src_dirs.append(mkdtemp())

        source_build.build_and_push(
            self.work_dir, self.sib_dirs, FAKE_BSI, [self.DEST_IMAGE], build_result
        )

        self.assertEqual(digest, build_result["image_digest"])
        self.assertFalse(
            os.path.exists(digest_file),
            f"temporary digest file {digest_file} is not deleted",
        )

    def test_raise_error_when_bsi_process_fails(self):
        # fake_bsi fail the process
        fd, fake_bsi = mkstemp()
        os.close(fd)
        os.unlink(fake_bsi)

        with self.assertRaises(FileNotFoundError):
            source_build.build_and_push(self.work_dir, self.sib_dirs, fake_bsi, "", {})

    @patch("source_build.run")
    @patch.dict("os.environ", {"BSI_DEBUG": "1"})
    def test_enable_bsi_debug_mode(self, run):
        source_build.build_and_push(self.work_dir, self.sib_dirs, FAKE_BSI, [self.DEST_IMAGE], {})

        bsi_cmd = run.mock_calls[0].args[0]
        args = create_bsi_cli_parser().parse_args(bsi_cmd[1:])
        self.assertTrue(args.debug_mode)


class TestPrepareBaseImageSources(unittest.TestCase):
    PARENT_IMAGE: Final = "registry.access.redhat.com/org/app:9.3-1234"

    def setUp(self) -> None:
        self.work_dir = mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.work_dir)

    @patch("source_build.run")
    def test_do_nothing_if_no_associated_source_image(self, run):
        sib_dirs = SourceImageBuildDirectories()

        skopeo_inspect_config_rv = Mock()
        skopeo_inspect_config_rv.stdout = json.dumps(
            {"config": {"Labels": {"version": "9.3", "release": "1"}}}
        )
        skopeo_inspect_raw_rv = Mock()
        skopeo_inspect_raw_rv.returncode = 1
        run.side_effect = [
            # can't find out source image by version and release
            skopeo_inspect_config_rv,
            skopeo_inspect_raw_rv,
        ]

        result = source_build.prepare_base_image_sources(self.PARENT_IMAGE, self.work_dir, sib_dirs)
        self.assertFalse(result)

    @patch("source_build.run")
    def test_nothing_is_gathered_if_parent_image_source_is_empty(self, run):
        def run_side_effect(cmd, **kwargs):
            skopeo_cmd = cmd[:2]

            if skopeo_cmd == ["skopeo", "inspect"]:
                if cmd[2] == "--config":
                    partial_config = json.dumps(
                        {"config": {"Labels": {"version": "9.3", "release": "1"}}}
                    )
                    return Mock(stdout=partial_config)
                if cmd[2] == "--raw":
                    return Mock(returncode=0)

            if skopeo_cmd == ["skopeo", "copy"]:
                # Write manifest.json to test the empty image (empty layers)
                # drop the protocol part: dir
                _, dest_dir = cmd[-1].split(":")
                with open(os.path.join(dest_dir, "manifest.json"), "w") as f:
                    json.dump(
                        {
                            "schemaVersion": 2,
                            "config": {
                                "mediaType": "application/vnd.oci.image.config.v1+json",
                                "digest": "sha256:626ca1c",
                                "size": 139,
                            },
                            "layers": [],
                        },
                        f,
                    )

        run.side_effect = run_side_effect
        sib_dirs = SourceImageBuildDirectories()

        result = source_build.prepare_base_image_sources(self.PARENT_IMAGE, self.work_dir, sib_dirs)
        self.assertFalse(result)

    @patch("source_build.run")
    @patch("tarfile.open")
    def test_layer_does_not_include_expected_content(self, tarfile_open, run):
        """
        Each layer (the tarball) generated by bsi has specific directory structure.
        This test ensures an exception does not break anything.
        The method logs something then skip it.
        """

        # Members' name that don't have expected layout of bsi directory structure
        member1 = Mock()
        member1.name = "member_1"
        member1.isfile.return_value = True
        member1.issym.return_value = False
        member2 = Mock()
        member2.name = "member_2"
        member2.isfile.return_value = False
        member2.issym.return_value = True

        mock_tar = Mock()
        mock_tar.__iter__ = Mock(return_value=iter([member1, member2]))

        # Mock tarfile context manager
        tarfile_open.return_value.__enter__.return_value = mock_tar

        def run_side_effect(cmd, **kwargs):
            skopeo_cmd = cmd[:2]

            if skopeo_cmd == ["skopeo", "inspect"]:
                if cmd[2] == "--config":
                    partial_config = json.dumps(
                        {"config": {"Labels": {"version": "9.3", "release": "1"}}}
                    )
                    return Mock(stdout=partial_config)
                if cmd[2] == "--raw":
                    return Mock(returncode=0)

            if skopeo_cmd == ["skopeo", "copy"]:
                manifest = {
                    "schemaVersion": 2,
                    "config": {
                        "mediaType": "application/vnd.oci.image.config.v1+json",
                        "digest": "sha256:626ca1c",
                        "size": 139,
                    },
                    "layers": [
                        {
                            "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                            "size": 123,
                            "digest": "sha256:e53f59cdb1f",
                        }
                    ],
                }
                _, dest_dir = cmd[-1].split(":")
                with open(os.path.join(dest_dir, "manifest.json"), "w") as f:
                    json.dump(manifest, f)

        run.side_effect = run_side_effect
        sib_dirs = SourceImageBuildDirectories()

        with self.assertLogs("source-build") as logs:
            self.assertFalse(
                source_build.prepare_base_image_sources(self.PARENT_IMAGE, self.work_dir, sib_dirs)
            )

        logs_content = "\n".join(logs.output)
        self.assertEqual(1, logs_content.count("No known operation happened on layer e53f59cdb1f"))

    @patch("source_build.run")
    @patch("source_build.extract_blob_member")
    @patch("tarfile.open")
    @patch("os.unlink")
    def test_prepare_srpm_and_extra_sources(self, unlink, tarfile_open, extract_blob_member, run):
        srpm_blob_member = Mock()
        srpm_blob_member.name = "./blobs/sha256/cc4ae6a"
        srpm_blob_member.isfile.return_value = True
        srpm_blob_member.issym.return_value = False
        srpm_symlink_member = Mock()
        srpm_symlink_member.name = "./rpm_dir/gdb-7.6.1-100.el7.src.rpm"
        srpm_symlink_member.isfile.return_value = False
        srpm_symlink_member.issym.return_value = True

        mock_tar = Mock()
        mock_tar.__iter__ = Mock(return_value=iter([srpm_blob_member, srpm_symlink_member]))

        # Mock tarfile context manager
        tarfile_open_layer_1 = MagicMock()
        tarfile_open_layer_1.__enter__.return_value = mock_tar

        extra_src_blob_member = Mock()
        extra_src_blob_member.name = "./blobs/sha256/cb0a4c2"
        extra_src_blob_member.isfile.return_value = True
        extra_src_blob_member.issym.return_value = False
        extra_src_symlink_member = Mock()
        extra_src_symlink_member.name = "./extra_src_dir/extra-src-0.tar"
        extra_src_symlink_member.isfile.return_value = False
        extra_src_symlink_member.issym.return_value = True

        mock_tar = Mock()
        mock_tar.__iter__ = Mock(
            return_value=iter([extra_src_blob_member, extra_src_symlink_member])
        )

        # Mock tarfile context manager
        tarfile_open_layer_2 = MagicMock()
        tarfile_open_layer_2.__enter__.return_value = mock_tar

        tarfile_open.side_effect = [tarfile_open_layer_1, tarfile_open_layer_2]

        def run_side_effect(cmd, **kwargs):
            skopeo_cmd = cmd[:2]

            if cmd[2] == "--config":
                partial_config = json.dumps(
                    {"config": {"Labels": {"version": "9.3", "release": "1"}}}
                )
                return Mock(stdout=partial_config)

            if cmd[2] == "--raw":
                rv = Mock()
                rv.returncode = 0
                return rv

            if skopeo_cmd == ["skopeo", "copy"]:
                # let_it_gather_parent_image_sources(dest_dir, tarfile_open)
                manifest = {
                    "schemaVersion": 2,
                    "config": {
                        "mediaType": "application/vnd.oci.image.config.v1+json",
                        "digest": "sha256:626ca1c",
                        "size": 139,
                    },
                    "layers": [
                        {
                            "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                            "size": 123,
                            "digest": "sha256:e53f59cdb1f",
                        },
                        {
                            "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                            "size": 456,
                            "digest": "sha256:d1a1af5ed0ce",
                        },
                    ],
                }
                _, dest_dir = cmd[-1].split(":")
                with open(os.path.join(dest_dir, "manifest.json"), "w") as f:
                    json.dump(manifest, f)

        run.side_effect = run_side_effect
        sib_dirs = SourceImageBuildDirectories()

        result = source_build.prepare_base_image_sources(self.PARENT_IMAGE, self.work_dir, sib_dirs)
        self.assertTrue(result)

        # The original extra-src-N.tar file should be removed
        unlink.assert_called_once()
        removed_extra_src_archive = unlink.mock_calls[0].args[0]
        self.assertEqual("extra-src-0.tar", os.path.basename(removed_extra_src_archive))

        extraction_dir = os.path.join(self.work_dir, "base_image_sources", "extraction_dir")
        used_log = logging.getLogger("source-build.base-image-sources")

        extract_srpm_call = call(
            "e53f59cdb1f",
            srpm_blob_member.name,
            sib_dirs.rpm_dir,
            rename_to=os.path.basename(srpm_symlink_member.name),
            work_dir=extraction_dir,
            log=used_log,
        )

        extra_src_dest_dir = os.path.join(
            extraction_dir, "extra_src_dir", os.path.basename(extra_src_symlink_member.name)
        )
        extract_extra_src_call = call(
            "d1a1af5ed0ce",
            extra_src_blob_member.name,
            extra_src_dest_dir,
            rename_to=os.path.basename(extra_src_symlink_member.name),
            work_dir=extraction_dir,
            log=used_log,
        )

        extract_blob_member.assert_has_calls([extract_srpm_call, extract_extra_src_call])


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

    def _test_include_sources(
        self,
        include_prefetched_sources: bool = False,
        mock_tarfile_open: MagicMock = None,
        parent_images: str = "",
        expect_parent_image_sources_included: bool = False,
        expect_prefetched_rpms_included: bool = False,
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
                    return Mock(
                        stdout=json.dumps(
                            {"config": {"Labels": {"version": "9.3", "release": "1"}}}
                        )
                    )

                if cmd[2] == "--raw":
                    # Indicate the source image of parent image exists
                    return Mock(returncode=0)

                if cmd[2] == "--format":
                    mock = Mock()
                    mock.stdout = self.BINARY_IMAGE_MANIFEST_DIGEST
                    return mock

            if run_cmd == ["skopeo", "copy"]:
                if cmd[2] == "--digestfile":
                    # copy for pushing the source image to registry
                    digest_file = cmd[3]
                    with open(digest_file, "w") as f:
                        f.write(self.FAKE_IMAGE_DIGEST)

                    pushed_images.append(cmd[-1].removeprefix("docker://"))
                else:
                    # copy for download parent image sources
                    # to simulate the download, write manifest.json and mock the tarfile.open
                    manifest = {
                        "schemaVersion": 2,
                        "config": {
                            "mediaType": "application/vnd.oci.image.config.v1+json",
                            "digest": "sha256:626ca1c",
                            "size": 139,
                        },
                        "layers": [
                            {
                                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                                "size": 123,
                                "digest": "sha256:e53f59cdb1f",  # map to the following mock tar
                            },
                        ],
                    }
                    _, image_download_dir = cmd[-1].split(":")
                    with open(os.path.join(image_download_dir, "manifest.json"), "w") as f:
                        json.dump(manifest, f)

                    srpm_blob_member = Mock()
                    srpm_blob_member.name = "./blobs/sha256/cc4ae6a"
                    srpm_blob_member.isfile.return_value = True
                    srpm_blob_member.issym.return_value = False
                    srpm_symlink_member = Mock()
                    srpm_symlink_member.name = "./rpm_dir/gdb-7.6.1-100.el7.src.rpm"
                    srpm_symlink_member.isfile.return_value = False
                    srpm_symlink_member.issym.return_value = True

                    mock_tar = Mock()
                    mock_tar.__iter__ = Mock(
                        return_value=iter([srpm_blob_member, srpm_symlink_member])
                    )
                    mock_tar.extractfile.return_value = io.BytesIO(b"some content")

                    # Mock tarfile context manager
                    tarfile_open_layer = MagicMock()
                    tarfile_open_layer.__enter__.return_value = mock_tar

                    # we open the tarfile twice - once to read its members, once to extract them
                    mock_tarfile_open.side_effect = [tarfile_open_layer, tarfile_open_layer]

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

                if expect_parent_image_sources_included or expect_prefetched_rpms_included:
                    self.assertIsNotNone(parser.srpms_dir)
                    self.assertTrue(os.path.exists(parser.srpms_dir))

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

    # @patch("source_build.run")
    def test_just_include_app_source(self):
        self._test_include_sources()

    def test_include_prefetched_sources(self):
        """Include prefetched pip dependencies"""
        self._test_include_sources(include_prefetched_sources=True)

    def test_include_prefetched_srpms(self):
        """Include prefetched SRPMs (and no other SRPMs)."""
        cachi2_output_dir = os.path.join(self.cachi2_dir, "output")
        srpm_path = "rpm/x86_64/updates-source/vim-9.1.113-1.fc38.src.rpm"
        create_fake_dep_packages_with_content(
            cachi2_output_dir,
            {srpm_path: os.urandom(4)},
        )
        try:
            self._test_include_sources(
                include_prefetched_sources=True, expect_prefetched_rpms_included=True
            )
        finally:
            os.unlink(os.path.join(cachi2_output_dir, "deps", srpm_path))

    @patch("tarfile.open")
    def test_include_parent_image_sources(self, tarfile_open):
        """
        Include sources from parent image. Like another test for gathering sources from parent
        image, go through the layers, but do not do real extraction from a tarball.
        """
        self._test_include_sources(
            mock_tarfile_open=tarfile_open,
            parent_images="\ngolang:2\n\nregistry.access.example.com/ubi9/ubi:9.3-1\n",
            expect_parent_image_sources_included=True,
        )

    @patch("tarfile.open")
    def test_include_parent_image_sources_2(self, tarfile_open):
        self._test_include_sources(
            mock_tarfile_open=tarfile_open,
            parent_images="\ngolang:2\n\nregistry.access.example.com/ubi9/ubi:9.3-1@sha256:123\n",
            expect_parent_image_sources_included=True,
        )

    @patch("tarfile.open")
    def test_include_all_kinds_of_sources(self, tarfile_open):
        self._test_include_sources(
            include_prefetched_sources=True,
            mock_tarfile_open=tarfile_open,
            parent_images="\ngolang:2\n\nregistry.access.example.com/ubi9/ubi:9.3-1@sha256:123\n",
            expect_parent_image_sources_included=True,
        )

    @patch("tarfile.open")
    def test_not_include_parent_image_sources_from_disallowed_registry(self, tarfile_open):
        self._test_include_sources(
            mock_tarfile_open=tarfile_open,
            parent_images=f"\ngolang:2\n\n{DISALLOWED_REGISTRY}/ubi9/ubi:9.3-1@sha256:123\n",
            expect_parent_image_sources_included=False,
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
