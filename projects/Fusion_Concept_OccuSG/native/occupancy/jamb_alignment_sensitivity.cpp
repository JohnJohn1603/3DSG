#include "MapConverter.hh"

#include <cmath>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

struct RunResult { int value; double floor, ceiling, origin_x, origin_y; };

RunResult project(const std::vector<voxel>& original, const std::vector<double>& bounds,
                  double dx, double dy, double dz) {
  std::vector<voxel> shifted = original;
  for (auto& v : shifted) {
    v.position.x += dx; v.position.y += dy; v.position.z += dz;
  }
  std::vector<double> shifted_bounds = bounds;
  shifted_bounds[0] += dx; shifted_bounds[1] += dx;
  shifted_bounds[2] += dy; shifted_bounds[3] += dy;
  shifted_bounds[4] += dz; shifted_bounds[5] += dz;
  MapConverter converter(0.05, 1, 0.3, 5);
  converter.updateMap(shifted, shifted_bounds);
  const int value = converter.map.get(4.4 + dx, 1.45 + dy);
  const int ix = static_cast<int>(std::floor((4.4 + dx - converter.map.offsetX()) / 0.05));
  const int iy = static_cast<int>(std::floor((1.45 + dy - converter.map.offsetY()) / 0.05));
  return {value, converter.map.getHeight(ix, iy), converter.map.getHeightTop(ix, iy),
          converter.map.offsetX(), converter.map.offsetY()};
}

int main(int argc, char** argv) {
  try {
    if (argc != 2) throw std::runtime_error("usage: jamb_alignment_sensitivity LEAVES.txt");
    std::ifstream input(argv[1]); std::string tag; std::size_t count{};
    input >> tag >> count;
    if (!input || tag != "leaf_count") throw std::runtime_error("expected leaf_count N");
    octomap::OcTree tree(0.05);
    for (std::size_t i = 0; i < count; ++i) {
      unsigned short x{}, y{}, z{}; float log_odds{};
      input >> x >> y >> z >> log_odds;
      if (!input) throw std::runtime_error("malformed leaf row");
      tree.setNodeValue(octomap::OcTreeKey(x, y, z), log_odds, true);
    }
    tree.updateInnerOccupancy();
    std::vector<voxel> leaves;
    for (auto it = tree.begin_leafs(), end = tree.end_leafs(); it != end; ++it) {
      voxel v; v.position = {it.getX(), it.getY(), it.getZ()};
      v.halfSize = static_cast<float>(it.getSize() / 2.0);
      v.occupied = tree.isNodeOccupied(*it); leaves.push_back(v);
    }
    double x0,y0,z0,x1,y1,z1;
    tree.getMetricMin(x0,y0,z0); tree.getMetricMax(x1,y1,z1);
    const std::vector<double> bounds{x0,x1,y0,y1,z0,z1};
    const auto base = project(leaves,bounds,0,0,0);
    constexpr double shift=0.01;
    const auto moved = project(leaves,bounds,shift,shift,shift);
    std::cout << "{\"control\":\"diagnostic-only rigid translation of reduced voxel evidence\","
              << "\"offset_xyz_m\":[" << shift << ',' << shift << ',' << shift << ']'
              << ",\"baseline\":{\"value\":" << base.value << ",\"floor\":" << base.floor
              << ",\"ceiling\":" << base.ceiling << ",\"origin_xy\":[" << base.origin_x << ',' << base.origin_y << ']'
              << "},\"translated\":{\"value\":" << moved.value << ",\"floor\":" << moved.floor
              << ",\"ceiling\":" << moved.ceiling << ",\"origin_xy\":[" << moved.origin_x << ',' << moved.origin_y << ']'
              << "},\"warning\":\"projection sensitivity only; not a corrected baseline or a full-scene/camera replay\"}\n";
    return 0;
  } catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
