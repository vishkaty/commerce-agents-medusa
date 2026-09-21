"""The package runs on its own: the skills, the reference routes and the retail fixtures
are vendored, and the Claude Code working directory is under DATA_DIR, not site-packages."""

from __future__ import annotations

from pathlib import Path

import demo_common
from commerce_medusa.host.sdk_turn import packaged_skills, project_root
from commerce_medusa.settings import LabSettings

EXPECTED = {
    "shopping": {
        "customer-care", "memory-personalization", "planning-goals", "purchase-research",
        "search-discovery",
    },
    "merchant": {
        "catalog-listings", "inventory-operations", "marketing-campaigns",
        "performance-insights", "pricing-promotions",
    },
}  # fmt: skip


def test_each_role_has_its_five_skills_packaged():
    for role, names in EXPECTED.items():
        root = packaged_skills(role)
        assert {p.name for p in root.iterdir() if (p / "SKILL.md").is_file()} == names, role


def test_project_root_is_under_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "d"))
    root = project_root("shopping")
    assert root == tmp_path / "d" / "agents" / "shopping" and root.is_dir()
    assert "site-packages" not in str(root)


def test_fixtures_default_to_the_packaged_copy(tmp_path):
    packaged = LabSettings().fixtures_dir
    assert (packaged / "catalog.json").is_file() and (packaged / "merchant_metrics.json").is_file()
    elsewhere = LabSettings(commerce_agents=tmp_path)  # a checkout without the fixtures
    assert elsewhere.fixtures_dir == packaged
    checkout = tmp_path / "examples" / "retail" / "data"
    checkout.mkdir(parents=True)
    assert LabSettings(commerce_agents=tmp_path).fixtures_dir == checkout


def test_packaged_defaults_and_routes_ship_in_the_wheel():
    assert (LabSettings().data_file("policies.json")).is_file()
    assert (LabSettings().data_file("memory-seed.json")).is_file()
    location = Path(demo_common.__file__).resolve().parent
    assert location.name == "demo_common" and location.parent.name != "examples", (
        "the vendored copy, not a reference checkout"
    )
