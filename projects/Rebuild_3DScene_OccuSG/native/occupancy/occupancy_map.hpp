#pragma once

#include <array>
#include <cstdint>
#include <vector>

#include <octomap/OcTree.h>

namespace standalone_sg::occupancy {

struct ScanPoint {
  std::array<float, 3> xyz{};
  bool has_hit{true};
};

struct Scan {
  std::array<float, 3> origin{};
  std::vector<ScanPoint> points;
  double max_range{-1.0};
};

struct Key {
  std::uint16_t x{};
  std::uint16_t y{};
  std::uint16_t z{};

  bool operator<(const Key& other) const;
  bool operator==(const Key& other) const;
};

struct KeyObservation {
  Key key;
  bool occupied{false};
  float log_odds{0.0F};
  double probability{0.5};
};

struct UpdateResult {
  std::uint64_t revision{0};
  std::vector<Key> free_keys;
  std::vector<Key> occupied_keys;
  std::vector<KeyObservation> observations;
};

struct LeafObservation {
  Key key;
  std::array<double, 3> center{};
  double size{0.0};
  bool occupied{false};
  float log_odds{0.0F};
  double probability{0.5};
};

struct OccupancySnapshot {
  std::uint64_t revision{0};
  double resolution{0.0};
  std::array<double, 3> bounds_min{};
  std::array<double, 3> bounds_max{};
  std::vector<LeafObservation> leaves;
};

class OccupancyMap {
 public:
  OccupancyMap(double resolution,
               double prob_hit = 0.7,
               double prob_miss = 0.4,
               double clamping_min = 0.12,
               double clamping_max = 0.97,
               double occupancy_threshold = 0.5);

  UpdateResult integrate(const Scan& scan);
  OccupancySnapshot snapshot_leaves() const;

  double resolution() const { return tree_.getResolution(); }
  std::uint64_t revision() const { return revision_; }
  const octomap::OcTree& tree() const { return tree_; }

 private:
  static octomap::point3d as_point(const std::array<float, 3>& value);
  static Key as_key(const octomap::OcTreeKey& key);
  static std::vector<Key> sorted_keys(const octomap::KeySet& keys);

  octomap::OcTree tree_;
  std::uint64_t revision_{0};
};

}  // namespace standalone_sg::occupancy
