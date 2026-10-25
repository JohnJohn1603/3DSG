#include "reference_insert_scan.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace standalone_sg::occupancy {
namespace {

std::vector<Key> sorted_keys(const octomap::KeySet& keys) {
  std::vector<Key> result;
  result.reserve(keys.size());
  for (const auto& key : keys) result.push_back(Key{key[0], key[1], key[2]});
  std::sort(result.begin(), result.end());
  return result;
}

}  // namespace

ReferenceKeySets reference_insert_scan(const octomap::OcTree& tree, const Scan& scan) {
  if (!(scan.max_range > 0.0) || !std::isfinite(scan.max_range)) {
    throw std::invalid_argument("reference scan.max_range must be finite and positive");
  }
  const octomap::point3d origin(scan.origin[0], scan.origin[1], scan.origin[2]);
  octomap::KeySet free_cells;
  octomap::KeySet occupied_cells;
  octomap::KeyRay key_ray;

  for (const ScanPoint& input : scan.points) {
    const octomap::point3d point(input.xyz[0], input.xyz[1], input.xyz[2]);
    const octomap::point3d delta = point - origin;
    const double distance = delta.norm();
    if (!(distance > 0.0) || !std::isfinite(distance)) {
      throw std::invalid_argument("reference scan point must define a non-zero ray");
    }
    if (input.has_hit && distance <= scan.max_range) {
      if (tree.computeRayKeys(origin, point, key_ray)) {
        free_cells.insert(key_ray.begin(), key_ray.end());
      }
      octomap::OcTreeKey key;
      if (tree.coordToKeyChecked(point, key)) occupied_cells.insert(key);
    } else {
      // Pinned insertScan clipped branch, including explicit endpoint key.
      const octomap::point3d new_end =
          origin + delta.normalized() * static_cast<float>(scan.max_range);
      if (tree.computeRayKeys(origin, new_end, key_ray)) {
        free_cells.insert(key_ray.begin(), key_ray.end());
        octomap::OcTreeKey end_key;
        if (!tree.coordToKeyChecked(new_end, end_key)) {
          throw std::runtime_error("reference clipped endpoint has no key");
        }
        free_cells.insert(end_key);
      }
    }
  }

  for (auto it = free_cells.begin(); it != free_cells.end();) {
    if (occupied_cells.find(*it) != occupied_cells.end()) {
      it = free_cells.erase(it);
    } else {
      ++it;
    }
  }
  return ReferenceKeySets{sorted_keys(free_cells), sorted_keys(occupied_cells)};
}

}  // namespace standalone_sg::occupancy

