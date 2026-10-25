#include "occupancy_map.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>
#include <tuple>

namespace standalone_sg::occupancy {

bool Key::operator<(const Key& other) const {
  return std::tie(x, y, z) < std::tie(other.x, other.y, other.z);
}

bool Key::operator==(const Key& other) const {
  return x == other.x && y == other.y && z == other.z;
}

OccupancyMap::OccupancyMap(double resolution,
                           double prob_hit,
                           double prob_miss,
                           double clamping_min,
                           double clamping_max,
                           double occupancy_threshold)
    : tree_(resolution) {
  if (!(resolution > 0.0) || !std::isfinite(resolution)) {
    throw std::invalid_argument("OctoMap resolution must be finite and positive");
  }
  tree_.setProbHit(prob_hit);
  tree_.setProbMiss(prob_miss);
  tree_.setClampingThresMin(clamping_min);
  tree_.setClampingThresMax(clamping_max);
  tree_.setOccupancyThres(occupancy_threshold);
}

octomap::point3d OccupancyMap::as_point(const std::array<float, 3>& value) {
  for (float coordinate : value) {
    if (!std::isfinite(coordinate)) {
      throw std::invalid_argument("scan coordinates must be finite");
    }
  }
  return octomap::point3d(value[0], value[1], value[2]);
}

Key OccupancyMap::as_key(const octomap::OcTreeKey& key) {
  return Key{key[0], key[1], key[2]};
}

std::vector<Key> OccupancyMap::sorted_keys(const octomap::KeySet& keys) {
  std::vector<Key> result;
  result.reserve(keys.size());
  for (const auto& key : keys) {
    result.push_back(as_key(key));
  }
  std::sort(result.begin(), result.end());
  return result;
}

UpdateResult OccupancyMap::integrate(const Scan& scan) {
  if (!(scan.max_range > 0.0) || !std::isfinite(scan.max_range)) {
    throw std::invalid_argument("scan.max_range must be finite and positive");
  }
  const octomap::point3d origin = as_point(scan.origin);
  octomap::Pointcloud pointcloud;
  pointcloud.reserve(scan.points.size());
  std::vector<octomap::point3d> clipped_endpoints;

  for (const ScanPoint& input : scan.points) {
    const octomap::point3d endpoint = as_point(input.xyz);
    const octomap::point3d delta = endpoint - origin;
    const double distance = delta.norm();
    if (!(distance > 0.0) || !std::isfinite(distance)) {
      throw std::invalid_argument("a scan point must define a non-zero ray");
    }
    if (input.has_hit && distance <= scan.max_range) {
      pointcloud.push_back(endpoint);
      continue;
    }

    // Match the pinned insertScan path: a clipped endpoint contributes both
    // the ray keys and the voxel containing the exact max-range endpoint.
    const octomap::point3d clipped_endpoint =
        origin + delta.normalized() * static_cast<float>(scan.max_range);
    clipped_endpoints.push_back(clipped_endpoint);
    const octomap::point3d compute_endpoint =
        origin + delta.normalized() * static_cast<float>(scan.max_range + tree_.getResolution());
    pointcloud.push_back(compute_endpoint);
  }

  octomap::KeySet free_cells;
  octomap::KeySet occupied_cells;
  tree_.computeUpdate(pointcloud, origin, free_cells, occupied_cells, scan.max_range);
  for (const auto& endpoint : clipped_endpoints) {
    octomap::OcTreeKey end_key;
    if (!tree_.coordToKeyChecked(endpoint, end_key)) {
      throw std::runtime_error("could not generate key for clipped endpoint");
    }
    free_cells.insert(end_key);
  }
  for (auto it = free_cells.begin(); it != free_cells.end();) {
    if (occupied_cells.find(*it) != occupied_cells.end()) {
      it = free_cells.erase(it);
    } else {
      ++it;
    }
  }

  UpdateResult result;
  result.revision = ++revision_;
  result.free_keys = sorted_keys(free_cells);
  result.occupied_keys = sorted_keys(occupied_cells);

  // Match the baseline batch semantics: update each key once and let occupied
  // keys win over free keys. computeUpdate already makes the sets disjoint.
  for (const auto& key : free_cells) {
    tree_.updateNode(key, false, false);
  }
  for (const auto& key : occupied_cells) {
    tree_.updateNode(key, true, false);
  }

  result.observations.reserve(result.free_keys.size() + result.occupied_keys.size());
  auto append_observation = [this, &result](const Key& key) {
    const octomap::OcTreeKey native_key(key.x, key.y, key.z);
    const octomap::OcTreeNode* node = tree_.search(native_key);
    if (node == nullptr) {
      throw std::runtime_error("OctoMap update key is not queryable after update");
    }
    result.observations.push_back(
        KeyObservation{key, tree_.isNodeOccupied(*node), node->getLogOdds(), node->getOccupancy()});
  };
  for (const auto& key : result.free_keys) {
    append_observation(key);
  }
  for (const auto& key : result.occupied_keys) {
    append_observation(key);
  }
  std::sort(result.observations.begin(), result.observations.end(),
            [](const KeyObservation& lhs, const KeyObservation& rhs) { return lhs.key < rhs.key; });
  return result;
}

OccupancySnapshot OccupancyMap::snapshot_leaves() const {
  OccupancySnapshot snapshot;
  snapshot.revision = revision_;
  snapshot.resolution = tree_.getResolution();
  for (auto it = tree_.begin_leafs(), end = tree_.end_leafs(); it != end; ++it) {
    const auto center = it.getCoordinate();
    snapshot.leaves.push_back(LeafObservation{
        as_key(it.getKey()),
        {center.x(), center.y(), center.z()},
        it.getSize(),
        tree_.isNodeOccupied(*it),
        it->getLogOdds(),
        it->getOccupancy(),
    });
  }
  std::sort(snapshot.leaves.begin(), snapshot.leaves.end(),
            [](const LeafObservation& lhs, const LeafObservation& rhs) { return lhs.key < rhs.key; });
  if (!snapshot.leaves.empty()) {
    snapshot.bounds_min = snapshot.leaves.front().center;
    snapshot.bounds_max = snapshot.leaves.front().center;
    for (const auto& leaf : snapshot.leaves) {
      for (int axis = 0; axis < 3; ++axis) {
        snapshot.bounds_min[axis] = std::min(snapshot.bounds_min[axis], leaf.center[axis] - leaf.size / 2.0);
        snapshot.bounds_max[axis] = std::max(snapshot.bounds_max[axis], leaf.center[axis] + leaf.size / 2.0);
      }
    }
  }
  return snapshot;
}

}  // namespace standalone_sg::occupancy
