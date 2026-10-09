"""Select a fixture matching the current stable HA, never a beta fixture by accident."""

import json
import sys
from urllib.request import urlopen

from packaging.requirements import Requirement
from packaging.version import Version


def metadata(package, version=None):
    path = f"/{version}" if version else ""
    with urlopen(f"https://pypi.org/pypi/{package}{path}/json", timeout=30) as response:
        return json.load(response)


def main():
    stable = metadata("homeassistant")["info"]["version"]
    if Version(stable).is_prerelease:
        raise RuntimeError("PyPI Home Assistant default is not stable")
    fixture = "pytest-homeassistant-custom-component"
    releases = metadata(fixture)["releases"]
    versions = sorted((Version(v) for v in releases if not Version(v).is_prerelease), reverse=True)
    # Bound PyPI metadata requests; fail explicitly if no match is in the newest 40 releases.
    for version in versions[:40]:
        if not any(not f.get("yanked", False) for f in releases[str(version)]):
            continue
        info = metadata(fixture, str(version))["info"]
        for dependency in info["requires_dist"] or []:
            requirement = Requirement(dependency)
            if requirement.marker is not None and not requirement.marker.evaluate():
                continue
            if requirement.name == "homeassistant" and str(requirement.specifier) == f"=={stable}":
                print(f"{fixture}=={version}")
                print(f"homeassistant=={stable}")
                print(f"Selected HA {stable}, fixture {version}", file=sys.stderr)
                return
    raise RuntimeError(f"No matching fixture for stable HA {stable}; do not silently test beta")


if __name__ == "__main__":
    main()
