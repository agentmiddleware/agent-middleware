"""Require the release trigger, package metadata, and artifacts to agree."""

import os
import re
import tomllib
from pathlib import Path


def verify_release(
    event_name: str, tag: str, input_version: str, pyproject: Path, dist: Path
) -> str:
    if event_name == "push":
        if not tag.startswith("python-v"):
            raise ValueError("push tag must start with python-v")
        requested_version = tag[len("python-v") :]
    elif event_name == "workflow_dispatch":
        requested_version = input_version
    else:
        raise ValueError(f"unsupported release event: {event_name}")

    project = tomllib.loads(pyproject.read_text())["project"]
    version = project["version"]
    if not requested_version or requested_version != version:
        raise ValueError(
            f"release version {requested_version!r} does not match pyproject {version!r}"
        )

    distribution = re.sub(r"[-_.]+", "_", project["name"]).lower()
    artifacts = list(dist.iterdir())
    wheels = [path for path in artifacts if path.name.endswith(".whl")]
    sdists = [path for path in artifacts if path.name.endswith(".tar.gz")]
    if len(artifacts) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one sdist")

    wheel_parts = wheels[0].name.removesuffix(".whl").split("-")
    if len(wheel_parts) not in (5, 6) or wheel_parts[:2] != [distribution, version]:
        raise ValueError(
            f"wheel filename has the wrong distribution or version: {wheels[0].name}"
        )
    if sdists[0].name != f"{distribution}-{version}.tar.gz":
        raise ValueError(
            f"sdist filename has the wrong distribution or version: {sdists[0].name}"
        )
    return version


if __name__ == "__main__":
    try:
        version = verify_release(
            os.environ["GITHUB_EVENT_NAME"],
            os.environ.get("RELEASE_TAG", ""),
            os.environ.get("RELEASE_INPUT_VERSION", ""),
            Path("sdk/python/pyproject.toml"),
            Path("sdk/python/dist"),
        )
    except (KeyError, OSError, ValueError) as exc:
        raise SystemExit(f"Release version check failed: {exc}") from exc
    print(f"Release version verified: {version}")
