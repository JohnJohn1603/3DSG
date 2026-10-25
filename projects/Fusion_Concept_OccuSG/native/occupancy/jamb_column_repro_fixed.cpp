#include "projection_backend.hpp"

#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

int main(int argc, char** argv) {
  try {
    if (argc != 2) throw std::runtime_error("usage: jamb_column_repro LEAVES.txt");
    constexpr double res = 0.05, min_free = 0.3, sample_x = 4.4, sample_y = 1.45;
    constexpr int min_occ = 5;
    std::ifstream input(argv[1]);
    std::string tag;
    std::size_t expected{};
    input >> tag >> expected;
    if (!input || tag != "leaf_count") throw std::runtime_error("expected leaf_count N");

    octomap::OcTree tree(res);
    for (std::size_t i = 0; i < expected; ++i) {
      unsigned short x{}, y{}, z{};
      float log_odds{};
      input >> x >> y >> z >> log_odds;
      if (!input) throw std::runtime_error("malformed leaf row");
      tree.setNodeValue(octomap::OcTreeKey(x, y, z), log_odds, true);
    }
    tree.updateInnerOccupancy();
    std::vector<voxel> leaves;
    for (auto it = tree.begin_leafs(), end = tree.end_leafs(); it != end; ++it) {
      voxel v;
      v.position = {it.getX(), it.getY(), it.getZ()};
      v.halfSize = static_cast<float>(it.getSize() / 2.0);
      v.occupied = tree.isNodeOccupied(*it);
      leaves.push_back(v);
    }
    if (leaves.size() != expected) throw std::runtime_error("OctoMap leaf count differs from fixture");

    double min_x, min_y, min_z, max_x, max_y, max_z;
    tree.getMetricMin(min_x, min_y, min_z);
    tree.getMetricMax(max_x, max_y, max_z);
    const std::vector<double> bounds{min_x, max_x, min_y, max_y, min_z, max_z};
    MapConverter source(res, 1, min_free, min_occ);
    source.updateMap(leaves, bounds);
    standalone_sg::occupancy::ProjectionBackend standalone(res, min_free, min_occ);
    const auto out = standalone.project(tree, 1, true);

    bool geometry_match = out.width == source.map.sizeX() && out.height == source.map.sizeY() &&
        std::abs(out.origin_x - source.map.offsetX()) < 1e-12 &&
        std::abs(out.origin_y - source.map.offsetY()) < 1e-12;
    std::size_t value_mismatch = 0, floor_mismatch = 0, ceiling_mismatch = 0;
    if (geometry_match) for (int y = 0; y < out.height; ++y) for (int x = 0; x < out.width; ++x) {
      const auto k = static_cast<std::size_t>(y) * out.width + x;
      if (out.values[k] != source.map.get(x, y)) ++value_mismatch;
      const double sf = source.map.getHeight(x, y), nf = out.z_floor[k];
      const double sc = source.map.getHeightTop(x, y), nc = out.z_ceiling[k];
      if (!(std::isnan(sf) && std::isnan(nf)) && std::abs(sf - nf) > 1e-10) ++floor_mismatch;
      if (!(std::isnan(sc) && std::isnan(nc)) && std::abs(sc - nc) > 1e-10) ++ceiling_mismatch;
    }
    const int sx = static_cast<int>(std::floor((sample_x - out.origin_x) / res));
    const int sy = static_cast<int>(std::floor((sample_y - out.origin_y) / res));
    const auto sample_offset = static_cast<std::size_t>(sy) * out.width + sx;
    std::cout << std::setprecision(17)
      << "{\"fixture_leaf_count\":" << expected << ",\"octree_leaf_count\":" << leaves.size()
      << ",\"tree_depth\":" << tree.getTreeDepth()
      << ",\"metric_bounds_xyz\":[" << min_x << ',' << max_x << ',' << min_y << ',' << max_y << ',' << min_z << ',' << max_z << ']'
      << ",\"geometry_match\":" << (geometry_match ? "true" : "false")
      << ",\"value_mismatches\":" << value_mismatch << ",\"floor_mismatches\":" << floor_mismatch
      << ",\"ceiling_mismatches\":" << ceiling_mismatch
      << ",\"sample_index_col_row\":[" << sx << ',' << sy << ']'
      << ",\"sample_source_value\":" << source.map.get(sample_x, sample_y)
      << ",\"sample_standalone_value\":" << out.values.at(sample_offset)
      << ",\"sample_source_floor\":" << source.map.getHeight(sx, sy)
      << ",\"sample_source_ceiling\":" << source.map.getHeightTop(sx, sy)
      << ",\"sample_standalone_floor\":" << out.z_floor.at(sample_offset)
      << ",\"sample_standalone_ceiling\":" << out.z_ceiling.at(sample_offset) << "}\n";
    return (!geometry_match || value_mismatch || floor_mismatch || ceiling_mismatch ||
            source.map.get(sample_x, sample_y) != 0) ? 2 : 0;
  } catch (const std::exception& e) {
    std::cerr << "jamb_column_repro: " << e.what() << '\n';
    return 1;
  }
}
