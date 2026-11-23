"""Release checks must fail before publishing mismatched artifacts."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from check_python_release import verify_release


class ReleaseVersionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.pyproject = root / "pyproject.toml"
        self.pyproject.write_text('[project]\nname = "amw-sdk"\nversion = "0.5.0"\n')
        self.dist = root / "dist"
        self.dist.mkdir()
        (self.dist / "amw_sdk-0.5.0-py3-none-any.whl").touch()
        (self.dist / "amw_sdk-0.5.0.tar.gz").touch()

    def verify(
        self, event: str = "push", tag: str = "python-v0.5.0", input_version: str = ""
    ) -> str:
        return verify_release(event, tag, input_version, self.pyproject, self.dist)

    def test_tag_release_matches(self) -> None:
        self.assertEqual(self.verify(), "0.5.0")

    def test_dispatch_release_matches(self) -> None:
        self.assertEqual(
            self.verify(event="workflow_dispatch", input_version="0.5.0"), "0.5.0"
        )

    def test_wrong_tag_or_dispatch_version_fails(self) -> None:
        for event, tag, input_version in (
            ("push", "python-v0.5.1", ""),
            ("push", "other-v0.5.0", ""),
            ("workflow_dispatch", "", "0.5.1"),
            ("workflow_dispatch", "", ""),
        ):
            with (
                self.subTest(event=event, tag=tag, input_version=input_version),
                self.assertRaises(ValueError),
            ):
                self.verify(event=event, tag=tag, input_version=input_version)

    def test_wrong_wheel_or_sdist_version_fails(self) -> None:
        for original, wrong in (
            ("amw_sdk-0.5.0-py3-none-any.whl", "amw_sdk-0.5.1-py3-none-any.whl"),
            ("amw_sdk-0.5.0.tar.gz", "amw_sdk-0.5.1.tar.gz"),
        ):
            (self.dist / original).rename(self.dist / wrong)
            with self.assertRaises(ValueError):
                self.verify()
            (self.dist / wrong).rename(self.dist / original)

    def test_missing_artifact_fails(self) -> None:
        (self.dist / "amw_sdk-0.5.0.tar.gz").unlink()
        with self.assertRaises(ValueError):
            self.verify()


if __name__ == "__main__":
    unittest.main()
