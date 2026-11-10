"""US market hours need the America/New_York rules on every install — also
where the operating system ships no time zone database (Windows, and
minimal Docker / cloud images). The agent builds that zone at import time,
so without the data it does not start at all."""

import os
import zoneinfo

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_tzdata_is_installed_on_every_platform():
    with open(os.path.join(REPO, "requirements.txt"), encoding="utf-8") as f:
        lines = [ln.split("#", 1)[0].strip() for ln in f]
    tz = [ln for ln in lines if ln.startswith("tzdata")]
    assert len(tz) == 1
    assert ";" not in tz[0]   # no "only on Windows" marker


def test_market_timezone_loads_without_a_system_database():
    pytest.importorskip("tzdata")   # what requirements.txt installs
    try:
        zoneinfo.reset_tzpath(to=[])   # a system with no /usr/share/zoneinfo
        ny = zoneinfo.ZoneInfo.no_cache("America/New_York")
        assert ny.key == "America/New_York"
    finally:
        zoneinfo.reset_tzpath()
