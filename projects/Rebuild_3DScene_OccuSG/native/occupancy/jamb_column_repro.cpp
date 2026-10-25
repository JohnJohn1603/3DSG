#include "projection_backend.hpp"

#include <cmath>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
struct InputLeaf {
  unsigned short key_x{}, key_y{}, key_z{};
  float log_odds{};
};

int map_index(double world, double origin, double resolution) {
  return static_cast<int>(std::floor((world - origin) / resolution));
}
}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc != 2) throw std::runtime_error("usage: jamb_column_repro LEAVES.txt");
    constexpr double resolution = 0.05;
    constexpr double min_free_height = 0.3;
    constexpr int minimum_occupancy = 5;
    constexpr double sample_x = 4.4;
    constexpr double sample_y = 1.45;

    std::ifstream input(argv[1]);
    if (!input) throw std::runtime_error("cannot open reduced leaf fixture");
    std::string tag;
    std::size_t expected_count = 0;
    input >> tag >> expected_count;
    if (tag != "leaf_count") throw std::runtime_error("fixture must start with leaf_count N");

    octomap::OcTree tree(resolution);
    std::vector<InputLeaf> leaves;
    leaves.reserve(expected_count);
    for (std::size_t i = 0; i < expected_count; ++i) {
      InputLeaf leaf;
      input >> leaf.key_x >> leaf.key_y >> leaf.key_z >> leaf.log_odds;
      if (!input) throw std::runtime_error("malformed leaf row");
      tree.setNodeValue(octomap::OcTreeKey(leaf.key_x, leaf.key_y, leaf.key_z), leaf.log_odds, true);
      leaves.push_back(leaf);
    }
    tree.updateInnerOccupancy();
    if (leaves.size() != expected_count || tree.size() != expected_count)
      throw std::runtime_error("reconstructed tree leaf count differs from fixture");

    double min_x, min_y, min_z, max_x, max_y, max_z;
    tree.getMetricMin(min_x, min_y, min_z);
    tree.getMetricMax(max_x, max_y, max_z);
    const std::vector<double> bounds{min_x, max_x, min_y, max_y, min_z, max_z};

    std::vector<voxel> source_leaves;
    source_leaves.reserve(tree.size());
    for (auto it = tree.begin_leafs(), end = tree.end_leafs(); it != end; ++it) {
      voxel leaf;
      leaf.position = {it.getX(), it.getY(), it.getZ()};
      leaf.halfSize = static_cast<float>(it.getSize() / 2.0);
      leaf.occupied = tree.isNodeOccupied(*it);
      source_leaves.push_back(leaf);
    }

    // Direct pinned MapConverter oracle over the exact reduced OcTree leaves.
    MapConverter source(resolution, 1, min_free_height, minimum_occupancy);
    source.updateMap(source_leaves, bounds);

    // Standalone runtime backend over the same tree; no scan reinsertion and no history.
    standalone_sg::occupancy::ProjectionBackend standalone(
        resolution, min_free_height, minimum_occupancy);
    const auto projected = standalone.project(tree, 1, true);

    bool arrays_match = projected.width == source.map.sizeX() &&
                        projected.height == source.map.sizeY() &&
                        std::abs(projected.origin_x - source.map.offsetX()) < 1e-12 &&
                        std::abs(projected.origin_y - source.map.offsetY()) < 1e-12;
    std::size_t value_mismatches = 0, floor_mismatches = 0, ceiling_mismatches = 0;
    if (arrays_match) {
      for (int y = 0; y < projected.height; ++y) {
        for (int x = 0; x < projected.width; ++x) {
          const std::size_t offset = static_cast<std::size_t>(y) * projected.width + x;
          if (projected.values[offset] != source.map.get(x, y)) ++value_mismatches;
          const double sf = source.map.getHeight(x, y), sc = source.map.getHeightTop(x, y);
          const double nf = projected.z_floor[offset], nc = projected.z_ceiling[offset];
          if (!(std::isnan(sf) && std::isnan(nf)) && std::abs(sf - nf) > 1e-10) ++floor_mismatches;
          if (!(std::isnan(sc) && std::isnan(nc)) && std::abs(sc - nc) > 1e-10) ++ceiling_mismatches;
        }
      }
    }

    const int sx = map_index(sample_x, projected.origin_x, resolution);
    const int sy = map_index(sample_y, projected.origin_y, resolution);
    std::cout << std::setprecision(17)
              << "{\"fixture_leaf_count\":" << expected_count
              << ",\"reconstructed_leaf_count\":" << tree.size()
              << ",\"tree_depth\":" << tree.getTreeDepth()
              << ",\"metric_bounds_xyz\":[" << min_x << ',' << max_x << ','
              << min_y << ',' << max_y << ',' << min_z << ',' << max_z << ']'
              << ",\"direct_source_and_standalone_shape_match\":" << (arrays_match ? "true" : "false")
              << ",\"value_mismatches\":" << value_mismatches
              << ",\"floor_mismatches\":" << floor_mismatches
              << ",\"ceiling_mismatches\":" << ceiling_mismatches
              << ",\"sample_world_xy\":[" << sample_x << ',' << sample_y << ']'
              << ",\"sample_index_xy\":[" << sx << ',' << sy << ']'
              << ",\"sample_source_value\":" << source.map.get(sample_x, sample_y)
              << ",\"sample_standalone_value\":" << projected.values.at(static_cast<std::size_t>(sy) * projected.width + sx)
              << ",\"sample_source_floor\":" << source.map.getHeight(sx, sy)
              << ",\"sample_source_ceiling\":" << source.map.getHeightTop(sx, sy)
              << ",\"sample_standalone_floor\":" << projected.z_floor.at(static_cast<std::size_t>(sy) * projected.width + sx)
              << ",\"sample_standalone_ceiling\":" << projected.z_ceiling.at(static_cast<std::size_t>(sy) * projected.width + sx)
              << "}\n";
    return (!arrays_match || value_mismatches || floor_mismatches || ceiling_mismatches ||
            source.map.get(sample_x, sample_y) != 0) ? 2 : 0;
  } catch (const std::exception& error) {
    std::cerr << "jamb_column_repro: " << error.what() << '\n';
    return 1;
  }
}
