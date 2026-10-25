from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def pytest_addoption(parser):
    parser.addoption(
        "--integration-resources",
        action="store_true",
        default=False,
        help="require all locked native/baseline inputs; missing resources fail before collection",
    )


def pytest_configure(config):
    import os

    from standalone_sg.resource_binding import verify_required_resources

    required = config.getoption("--integration-resources") or os.environ.get(
        "OCCUSG_REQUIRE_NATIVE_RESOURCES", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    if required:
        verify_required_resources((
            "baseline_lock", "native_m2", "native_m2_m3", "native_dude",
            "native_m3_smoke", "native_dude_source_reference",
        ))
