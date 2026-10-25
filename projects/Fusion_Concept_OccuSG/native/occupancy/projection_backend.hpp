#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <vector>

#include <octomap/OcTree.h>

#include "MapConverter.hh"
#include "occupancy_map.hpp"

namespace standalone_sg::occupancy {

struct ProjectionResult {
  std::uint64_t revision{0};
  std::uint64_t input_leaf_fingerprint{0};
  std::size_t input_leaf_count{0};
  bool updated{false};
  bool full_refresh{false};
  std::array<double, 4> changed_bounds_xy{};
  std::array<double, 6> changed_bounds_xyz{};
  bool has_changed_bounds{false};
  double resolution{0.0};
  double origin_x{0.0};
  double origin_y{0.0};
  int width{0};
  int height{0};
  std::vector<int> values;
  std::vector<double> z_floor;
  std::vector<double> z_ceiling;
  std::array<double, 2> height_trace_origin_xy{};
  int height_trace_width{0};
  int height_trace_height{0};
  std::vector<ScopeBridgeHeightCell> height_trace_cells;
};

class ProjectionBackend {
 public:
  ProjectionBackend(double resolution, double min_free_height,
                    int minimum_occupancy);

  ProjectionResult project(const octomap::OcTree& tree,
                           std::uint64_t revision,
                           bool full_refresh,
                           bool capture_height_intervals = false);

 private:
  struct Bounds {
    double min_x;
    double max_x;
    double min_y;
    double max_y;
    double min_z;
    double max_z;
  };

  bool compute_changed_bounds(const octomap::OcTree& current, Bounds& bounds) const;
  void set_metric_z_bounds(const octomap::OcTree& tree, Bounds& bounds) const;
  static void include(double x, double y, double z, Bounds& bounds, bool& initialized);
  std::vector<voxel> collect_leaves(const octomap::OcTree& tree,
                                   const Bounds* bounds) const;
  std::vector<double> as_min_max(const Bounds& bounds) const;
  ProjectionResult capture(std::uint64_t revision, bool full_refresh,
                           bool updated, const Bounds* bounds) const;

  double resolution_;
  std::unique_ptr<MapConverter> converter_;
  std::unique_ptr<octomap::OcTree> previous_tree_;
};

}  // namespace standalone_sg::occupancy
