"""Fixture-scoped parity against MapConverter.cpp from the pinned source lock."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from standalone_sg.mapping import project_leaves


ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "locks/sources.json").read_text(encoding="utf-8"))
MAPCONVERSION_LOCK = next(
    entry for entry in LOCK["submodules"] if entry["name"] == "Map-Conversion-3D-Voxel-Map-to-2D-Occupancy-Map"
)
SOURCE_ROOT = Path(
    os.environ.get(
        "STANDALONE_SG_MAPCONVERSION_SOURCE",
        str((ROOT / MAPCONVERSION_LOCK["checkout"]).resolve()),
    )
)
SOURCE_INCLUDE = SOURCE_ROOT / "mapconversion/include"
SOURCE_IMPL = SOURCE_ROOT / "mapconversion/src/MapConverter.cpp"
FIXTURES = (
    ROOT / "tests/fixtures/m3_mapconversion_reference.json",
    ROOT / "tests/fixtures/m3_mapconversion_disjoint_ranges.json",
    ROOT / "tests/fixtures/m3_mapconversion_resolution005.json",
)


@pytest.fixture(scope="session")
def mapconverter_reference_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("g++") is None:
        pytest.skip("g++ is unavailable")
    if not SOURCE_IMPL.exists() or not (SOURCE_INCLUDE / "MapConverter.hh").exists():
        pytest.skip("pinned MapConversion checkout is unavailable")
    source_commit = subprocess.check_output(
        ["git", "-C", str(SOURCE_ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    if source_commit != MAPCONVERSION_LOCK["commit"]:
        pytest.fail(
            f"MapConversion checkout commit {source_commit} does not match lock "
            f"{MAPCONVERSION_LOCK['commit']}"
        )
    eigen_include = Path(os.environ.get("EIGEN3_INCLUDE_DIR", "/usr/include/eigen3"))
    if not (eigen_include / "Eigen/Dense").exists():
        pytest.skip("Eigen3 headers are unavailable")

    work = tmp_path_factory.mktemp("mapconverter-reference")
    harness = work / "mapconverter_probe.cpp"
    harness.write_text(
        r'''
#include "MapConverter.hh"
#include <iomanip>
#include <iostream>
#include <limits>
#include <vector>

int main() {
  double resolution, min_z;
  int min_occupancy;
  double min_x, max_x, min_y, max_y, min_leaf_z, max_leaf_z;
  std::size_t count;
  if (!(std::cin >> resolution >> min_z >> min_occupancy >> min_x >> max_x
                 >> min_y >> max_y >> min_leaf_z >> max_leaf_z >> count)) return 2;
  std::vector<voxel> leaves;
  leaves.reserve(count);
  for (std::size_t i = 0; i < count; ++i) {
    voxel item;
    int occupied;
    std::cin >> item.position.x >> item.position.y >> item.position.z
             >> item.halfSize >> occupied;
    item.occupied = occupied != 0;
    leaves.push_back(item);
  }
  MapConverter converter(resolution, 1, min_z, min_occupancy);
  converter.updateMap(leaves, {min_x, max_x, min_y, max_y, min_leaf_z, max_leaf_z});
  std::cout << std::setprecision(17)
            << converter.map.sizeX() << " " << converter.map.sizeY() << " "
            << converter.map.offsetX() << " " << converter.map.offsetY() << " "
            << converter.map.getResulution() << "\n";
  for (int y = 0; y < converter.map.sizeY(); ++y) {
    for (int x = 0; x < converter.map.sizeX(); ++x) {
      std::cout << x << " " << y << " "
                << converter.map.offsetX() + x * resolution << " "
                << converter.map.offsetY() + y * resolution << " "
                << converter.map.get(x, y) << " "
                << converter.map.getHeight(x, y) << " "
                << converter.map.getHeightTop(x, y) << "\n";
    }
  }
}
''',
        encoding="utf-8",
    )
    binary = work / "mapconverter_probe"
    subprocess.run(
        [
            "g++",
            "-std=c++14",
            "-O2",
            "-I",
            str(SOURCE_INCLUDE),
            "-I",
            str(eigen_include),
            str(harness),
            str(SOURCE_IMPL),
            "-o",
            str(binary),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return binary


def _run_source(binary: Path, fixture: dict) -> tuple[dict, list[dict]]:
    min_x, max_x, min_y, max_y = fixture["bounds_xy"]
    xs = [leaf["center"][0] for leaf in fixture["leaves"]]
    ys = [leaf["center"][1] for leaf in fixture["leaves"]]
    zs = [leaf["center"][2] for leaf in fixture["leaves"]]
    payload = [
        f"{fixture['resolution_m']} {fixture['min_free_height_m']} "
        f"{fixture['minimum_occupancy']} {min_x} {max_x} {min_y} {max_y} "
        f"{min(zs)} {max(zs)} {len(fixture['leaves'])}"
    ]
    payload.extend(
        f"{leaf['center'][0]} {leaf['center'][1]} {leaf['center'][2]} "
        f"{leaf['size'] / 2.0} {int(leaf['occupied'])}"
        for leaf in fixture["leaves"]
    )
    lines = subprocess.check_output(
        [str(binary)], input="\n".join(payload) + "\n", text=True
    ).splitlines()
    width, height, origin_x, origin_y, resolution = map(float, lines[0].split())
    rows = []
    for line in lines[1:]:
        x, y, wx, wy, value, floor, ceiling = line.split()
        rows.append(
            {
                "index": (int(x), int(y)),
                "world_xy": (float(wx), float(wy)),
                "value": int(value),
                "z_floor": float(floor),
                "z_ceiling": float(ceiling),
            }
        )
    return {
        "shape_xy": (int(width), int(height)),
        "origin_xy": (origin_x, origin_y),
        "resolution": resolution,
    }, rows


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda path: path.stem)
def test_python_port_matches_pinned_mapconverter_on_fixture_intersection(
    mapconverter_reference_binary: Path, fixture_path: Path
) -> None:
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    source_meta, source_rows = _run_source(mapconverter_reference_binary, fixture)
    min_x, max_x, min_y, max_y = fixture["bounds_xy"]
    grid = project_leaves(
        fixture["leaves"],
        resolution_m=fixture["resolution_m"],
        min_free_height_m=fixture["min_free_height_m"],
        minimum_occupancy=fixture["minimum_occupancy"],
        bounds_xy=tuple(fixture["bounds_xy"]),
        revision=fixture.get("revision", 0),
    )

    compared = 0
    for source_cell in source_rows:
        world_x, world_y = source_cell["world_xy"]
        col = round((world_x - min_x) / grid.resolution_m)
        row = round((world_y - min_y) / grid.resolution_m)
        if not (0 <= row < grid.shape[0] and 0 <= col < grid.shape[1]):
            continue
        assert world_x == pytest.approx(min_x + col * grid.resolution_m, abs=1e-8)
        assert world_y == pytest.approx(min_y + row * grid.resolution_m, abs=1e-8)
        assert int(grid.values[row, col]) == source_cell["value"], (
            fixture_path.name,
            source_cell,
            (row, col),
            int(grid.values[row, col]),
        )
        assert grid.z_floor[row, col] == pytest.approx(source_cell["z_floor"], abs=2e-6) if np.isfinite(source_cell["z_floor"]) else np.isnan(grid.z_floor[row, col])
        assert grid.z_ceiling[row, col] == pytest.approx(source_cell["z_ceiling"], abs=2e-6) if np.isfinite(source_cell["z_ceiling"]) else np.isnan(grid.z_ceiling[row, col])
        compared += 1

    assert compared > 0
    # This claim is intentionally fixture-scoped: Map2D changes its allocated
    # array extent while growing, so cells outside the source extent are not
    # asserted as if a partial/delta update had occurred.
    assert source_meta["resolution"] == pytest.approx(grid.resolution_m)
    assert source_meta["shape_xy"][0] > 0 and source_meta["shape_xy"][1] > 0


def test_disjoint_interval_fixture_checks_neighbour_occupation_against_source(
    mapconverter_reference_binary: Path,
) -> None:
    fixture = json.loads(FIXTURES[1].read_text(encoding="utf-8"))
    _, rows = _run_source(mapconverter_reference_binary, fixture)
    cell = next(row for row in rows if row["index"] == (5, 3))
    assert cell["value"] == 100
    free_cell = next(row for row in rows if row["index"] == (4, 3))
    assert free_cell["z_floor"] == pytest.approx(-0.001000003, abs=2e-6)
    assert free_cell["z_ceiling"] == pytest.approx(1.401000003, abs=2e-6)
