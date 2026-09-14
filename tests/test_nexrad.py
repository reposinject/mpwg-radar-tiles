"""NEXRAD is scaffolded, not implemented."""

from __future__ import annotations

import pytest

from mpwg_radar.sources import NexradSource


def test_nexrad_is_explicitly_unimplemented():
    with pytest.raises(NotImplementedError, match="scaffolded"):
        NexradSource().load()
