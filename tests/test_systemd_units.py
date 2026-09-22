"""Timer/unit contracts that keep RALA fresh without stopping composite."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD = ROOT / "deploy" / "systemd"


def _text(name: str) -> str:
    return (SYSTEMD / name).read_text(encoding="utf-8")


def test_rala_timer_is_two_minutes_and_does_not_replace_composite():
    timer = _text("mpwg-radar-cooker-rala.timer")
    assert "OnUnitActiveSec=2min" in timer
    assert "mpwg-radar-cooker-rala.service" in timer
    composite = _text("mpwg-radar-cooker.timer")
    assert "OnUnitActiveSec=3min" in composite
    assert "mpwg-radar-cooker.service" in composite
    assert "rala" not in composite.lower()


def _directives(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_rala_timeout_covers_archive_catchup_without_raising_composite():
    rala = _text("mpwg-radar-cooker-rala.service")
    composite = _text("mpwg-radar-cooker.service")
    rala_directives = _directives(rala)
    assert "TimeoutStartSec=4200" in rala_directives
    assert "Environment=MPWG_RALA_UPLOAD_CONCURRENCY=8" in rala_directives
    assert "Environment=MPWG_TILE_WORKERS=2" in rala_directives
    assert "Environment=MPWG_KEEP_DBZ=0" in rala_directives
    assert "TimeoutStartSec=1800" in _directives(composite)
    assert "TimeoutStartSec=4200" not in _directives(composite)
    dropin = _directives(
        _text("mpwg-radar-cooker-rala.service.d/zz-catchup-timeout.conf")
    )
    assert "TimeoutStartSec=4200" in dropin
    assert "TimeoutStartSec=1800" not in dropin
    setup = (ROOT / "deploy" / "ec2-setup.sh").read_text(encoding="utf-8")
    assert "zz-catchup-timeout.conf" in setup
    assert "mpwg-radar-cooker-rala.service.d" in setup
    assert "TimeoutStartSec=1800" in setup
    assert "mpwg-radar-cooker.service.d" not in setup


def test_rala_service_does_not_conflict_with_composite():
    unit = _text("mpwg-radar-cooker-rala.service")
    directives = _directives(unit)
    assert not any(line.startswith("Conflicts=") for line in directives)
    assert "Environment=MPWG_PRODUCT=rala" in directives
    assert any("cook --product rala" in line for line in directives)
    assert "CPUWeight=60" in directives
    composite = _directives(_text("mpwg-radar-cooker.service"))
    assert not any(line.startswith("Conflicts=") for line in composite)
    assert "CPUWeight=100" in composite
    assert not any("cook --product rala" in line for line in composite)
