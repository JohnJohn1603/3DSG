#include "projection_backend.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <iostream>
#include <limits>
#include <memory>
#include <vector>

namespace {

using standalone_sg::occupancy::ProjectionResult;
using Bounds = std::array<double, 6>;  // minX,maxX,minY,maxY,minZ,maxZ

struct PreparedUpdate {
  bool changed{false};
  Bounds bounds{};
  std::vector<voxel> leaves;
};

PreparedUpdate prepare_source_partial_update(const octomap::OcTree& current,
                                             const octomap::OcTree& previous) {
  PreparedUpdate update;
  update.bounds = {INFINITY, -INFINITY, INFINITY, -INFINITY, INFINITY, -INFINITY};
  auto include = [&update](double x, double y, double z) {
    update.changed = true;
    update.bounds[0] = std::min(update.bounds[0], x);
    update.bounds[1] = std::max(update.bounds[1], x);
    update.bounds[2] = std::min(update.bounds[2], y);
    update.bounds[3] = std::max(update.bounds[3], y);
    update.bounds[4] = std::min(update.bounds[4], z);
    update.bounds[5] = std::max(update.bounds[5], z);
  };

  // Mirror map_conversion_oct_node::computeBoundingBox: new leaves and
  // occupancy flips, then leaves removed from the old tree.
  for (auto it = current.begin_leafs(), end = current.end_leafs(); it != end; ++it) {
    const octomap::OcTreeNode* old_node = previous.search(it.getKey());
    if (old_node != nullptr &&
        current.isNodeOccupied(*it) == previous.isNodeOccupied(old_node)) {
      continue;
    }
    include(it.getX(), it.getY(), it.getZ());
  }
  for (auto it = previous.begin_leafs(), end = previous.end_leafs(); it != end; ++it) {
    if (current.search(it.getKey()) != nullptr) continue;
    include(it.getX(), it.getY(), it.getZ());
  }
  if (!update.changed) return update;

  // mapCallback overwrites changed Z with the complete metric Z range of the
  // new tree while preserving the changed XY box.
  double min_x, min_y, min_z, max_x, max_y, max_z;
  current.getMetricMin(min_x, min_y, min_z);
  current.getMetricMax(max_x, max_y, max_z);
  update.bounds[4] = min_z;
  update.bounds[5] = max_z;

  const octomap::point3d minimum(update.bounds[0], update.bounds[2], update.bounds[4]);
  const octomap::point3d maximum(update.bounds[1], update.bounds[3], update.bounds[5]);
  for (auto it = current.begin_leafs_bbx(minimum, maximum),
            end = current.end_leafs_bbx(); it != end; ++it) {
    voxel value;
    value.position = {it.getX(), it.getY(), it.getZ()};
    value.halfSize = static_cast<float>(it.getSize() / 2.0);
    value.occupied = current.isNodeOccupied(*it);
    update.leaves.push_back(value);
  }
  return update;
}

PreparedUpdate prepare_source_full_update(const octomap::OcTree& tree) {
  PreparedUpdate update;
  if (tree.size() == 0) return update;
  double min_x, min_y, min_z, max_x, max_y, max_z;
  tree.getMetricMin(min_x, min_y, min_z);
  tree.getMetricMax(max_x, max_y, max_z);
  update.changed = true;
  update.bounds = {min_x, max_x, min_y, max_y, min_z, max_z};
  const octomap::point3d minimum(min_x, min_y, min_z);
  const octomap::point3d maximum(max_x, max_y, max_z);
  for (auto it = tree.begin_leafs_bbx(minimum, maximum),
            end = tree.end_leafs_bbx(); it != end; ++it) {
    voxel value;
    value.position = {it.getX(), it.getY(), it.getZ()};
    value.halfSize = static_cast<float>(it.getSize() / 2.0);
    value.occupied = tree.isNodeOccupied(*it);
    update.leaves.push_back(value);
  }
  return update;
}

bool same_doubles(const std::vector<double>& left,
                  const std::vector<double>& right) {
  if (left.size() != right.size()) return false;
  for (std::size_t index = 0; index < left.size(); ++index) {
    if (std::isnan(left[index]) && std::isnan(right[index])) continue;
    if (left[index] != right[index]) return false;
  }
  return true;
}

bool matches_map(const ProjectionResult& actual, MapConverter& reference) {
  if (actual.width != reference.map.sizeX() ||
      actual.height != reference.map.sizeY() ||
      actual.origin_x != reference.map.offsetX() ||
      actual.origin_y != reference.map.offsetY()) {
    return false;
  }
  std::vector<int> values;
  std::vector<double> floors;
  std::vector<double> ceilings;
  for (int y = 0; y < actual.height; ++y) {
    for (int x = 0; x < actual.width; ++x) {
      values.push_back(reference.map.get(x, y));
      floors.push_back(reference.map.getHeight(x, y));
      ceilings.push_back(reference.map.getHeightTop(x, y));
    }
  }
  return actual.values == values && same_doubles(actual.z_floor, floors) &&
         same_doubles(actual.z_ceiling, ceilings);
}

bool matches_bounds(const ProjectionResult& actual, const PreparedUpdate& expected) {
  if (actual.has_changed_bounds != expected.changed) return false;
  if (!expected.changed) return true;
  const std::array<double, 6> actual_bounds = {
      actual.changed_bounds_xyz[0], actual.changed_bounds_xyz[1],
      actual.changed_bounds_xyz[2], actual.changed_bounds_xyz[3],
      actual.changed_bounds_xyz[4], actual.changed_bounds_xyz[5]};
  return actual_bounds == expected.bounds;
}

void set_node_state(octomap::OcTree& tree, const octomap::OcTreeKey& key,
                    bool occupied) {
  tree.setNodeValue(key, occupied ? 1.0F : -1.0F);
}

}  // namespace

int main() {
  octomap::OcTree tree(0.1);
  for (int x = 0; x < 5; ++x) {
    for (int y = 0; y < 5; ++y) {
      for (int z = 0; z < 6; ++z) {
        const bool occupied = (x + y + z) % 2 != 0;
        tree.updateNode(octomap::point3d(0.05F + x * 0.1F,
                                         0.05F + y * 0.1F,
                                         0.05F + z * 0.1F),
                        occupied);
      }
    }
  }

  standalone_sg::occupancy::ProjectionBackend backend(0.1, 0.3, 5);
  MapConverter reference(0.1, 1, 0.3, 5);
  const PreparedUpdate full_input = prepare_source_full_update(tree);
  reference.updateMap(full_input.leaves,
                      {full_input.bounds[0], full_input.bounds[1],
                       full_input.bounds[2], full_input.bounds[3],
                       full_input.bounds[4], full_input.bounds[5]});
  const auto full = backend.project(tree, 1, true);
  if (!full.updated || !matches_bounds(full, full_input) ||
      !matches_map(full, reference)) {
    std::cerr << "full-map metric-bounds callback parity failed\n";
    return 1;
  }

  // Flip one upper leaf. The reference callback must include unchanged lower
  // leaves in the same XY column by replacing changed Z with metric tree Z.
  std::unique_ptr<octomap::OcTree> previous(new octomap::OcTree(tree));
  octomap::OcTreeKey changed_key;
  double changed_x = 0.0, changed_y = 0.0, changed_z = 0.0;
  for (auto it = tree.begin_leafs(), end = tree.end_leafs(); it != end; ++it) {
    if (it.getSize() <= tree.getResolution() * 1.001 && it.getZ() > 0.35) {
      changed_key = it.getKey();
      changed_x = it.getX();
      changed_y = it.getY();
      changed_z = it.getZ();
      set_node_state(tree, changed_key, !tree.isNodeOccupied(*it));
      break;
    }
  }
  const PreparedUpdate one_flip = prepare_source_partial_update(tree, *previous);
  if (!one_flip.changed || one_flip.leaves.empty() ||
      !(one_flip.bounds[4] < changed_z) || !(one_flip.bounds[5] > changed_z)) {
    std::cerr << "partial fixture did not include unchanged Z leaves\n";
    return 2;
  }
  reference.updateMap(one_flip.leaves,
                      {one_flip.bounds[0], one_flip.bounds[1],
                       one_flip.bounds[2], one_flip.bounds[3],
                       one_flip.bounds[4], one_flip.bounds[5]});
  const auto partial_flip = backend.project(tree, 2, false);
  if (!partial_flip.updated || !matches_bounds(partial_flip, one_flip) ||
      !matches_map(partial_flip, reference)) {
    std::cerr << "partial occupancy-change callback parity failed\n";
    return 3;
  }

  // Delete one leaf while same-XY Z leaves survive. Expanded-Z BBX must still
  // be non-empty, so source callback calls updateMap.
  previous.reset(new octomap::OcTree(tree));
  octomap::OcTreeKey one_deleted_key;
  unsigned int one_deleted_depth = 0;
  double target_x = changed_x, target_y = changed_y;
  std::size_t same_column_count = 0;
  for (auto it = tree.begin_leafs(), end = tree.end_leafs(); it != end; ++it) {
    if (std::abs(it.getX() - target_x) < 1e-5 &&
        std::abs(it.getY() - target_y) < 1e-5) {
      ++same_column_count;
      one_deleted_key = it.getKey();
      one_deleted_depth = it.getDepth();
    }
  }
  if (same_column_count < 2) {
    std::cerr << "fixture lacks multiple Z leaves in one XY column\n";
    return 4;
  }
  tree.deleteNode(one_deleted_key, one_deleted_depth);
  const PreparedUpdate one_delete = prepare_source_partial_update(tree, *previous);
  if (one_delete.leaves.empty()) {
    std::cerr << "single-leaf deletion did not retain same-column BBX leaves\n";
    return 5;
  }
  reference.updateMap(one_delete.leaves,
                      {one_delete.bounds[0], one_delete.bounds[1],
                       one_delete.bounds[2], one_delete.bounds[3],
                       one_delete.bounds[4], one_delete.bounds[5]});
  const auto partial_one_delete = backend.project(tree, 3, false);
  if (!partial_one_delete.updated || !matches_bounds(partial_one_delete, one_delete) ||
      !matches_map(partial_one_delete, reference)) {
    std::cerr << "single-leaf deletion callback parity failed\n";
    return 6;
  }

  // Delete every leaf in that XY column. The callback still detects a changed
  // AABB, but the source BBX query is empty and MapConverter retains prior data.
  previous.reset(new octomap::OcTree(tree));
  std::vector<std::pair<octomap::OcTreeKey, unsigned int>> column_leaves;
  for (auto it = tree.begin_leafs(), end = tree.end_leafs(); it != end; ++it) {
    if (std::abs(it.getX() - target_x) < 1e-5 &&
        std::abs(it.getY() - target_y) < 1e-5) {
      column_leaves.emplace_back(it.getKey(), it.getDepth());
    }
  }
  if (column_leaves.empty()) return 7;
  for (const auto& leaf : column_leaves) tree.deleteNode(leaf.first, leaf.second);
  const PreparedUpdate empty_column = prepare_source_partial_update(tree, *previous);
  if (!empty_column.changed || !empty_column.leaves.empty()) {
    std::cerr << "whole-column deletion fixture did not produce an empty BBX\n";
    return 8;
  }
  const auto partial_empty_column = backend.project(tree, 4, false);
  if (partial_empty_column.updated || !matches_bounds(partial_empty_column, empty_column) ||
      !matches_map(partial_empty_column, reference)) {
    std::cerr << "empty-column deletion callback parity failed\n";
    return 9;
  }
  return 0;
}
