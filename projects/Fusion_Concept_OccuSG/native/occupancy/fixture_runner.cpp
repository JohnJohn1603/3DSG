#include "occupancy_map.hpp"
#include "reference_insert_scan.hpp"
#ifdef STANDALONE_SG_WITH_M3
#include "projection_backend.hpp"
#endif

#include <fstream>
#include <algorithm>
#include <array>
#include <cstdint>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <cmath>
#include <stdexcept>
#include <string>
#include <vector>

using standalone_sg::occupancy::Key;
using standalone_sg::occupancy::KeyObservation;
using standalone_sg::occupancy::OccupancyMap;
using standalone_sg::occupancy::Scan;
using standalone_sg::occupancy::ScanPoint;
using standalone_sg::occupancy::UpdateResult;
using standalone_sg::occupancy::ReferenceKeySets;
using standalone_sg::occupancy::OccupancySnapshot;

namespace {

struct Fixture {
  double resolution{0.05};
  double prob_hit{0.7};
  double prob_miss{0.4};
  double clamping_min{0.12};
  double clamping_max{0.97};
  double occupancy_threshold{0.5};
  std::vector<Scan> scans;
};

Scan read_scan_body(std::istream& input) {
  Scan scan;
  std::size_t point_count = 0;
  input >> scan.max_range >> scan.origin[0] >> scan.origin[1] >> scan.origin[2] >> point_count;
  if (!input) {
    throw std::runtime_error("malformed scan header");
  }
  scan.points.reserve(point_count);
  for (std::size_t index = 0; index < point_count; ++index) {
    ScanPoint point;
    int has_hit = 0;
    input >> point.xyz[0] >> point.xyz[1] >> point.xyz[2] >> has_hit;
    if (!input) {
      throw std::runtime_error("malformed scan point");
    }
    point.has_hit = has_hit != 0;
    scan.points.push_back(point);
  }
  return scan;
}

Fixture read_fixture(const std::string& path) {
  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("cannot open fixture: " + path);
  }
  Fixture fixture;
  std::string tag;
  std::size_t scan_count = 0;
  while (input >> tag) {
    if (!tag.empty() && tag[0] == '#') {
      std::string ignored;
      std::getline(input, ignored);
    } else if (tag == "resolution") {
      input >> fixture.resolution;
    } else if (tag == "prob_hit") {
      input >> fixture.prob_hit;
    } else if (tag == "prob_miss") {
      input >> fixture.prob_miss;
    } else if (tag == "clamping_min") {
      input >> fixture.clamping_min;
    } else if (tag == "clamping_max") {
      input >> fixture.clamping_max;
    } else if (tag == "occupancy_threshold") {
      input >> fixture.occupancy_threshold;
    } else if (tag == "scan_count") {
      input >> scan_count;
      fixture.scans.reserve(scan_count);
    } else if (tag == "scan") {
      fixture.scans.push_back(read_scan_body(input));
    } else {
      throw std::runtime_error("unknown fixture token: " + tag);
    }
  }
  if (fixture.scans.size() != scan_count) {
    throw std::runtime_error("fixture scan_count does not match scan blocks");
  }
  return fixture;
}

void emit_key(const Key& key) {
  std::cout << "[" << key.x << "," << key.y << "," << key.z << "]";
}

void emit_keys(const std::vector<Key>& keys) {
  std::cout << "[";
  for (std::size_t index = 0; index < keys.size(); ++index) {
    if (index != 0) std::cout << ",";
    emit_key(keys[index]);
  }
  std::cout << "]";
}

void emit_observations(const std::vector<KeyObservation>& observations) {
  std::cout << "[";
  for (std::size_t index = 0; index < observations.size(); ++index) {
    const auto& observation = observations[index];
    if (index != 0) std::cout << ",";
    std::cout << "{\"key\":";
    emit_key(observation.key);
    std::cout << ",\"occupied\":" << (observation.occupied ? "true" : "false")
              << ",\"log_odds\":" << observation.log_odds
              << ",\"probability\":" << observation.probability << "}";
  }
  std::cout << "]";
}

void emit_update(const UpdateResult& update) {
  std::cout << "{\"revision\":" << update.revision << ",\"free_keys\":";
  emit_keys(update.free_keys);
  std::cout << ",\"occupied_keys\":";
  emit_keys(update.occupied_keys);
  std::cout << ",\"observations\":";
  emit_observations(update.observations);
  std::cout << "}";
}

void emit_reference_update(const ReferenceKeySets& update, std::size_t revision) {
  std::cout << "{\"revision\":" << revision << ",\"free_keys\":";
  emit_keys(update.free_keys);
  std::cout << ",\"occupied_keys\":";
  emit_keys(update.occupied_keys);
  std::cout << "}";
}

void emit_snapshot(const OccupancySnapshot& snapshot) {
  std::cout << "{\"revision\":" << snapshot.revision
            << ",\"resolution\":" << snapshot.resolution << ",\"bounds_min\":["
            << snapshot.bounds_min[0] << "," << snapshot.bounds_min[1] << "," << snapshot.bounds_min[2]
            << "],\"bounds_max\":[" << snapshot.bounds_max[0] << "," << snapshot.bounds_max[1]
            << "," << snapshot.bounds_max[2] << "],\"leaves\":[";
  for (std::size_t index = 0; index < snapshot.leaves.size(); ++index) {
    if (index != 0) std::cout << ",";
    const auto& leaf = snapshot.leaves[index];
    std::cout << "{\"key\":";
    emit_key(leaf.key);
    std::cout << ",\"center\":[" << leaf.center[0] << "," << leaf.center[1] << "," << leaf.center[2]
              << "],\"size\":" << leaf.size << ",\"occupied\":"
              << (leaf.occupied ? "true" : "false") << ",\"log_odds\":" << leaf.log_odds
              << ",\"probability\":" << leaf.probability << "}";
  }
  std::cout << "]}";
}

#ifdef STANDALONE_SG_WITH_M3
// Direct-source callback oracle and diagnostic trace helpers are defined below.
using TraceBounds = std::array<double, 6>;
struct SourceUpdate { bool changed{false}; TraceBounds bounds{}; std::vector<voxel> leaves; };

std::uint64_t fingerprint_leaves(const std::vector<voxel>& leaves) {
  std::uint64_t hash = 1469598103934665603ULL;
  auto add = [&hash](const void* bytes, std::size_t count) {
    const auto* data = static_cast<const unsigned char*>(bytes);
    for (std::size_t i = 0; i < count; ++i) { hash ^= data[i]; hash *= 1099511628211ULL; }
  };
  for (const auto& leaf : leaves) {
    const double fields[] = {leaf.position.x, leaf.position.y, leaf.position.z, leaf.halfSize};
    add(fields, sizeof(fields));
    const std::uint8_t occupied = leaf.occupied ? 1 : 0;
    add(&occupied, sizeof(occupied));
  }
  return hash;
}

void include_source_bound(double x, double y, double z, TraceBounds& b, bool& initialized) {
  if (!initialized) { b = {x,x,y,y,z,z}; initialized = true; return; }
  b[0]=std::min(b[0],x); b[1]=std::max(b[1],x); b[2]=std::min(b[2],y);
  b[3]=std::max(b[3],y); b[4]=std::min(b[4],z); b[5]=std::max(b[5],z);
}

std::vector<voxel> source_leaves_in_bounds(const octomap::OcTree& tree, const TraceBounds& b) {
  std::vector<voxel> leaves;
  const octomap::point3d lo(b[0],b[2],b[4]), hi(b[1],b[3],b[5]);
  for (auto it=tree.begin_leafs_bbx(lo,hi), end=tree.end_leafs_bbx(); it!=end; ++it) {
    voxel v; v.position={it.getX(),it.getY(),it.getZ()};
    v.halfSize=static_cast<float>(it.getSize()/2.0); v.occupied=tree.isNodeOccupied(*it);
    leaves.push_back(v);
  }
  return leaves;
}

SourceUpdate prepare_pinned_callback_update(const octomap::OcTree& current,
                                             const octomap::OcTree& previous,
                                             bool full_refresh) {
  SourceUpdate out;
  if (full_refresh) {
    if (current.size()==0) return out;
    double min_x,min_y,min_z,max_x,max_y,max_z;
    current.getMetricMin(min_x,min_y,min_z); current.getMetricMax(max_x,max_y,max_z);
    out.bounds={min_x,max_x,min_y,max_y,min_z,max_z}; out.changed=true;
  } else {
    bool initialized=false;
    for (auto it=current.begin_leafs(),end=current.end_leafs(); it!=end; ++it) {
      const auto* old=previous.search(it.getKey());
      if (old && current.isNodeOccupied(*it)==previous.isNodeOccupied(old)) continue;
      include_source_bound(it.getX(),it.getY(),it.getZ(),out.bounds,initialized);
    }
    for (auto it=previous.begin_leafs(),end=previous.end_leafs(); it!=end; ++it) {
      if (current.search(it.getKey())) continue;
      include_source_bound(it.getX(),it.getY(),it.getZ(),out.bounds,initialized);
    }
    if (!initialized) return out;
    double min_x,min_y,min_z,max_x,max_y,max_z;
    current.getMetricMin(min_x,min_y,min_z); current.getMetricMax(max_x,max_y,max_z);
    out.bounds[4]=min_z; out.bounds[5]=max_z; out.changed=true;
  }
  out.leaves=source_leaves_in_bounds(current,out.bounds);
  return out;
}

struct OracleComparison {
  bool bounds_match{false}, input_leaves_match{false}, geometry_match{false};
  std::size_t value_mismatches{0}, floor_mismatches{0}, ceiling_mismatches{0};
};
bool same_height(double a,double b) { return (std::isnan(a)&&std::isnan(b)) || a==b; }

OracleComparison compare_with_pinned_mapconverter(
    const standalone_sg::occupancy::ProjectionResult& actual, MapConverter& oracle,
    const SourceUpdate& input) {
  OracleComparison c;
  c.input_leaves_match=actual.input_leaf_count==input.leaves.size() &&
      actual.input_leaf_fingerprint==fingerprint_leaves(input.leaves);
  c.bounds_match=actual.has_changed_bounds==input.changed;
  if (input.changed&&actual.has_changed_bounds) {
    const TraceBounds got={actual.changed_bounds_xyz[0],actual.changed_bounds_xyz[1],
      actual.changed_bounds_xyz[2],actual.changed_bounds_xyz[3],
      actual.changed_bounds_xyz[4],actual.changed_bounds_xyz[5]};
    c.bounds_match=c.bounds_match&&(got==input.bounds);
  }
  bool same=actual.width==oracle.map.sizeX()&&actual.height==oracle.map.sizeY()&&
      actual.origin_x==oracle.map.offsetX()&&actual.origin_y==oracle.map.offsetY()&&
      actual.resolution==oracle.map.getResulution();
  if (same) for (int y=0;y<actual.height;++y) for (int x=0;x<actual.width;++x) {
    const std::size_t i=static_cast<std::size_t>(y)*actual.width+x;
    if (actual.values[i]!=oracle.map.get(x,y)) ++c.value_mismatches;
    if (!same_height(actual.z_floor[i],oracle.map.getHeight(x,y))) ++c.floor_mismatches;
    if (!same_height(actual.z_ceiling[i],oracle.map.getHeightTop(x,y))) ++c.ceiling_mismatches;
  }
  c.geometry_match=same&&!c.value_mismatches&&!c.floor_mismatches&&!c.ceiling_mismatches;
  return c;
}

void emit_oracle_comparison(const OracleComparison& c,
    const standalone_sg::occupancy::ProjectionResult& actual, std::size_t input_count,
    std::uint64_t input_hash, const std::string& state) {
  std::cout << ",\"source_oracle\":{\"backend\":\"pinned_cpp_MapConverter_direct\",\"converter_state\":\""
    << state << "\",\"source_leaf_count\":" << input_count
    << ",\"adapter_leaf_count\":" << actual.input_leaf_count
    << ",\"source_leaf_fingerprint\":" << input_hash
    << ",\"adapter_leaf_fingerprint\":" << actual.input_leaf_fingerprint
    << ",\"bounds_match\":" << (c.bounds_match?"true":"false")
    << ",\"input_leaves_match\":" << (c.input_leaves_match?"true":"false")
    << ",\"geometry_match\":" << (c.geometry_match?"true":"false")
    << ",\"value_mismatches\":" << c.value_mismatches
    << ",\"floor_mismatches\":" << c.floor_mismatches
    << ",\"ceiling_mismatches\":" << c.ceiling_mismatches << "}";
}
void emit_optional_double(double value) {
  if (std::isfinite(value)) std::cout << value;
  else std::cout << "null";
}

void emit_range_list(std::ostream& output, const std::vector<heightRange>& ranges) {
  output << "[";
  for (std::size_t i = 0; i < ranges.size(); ++i) {
    if (i) output << ",";
    output << "{\"bottom_m\":" << ranges[i].bottom
           << ",\"top_m\":" << ranges[i].top << "}";
  }
  output << "]";
}

void write_height_interval_trace(const std::string& path,
                                 const standalone_sg::occupancy::ProjectionResult& result,
                                 const std::string& mode) {
  if (path.empty()) return;
  const std::filesystem::path file(path);
  std::filesystem::create_directories(file.parent_path());
  std::ofstream out(file, std::ios::binary | std::ios::trunc);
  if (!out) throw std::runtime_error("cannot create height-interval trace: " + path);
  out << std::setprecision(17);
  out << "{\"schema_version\":1,\"capture_point\":\"after_min_free_height_pruning_before_z_envelope_collapse\","
      << "\"map_revision\":" << result.revision
      << ",\"projection_mode\":\"" << mode << "\",\"updated\":"
      << (result.updated ? "true" : "false")
      << ",\"coordinate_frame\":\"world\",\"units\":\"m\","
      << "\"transform_provenance\":\"M1 RGB-D backprojection T_W_Copt and Habitat capture basis conversion; see replay manifest\","
      << "\"interval_semantics\":{\"free\":\"non-occupied Z ranges surviving source min_free_height pruning; not floor labels\","
      << "\"free_before_pruning\":\"non-occupied ranges before source min_free_height pruning\","
      << "\"occupied\":\"occupied voxel Z ranges; not floor labels\","
      << "\"m3_value\":\"-1 unknown, 0 free, 1..100 neighbour occupation score\"},"
      << "\"local_update_grid\":{\"origin_xy_m\":["
      << result.height_trace_origin_xy[0] << "," << result.height_trace_origin_xy[1]
      << "],\"resolution_m\":" << result.resolution
      << ",\"width\":" << result.height_trace_width
      << ",\"height\":" << result.height_trace_height
      << ",\"cell_emission\":\"interior cells actually processed by MapConverter; outer one-cell border is not processed by source algorithm\"},\"cells\":[";
  for (std::size_t i = 0; i < result.height_trace_cells.size(); ++i) {
    const auto& cell = result.height_trace_cells[i];
    if (i) out << ",";
    const char* state = cell.m3_value < 0 ? "unknown"
                        : cell.m3_value == 0 ? "free"
                                             : "neighbor_occupation_score";
    out << "{\"cell_index_xy\":[" << cell.cell_x << "," << cell.cell_y
        << "],\"world_xy_m\":[" << cell.world_x << "," << cell.world_y
        << "],\"m3_value\":" << cell.m3_value
        << ",\"m3_semantics\":\"" << state << "\",\"z_floor_m\":";
    if (std::isfinite(cell.z_floor)) out << cell.z_floor; else out << "null";
    out << ",\"z_ceiling_m\":";
    if (std::isfinite(cell.z_ceiling)) out << cell.z_ceiling; else out << "null";
    out << ",\"free_intervals_before_pruning\":";
    emit_range_list(out, cell.free_before_min_height);
    out << ",\"free_intervals_used_by_projection\":";
    emit_range_list(out, cell.free_used_for_projection);
    out << ",\"occupied_intervals\":";
    emit_range_list(out, cell.occupied);
    out << "}";
  }
  out << "]}\n";
  out.flush();
  if (!out) throw std::runtime_error("failed writing height-interval trace: " + path);
}

void emit_projection(const standalone_sg::occupancy::ProjectionResult& result,
                     const std::string& mode_override = "",
                     const OracleComparison* oracle = nullptr,
                     std::size_t source_leaf_count = 0,
                     std::uint64_t source_leaf_fingerprint = 0,
                     const std::string& converter_state = "retained",
                     const std::string& interval_trace_path = "") {
  const std::string mode = mode_override.empty()
      ? (result.full_refresh ? "full_refresh" : "partial_update")
      : mode_override;
  std::cout << "{\"revision\":" << result.revision
            << ",\"updated\":" << (result.updated ? "true" : "false")
            << ",\"mode\":\"" << mode << "\""
            << ",\"resolution\":" << result.resolution
            << ",\"origin_xy\":[" << result.origin_x << "," << result.origin_y << "]"
            << ",\"width\":" << result.width << ",\"height\":" << result.height
            << ",\"values\":[";
  for (std::size_t index = 0; index < result.values.size(); ++index) {
    if (index) std::cout << ",";
    std::cout << result.values[index];
  }
  std::cout << "],\"z_floor\":[";
  for (std::size_t index = 0; index < result.z_floor.size(); ++index) {
    if (index) std::cout << ",";
    emit_optional_double(result.z_floor[index]);
  }
  std::cout << "],\"z_ceiling\":[";
  for (std::size_t index = 0; index < result.z_ceiling.size(); ++index) {
    if (index) std::cout << ",";
    emit_optional_double(result.z_ceiling[index]);
  }
  std::cout << "],\"changed_bounds_xy\":";
  if (result.has_changed_bounds) {
    std::cout << "[" << result.changed_bounds_xy[0] << "," << result.changed_bounds_xy[1]
              << "," << result.changed_bounds_xy[2] << "," << result.changed_bounds_xy[3] << "]";
  } else {
    std::cout << "null";
  }
  std::cout << ",\"changed_bounds_xyz\":";
  if (result.has_changed_bounds) {
    std::cout << "[" << result.changed_bounds_xyz[0] << "," << result.changed_bounds_xyz[1]
              << "," << result.changed_bounds_xyz[2] << "," << result.changed_bounds_xyz[3]
              << "," << result.changed_bounds_xyz[4] << "," << result.changed_bounds_xyz[5] << "]";
  } else {
    std::cout << "null";
  }
  if (oracle != nullptr)
    emit_oracle_comparison(*oracle, result, source_leaf_count,
                           source_leaf_fingerprint, converter_state);
  if (!interval_trace_path.empty()) {
    std::cout << ",\"height_interval_trace\":{\"file\":\"" << interval_trace_path
              << "\",\"cell_count\":" << result.height_trace_cells.size()
              << ",\"capture_point\":\"after_min_free_height_pruning_before_z_envelope_collapse\"}";
  }
  std::cout << "}";
}
#endif

int run_server(int argc, char** argv) {
  double resolution = 0.05;
  double prob_hit = 0.7;
  double prob_miss = 0.4;
  double clamping_min = 0.12;
  double clamping_max = 0.97;
  double occupancy_threshold = 0.5;
  double projection_min_free_height = 0.3;
  int projection_minimum_occupancy = 5;
  std::string height_interval_trace_dir;
  for (int index = 2; index < argc; ++index) {
    const std::string option(argv[index]);
    auto read_value = [&](double& target) {
      if (index + 1 >= argc) throw std::runtime_error("missing value for " + option);
      target = std::stod(argv[++index]);
    };
    if (option == "--resolution") read_value(resolution);
    else if (option == "--prob-hit") read_value(prob_hit);
    else if (option == "--prob-miss") read_value(prob_miss);
    else if (option == "--clamping-min") read_value(clamping_min);
    else if (option == "--clamping-max") read_value(clamping_max);
    else if (option == "--occupancy-threshold") read_value(occupancy_threshold);
    else if (option == "--projection-min-free-height") read_value(projection_min_free_height);
    else if (option == "--projection-minimum-occupancy") {
      double value = 0.0;
      read_value(value);
      if (value != std::floor(value)) throw std::runtime_error("projection minimum occupancy must be integer");
      projection_minimum_occupancy = static_cast<int>(value);
    }
    else if (option == "--height-interval-trace-dir") {
      if (index + 1 >= argc) throw std::runtime_error("missing value for --height-interval-trace-dir");
      height_interval_trace_dir = argv[++index];
    }
    else throw std::runtime_error("unknown server option: " + option);
  }

  OccupancyMap map(resolution, prob_hit, prob_miss, clamping_min, clamping_max, occupancy_threshold);
#ifdef STANDALONE_SG_WITH_M3
  standalone_sg::occupancy::ProjectionBackend projection(
      resolution, projection_min_free_height, projection_minimum_occupancy);
  MapConverter source_oracle(resolution, 1, projection_min_free_height,
                             projection_minimum_occupancy);
  std::unique_ptr<octomap::OcTree> source_previous(new octomap::OcTree(resolution));
#endif
  std::cout << std::setprecision(17);
  std::string tag;
  std::uint64_t project_request_index = 0;
  while (std::cin >> tag) {
    if (tag == "snapshot") {
      emit_snapshot(map.snapshot_leaves());
      std::cout << "\n" << std::flush;
      continue;
    }
#ifdef STANDALONE_SG_WITH_M3
    if (tag == "project") {
      ++project_request_index;
      std::string mode;
      if (!(std::cin >> mode) || (mode != "full" && mode != "partial" && mode != "clean")) {
        throw std::runtime_error("project requires mode full, partial or clean");
      }
      if (mode == "clean") {
        standalone_sg::occupancy::ProjectionBackend clean_projection(
            resolution, projection_min_free_height, projection_minimum_occupancy);
        auto clean_result = clean_projection.project(map.tree(), map.revision(), true);
        octomap::OcTree empty_previous(resolution);
        const SourceUpdate clean_input = prepare_pinned_callback_update(map.tree(), empty_previous, true);
        MapConverter clean_oracle(resolution, 1, projection_min_free_height, projection_minimum_occupancy);
        if (clean_input.changed) clean_oracle.updateMap(clean_input.leaves,
            std::vector<double>(clean_input.bounds.begin(), clean_input.bounds.end()));
        const auto clean_compare = compare_with_pinned_mapconverter(clean_result, clean_oracle, clean_input);
        const std::string trace_path = height_interval_trace_dir.empty() ? "" :
            (std::filesystem::path(height_interval_trace_dir) /
             ("project_" + std::to_string(project_request_index) + "_m3rev_" +
              std::to_string(clean_result.revision) + ".json")).string();
        write_height_interval_trace(trace_path, clean_result, "clean_full_refresh");
        emit_projection(clean_result, "clean_full_refresh", &clean_compare, clean_input.leaves.size(),
                        fingerprint_leaves(clean_input.leaves), "new", trace_path);
      } else {
        const bool full = mode == "full";
        const SourceUpdate source_input = prepare_pinned_callback_update(map.tree(), *source_previous, full);
        const auto result = projection.project(map.tree(), map.revision(), full,
                                               !height_interval_trace_dir.empty());
        if (source_input.changed && !source_input.leaves.empty()) source_oracle.updateMap(source_input.leaves,
            std::vector<double>(source_input.bounds.begin(), source_input.bounds.end()));
        const auto comparison = compare_with_pinned_mapconverter(result, source_oracle, source_input);
        const std::string trace_path = height_interval_trace_dir.empty() ? "" :
            (std::filesystem::path(height_interval_trace_dir) /
             ("project_" + std::to_string(project_request_index) + "_m3rev_" +
              std::to_string(result.revision) + ".json")).string();
        write_height_interval_trace(trace_path, result, result.full_refresh ? "full_refresh" : "partial_update");
        emit_projection(result, "", &comparison, source_input.leaves.size(),
                        fingerprint_leaves(source_input.leaves), "retained", trace_path);
        source_previous.reset(new octomap::OcTree(map.tree()));
      }
      std::cout << "\n" << std::flush;
      continue;
    }
#endif
    if (tag != "scan") throw std::runtime_error("server expected scan, snapshot or project command");
    const UpdateResult update = map.integrate(read_scan_body(std::cin));
    emit_update(update);
    std::cout << "\n" << std::flush;
  }
  return 0;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    if (argc >= 2 && std::string(argv[1]) == "--serve") {
      return run_server(argc, argv);
    }
    if (argc == 4 && std::string(argv[1]) == "--reference" && std::string(argv[2]) == "--fixture") {
      const Fixture fixture = read_fixture(argv[3]);
      octomap::OcTree reference_tree(fixture.resolution);
      std::cout << "{\"resolution\":" << reference_tree.getResolution() << ",\"updates\":[";
      for (std::size_t index = 0; index < fixture.scans.size(); ++index) {
        if (index != 0) std::cout << ",";
        emit_reference_update(reference_insert_scan(reference_tree, fixture.scans[index]), index + 1);
      }
      std::cout << "]}\n";
      return 0;
    }
    if (argc != 3 || std::string(argv[1]) != "--fixture") {
      std::cerr << "usage: standalone_sg_octomap_fixture_runner --fixture PATH\n"
                << "   or: standalone_sg_octomap_fixture_runner --reference --fixture PATH\n"
                << "   or: standalone_sg_octomap_fixture_runner --serve [options]\n";
      return 2;
    }
    const Fixture fixture = read_fixture(argv[2]);
    OccupancyMap map(fixture.resolution, fixture.prob_hit, fixture.prob_miss,
                     fixture.clamping_min, fixture.clamping_max, fixture.occupancy_threshold);
    std::cout << std::setprecision(9);
    std::cout << "{\"resolution\":" << map.resolution() << ",\"updates\":[";
    for (std::size_t index = 0; index < fixture.scans.size(); ++index) {
      if (index != 0) std::cout << ",";
      emit_update(map.integrate(fixture.scans[index]));
    }
    std::cout << "]}\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "M2 fixture runner error: " << error.what() << "\n";
    return 1;
  }
}
