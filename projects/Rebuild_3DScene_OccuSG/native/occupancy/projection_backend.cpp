#include "projection_backend.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <stdexcept>

namespace standalone_sg::occupancy {
namespace {
std::uint64_t leaf_fingerprint(const std::vector<voxel>& leaves) {
  std::uint64_t hash = 1469598103934665603ULL;
  auto add = [&hash](const void* bytes, std::size_t count) {
    const auto* data = static_cast<const unsigned char*>(bytes);
    for (std::size_t i = 0; i < count; ++i) {
      hash ^= data[i];
      hash *= 1099511628211ULL;
    }
  };
  for (const auto& leaf : leaves) {
    const double coordinates[] = {leaf.position.x, leaf.position.y, leaf.position.z, leaf.halfSize};
    add(coordinates, sizeof(coordinates));
    const std::uint8_t occupied = leaf.occupied ? 1 : 0;
    add(&occupied, sizeof(occupied));
  }
  return hash;
}
}  // namespace

ProjectionBackend::ProjectionBackend(double resolution, double min_free_height,
                                     int minimum_occupancy)
    : resolution_(resolution),
      converter_(new MapConverter(resolution, 1, min_free_height,
                                  minimum_occupancy)),
      previous_tree_(new octomap::OcTree(resolution)) {
  if (!(resolution > 0.0) || !std::isfinite(resolution)) {
    throw std::invalid_argument("projection resolution must be finite and positive");
  }
  if (!(min_free_height >= 0.0) || !std::isfinite(min_free_height)) {
    throw std::invalid_argument("projection min_free_height must be finite and non-negative");
  }
  if (minimum_occupancy < 0 || minimum_occupancy > 100) {
    throw std::invalid_argument("projection minimum_occupancy must be in 0..100");
  }
}

void ProjectionBackend::include(double x, double y, double z, Bounds& bounds,
                               bool& initialized) {
  if (!initialized) {
    bounds = Bounds{x, x, y, y, z, z};
    initialized = true;
    return;
  }
  bounds.min_x = std::min(bounds.min_x, x);
  bounds.max_x = std::max(bounds.max_x, x);
  bounds.min_y = std::min(bounds.min_y, y);
  bounds.max_y = std::max(bounds.max_y, y);
  bounds.min_z = std::min(bounds.min_z, z);
  bounds.max_z = std::max(bounds.max_z, z);
}

bool ProjectionBackend::compute_changed_bounds(const octomap::OcTree& current,
                                               Bounds& bounds) const {
  bool initialized = false;
  for (auto it = current.begin_leafs(), end = current.end_leafs(); it != end; ++it) {
    const octomap::OcTreeNode* old_node = previous_tree_->search(it.getKey());
    if (old_node != nullptr &&
        current.isNodeOccupied(*it) == previous_tree_->isNodeOccupied(old_node)) {
      continue;
    }
    include(it.getX(), it.getY(), it.getZ(), bounds, initialized);
  }
  for (auto it = previous_tree_->begin_leafs(), end = previous_tree_->end_leafs(); it != end; ++it) {
    if (current.search(it.getKey()) != nullptr) continue;
    include(it.getX(), it.getY(), it.getZ(), bounds, initialized);
  }
  return initialized;
}

void ProjectionBackend::set_metric_z_bounds(const octomap::OcTree& tree,
                                            Bounds& bounds) const {
  double min_x, min_y, min_z, max_x, max_y, max_z;
  tree.getMetricMin(min_x, min_y, min_z);
  tree.getMetricMax(max_x, max_y, max_z);
  bounds.min_z = min_z;
  bounds.max_z = max_z;
}

std::vector<voxel> ProjectionBackend::collect_leaves(
    const octomap::OcTree& tree, const Bounds* bounds) const {
  std::vector<voxel> result;
  auto append = [&result, &tree](const auto& it) {
    voxel value;
    value.position = {it.getX(), it.getY(), it.getZ()};
    value.halfSize = static_cast<float>(it.getSize() / 2.0);
    value.occupied = tree.isNodeOccupied(*it);
    result.push_back(value);
  };
  if (bounds == nullptr) {
    for (auto it = tree.begin_leafs(), end = tree.end_leafs(); it != end; ++it) {
      append(it);
    }
  } else {
    const octomap::point3d minimum(bounds->min_x, bounds->min_y, bounds->min_z);
    const octomap::point3d maximum(bounds->max_x, bounds->max_y, bounds->max_z);
    for (auto it = tree.begin_leafs_bbx(minimum, maximum),
              end = tree.end_leafs_bbx();
         it != end; ++it) {
      append(it);
    }
  }
  return result;
}

std::vector<double> ProjectionBackend::as_min_max(const Bounds& bounds) const {
  return {bounds.min_x, bounds.max_x, bounds.min_y,
          bounds.max_y, bounds.min_z, bounds.max_z};
}

ProjectionResult ProjectionBackend::capture(std::uint64_t revision,
                                            bool full_refresh,
                                            bool updated,
                                            const Bounds* bounds) const {
  ProjectionResult result;
  result.revision = revision;
  result.full_refresh = full_refresh;
  result.updated = updated;
  result.resolution = converter_->map.getResulution();
  result.origin_x = converter_->map.offsetX();
  result.origin_y = converter_->map.offsetY();
  result.width = converter_->map.sizeX();
  result.height = converter_->map.sizeY();
  if (bounds != nullptr) {
    result.has_changed_bounds = true;
    result.changed_bounds_xy = {bounds->min_x, bounds->max_x,
                                bounds->min_y, bounds->max_y};
    result.changed_bounds_xyz = {bounds->min_x, bounds->max_x,
                                 bounds->min_y, bounds->max_y,
                                 bounds->min_z, bounds->max_z};
  }
  if (result.width <= 0 || result.height <= 0) return result;
  const std::size_t count = static_cast<std::size_t>(result.width) * result.height;
  result.values.reserve(count);
  result.z_floor.reserve(count);
  result.z_ceiling.reserve(count);
  for (int y = 0; y < result.height; ++y) {
    for (int x = 0; x < result.width; ++x) {
      result.values.push_back(converter_->map.get(x, y));
      result.z_floor.push_back(converter_->map.getHeight(x, y));
      result.z_ceiling.push_back(converter_->map.getHeightTop(x, y));
    }
  }
  return result;
}

ProjectionResult ProjectionBackend::project(const octomap::OcTree& tree,
                                            std::uint64_t revision,
                                            bool full_refresh) {
  if (std::abs(tree.getResolution() - resolution_) > 1e-9) {
    throw std::invalid_argument("projection tree resolution changed");
  }

  Bounds bounds{};
  bool changed = false;
  bool updated = false;
  std::vector<voxel> input_leaves;
  if (full_refresh) {
    if (tree.size() != 0) {
      double min_x, min_y, min_z, max_x, max_y, max_z;
      tree.getMetricMin(min_x, min_y, min_z);
      tree.getMetricMax(max_x, max_y, max_z);
      bounds = Bounds{min_x, max_x, min_y, max_y, min_z, max_z};
      changed = true;
      input_leaves = collect_leaves(tree, &bounds);
      converter_->updateMap(input_leaves, as_min_max(bounds));
      updated = true;
    }
  } else if ((changed = compute_changed_bounds(tree, bounds))) {
    // map_conversion_oct_node::mapCallback overwrites computeBoundingBox's
    // Z range with the current tree's complete metric range before BBX query.
    set_metric_z_bounds(tree, bounds);
    input_leaves = collect_leaves(tree, &bounds);
    // Pinned map_conversion_oct_node calls updateMap with the new leaves in
    // the changed AABB. MapConverter returns immediately when that list is
    // empty; preserve that deletion behavior for source parity.
    if (!input_leaves.empty()) {
      converter_->updateMap(input_leaves, as_min_max(bounds));
      updated = true;
    }
  }

  previous_tree_.reset(new octomap::OcTree(tree));
  ProjectionResult result = capture(revision, full_refresh, updated, changed ? &bounds : nullptr);
  result.input_leaf_count = input_leaves.size();
  result.input_leaf_fingerprint = leaf_fingerprint(input_leaves);
  return result;
}

}  // namespace standalone_sg::occupancy
