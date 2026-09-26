import json
import shutil
import subprocess

import pytest

from househunt.models import Destination
from househunt.pipeline import _within_limits
from househunt.report import COLOR_JS

GREEN = [21, 193, 21]
RED = [193, 21, 21]


def tt_color(*calls):
    script = COLOR_JS + f"console.log(JSON.stringify({json.dumps(calls)}.map(a => ttColor(...a))));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_color_scale_boundaries():
    fast, at_limit, mid, at_red, beyond, none = tt_color(
        [10, 30, 2], [30, 30, 2], [45, 30, 2], [60, 30, 2], [200, 30, 2], [None, 30, 2]
    )
    assert fast == GREEN and at_limit == GREEN
    assert at_red == RED and beyond == RED
    assert mid[0] > 150 and mid[1] > 150 and mid[2] < 50
    assert none is None


def test_transit_lower_bound_counts_against_limit():
    dest = Destination(name="Work", max_minutes={"transit": 45})
    assert not _within_limits({"Work": {"car": 50, "transit": None, "transit_at_least": 50}}, [dest])
    assert _within_limits({"Work": {"car": 20, "transit": 40}}, [dest])
    assert _within_limits({"Work": {"car": 20, "transit": None}}, [dest])
