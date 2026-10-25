#include "occupancy_map.hpp"
#include "reference_insert_scan.hpp"
#ifdef STANDALONE_SG_WITH_M3
#include "projection_backend.hpp"
#endif

#include <fstream>
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <type_traits>
#include <cstring>
#include <type_traits>
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

void emit_compact_update(const UpdateResult& update) {
  std::cout << "{\"revision\":" << update.revision
            << ",\"free_key_count\":" << update.free_keys.size()
            << ",\"occupied_key_count\":" << update.occupied_keys.size() << "}";
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

void emit_projection(const standalone_sg::occupancy::ProjectionResult& result,
                     const std::string& mode_override = "",
                     const OracleComparison* oracle = nullptr,
                     std::size_t source_leaf_count = 0,
                     std::uint64_t source_leaf_fingerprint = 0,
                     const std::string& converter_state = "retained") {
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
  std::cout << "}";
}
#endif


struct BinaryM2Request {
  std::uint64_t request_id{};
  std::uint64_t frame_index{};
  std::uint64_t capture_monotonic_ns{};
  std::uint8_t projection_mode{};
  std::string frame_id;
  std::string package_sha256;
  Scan scan;
};

template <typename T>
T read_le_integer(const unsigned char* data) {
  static_assert(std::is_unsigned<T>::value, "unsigned integer required");
  T value = 0;
  for (std::size_t index = 0; index < sizeof(T); ++index)
    value |= static_cast<T>(data[index]) << (index * 8U);
  return value;
}

float read_le_float32(const unsigned char* data) {
  const std::uint32_t bits = read_le_integer<std::uint32_t>(data);
  float value{};
  std::memcpy(&value, &bits, sizeof(value));
  return value;
}

double read_le_float64(const unsigned char* data) {
  const std::uint64_t bits = read_le_integer<std::uint64_t>(data);
  double value{};
  std::memcpy(&value, &bits, sizeof(value));
  return value;
}

std::string hex_digest(const unsigned char* bytes, std::size_t count) {
  static constexpr char digits[] = "0123456789abcdef";
  std::string out;
  out.reserve(count * 2);
  for (std::size_t index = 0; index < count; ++index) {
    out.push_back(digits[bytes[index] >> 4U]);
    out.push_back(digits[bytes[index] & 0x0fU]);
  }
  return out;
}

void emit_json_string(const std::string& value) {
  std::cout << '"';
  for (const unsigned char ch : value) {
    if (ch == '"' || ch == '\\') std::cout << '\\' << static_cast<char>(ch);
    else if (ch < 0x20) {
      std::cout << "\\u00" << std::hex << std::setw(2) << std::setfill('0')
                << static_cast<unsigned>(ch) << std::dec << std::setfill(' ');
    } else std::cout << static_cast<char>(ch);
  }
  std::cout << '"';
}

BinaryM2Request read_binary_m2_request() {
  std::array<unsigned char, 32> header{};
  std::cin.read(reinterpret_cast<char*>(header.data()), static_cast<std::streamsize>(header.size()));
  if (std::cin.gcount() != static_cast<std::streamsize>(header.size()))
    throw std::runtime_error("truncated M2 v2 envelope header");
  if (header[0] != '3' || header[1] != 'S' || header[2] != 'G' || header[3] != 'M')
    throw std::runtime_error("invalid M2 v2 magic");
  if (read_le_integer<std::uint16_t>(&header[4]) != 2 ||
      read_le_integer<std::uint16_t>(&header[6]) != 1)
    throw std::runtime_error("unsupported M2 v2 version/opcode");
  BinaryM2Request request;
  request.request_id = read_le_integer<std::uint64_t>(&header[8]);
  const std::uint64_t payload_size = read_le_integer<std::uint64_t>(&header[16]);
  if (read_le_integer<std::uint32_t>(&header[24]) != 0 ||
      read_le_integer<std::uint32_t>(&header[28]) != 0)
    throw std::runtime_error("nonzero M2 v2 flags/reserved bits");
  constexpr std::uint64_t kMaxPayload = 128ULL * 1024ULL * 1024ULL;
  if (payload_size > kMaxPayload) throw std::runtime_error("M2 v2 payload exceeds 128 MiB limit");
  std::vector<unsigned char> payload(static_cast<std::size_t>(payload_size));
  if (!payload.empty()) {
    std::cin.read(reinterpret_cast<char*>(payload.data()), static_cast<std::streamsize>(payload.size()));
    if (std::cin.gcount() != static_cast<std::streamsize>(payload.size()))
      throw std::runtime_error("truncated M2 v2 payload");
  }
  constexpr std::size_t kFixed = 8 + 12 + 4 + 1 + 8 + 8 + 2;
  if (payload.size() < kFixed + 1 + 32) throw std::runtime_error("truncated M2 v2 metadata");
  std::size_t offset = 0;
  request.scan.max_range = read_le_float64(&payload[offset]); offset += 8;
  for (std::size_t axis = 0; axis < 3; ++axis) {
    request.scan.origin[axis] = read_le_float32(&payload[offset]); offset += 4;
  }
  const std::uint32_t point_count = read_le_integer<std::uint32_t>(&payload[offset]); offset += 4;
  request.projection_mode = payload[offset++];
  if (request.projection_mode < 1 || request.projection_mode > 3)
    throw std::runtime_error("invalid M2 v2 projection mode");
  request.frame_index = read_le_integer<std::uint64_t>(&payload[offset]); offset += 8;
  request.capture_monotonic_ns = read_le_integer<std::uint64_t>(&payload[offset]); offset += 8;
  const std::uint16_t frame_id_size = read_le_integer<std::uint16_t>(&payload[offset]); offset += 2;
  if (frame_id_size == 0 || frame_id_size > 1024) throw std::runtime_error("invalid M2 v2 frame ID length");
  const std::uint64_t expected = static_cast<std::uint64_t>(offset) + frame_id_size + 32ULL +
      static_cast<std::uint64_t>(point_count) * 12ULL + (static_cast<std::uint64_t>(point_count) + 7ULL) / 8ULL;
  if (expected != payload.size()) throw std::runtime_error("M2 v2 point count does not match payload length");
  request.frame_id.assign(reinterpret_cast<const char*>(&payload[offset]), frame_id_size); offset += frame_id_size;
  if (request.frame_id.find('\0') != std::string::npos) throw std::runtime_error("M2 v2 frame ID contains NUL");
  request.package_sha256 = hex_digest(&payload[offset], 32); offset += 32;
  if (!std::isfinite(request.scan.max_range) || request.scan.max_range <= 0.0)
    throw std::runtime_error("invalid M2 v2 max range");
  for (float value : request.scan.origin)
    if (!std::isfinite(value)) throw std::runtime_error("invalid M2 v2 sensor origin");
  request.scan.points.reserve(point_count);
  const std::size_t point_data = offset;
  const std::size_t hit_data = point_data + static_cast<std::size_t>(point_count) * 12U;
  const std::size_t hit_size = (static_cast<std::size_t>(point_count) + 7U) / 8U;
  if (point_count % 8U && hit_size) {
    const unsigned char unused_mask = static_cast<unsigned char>(0xffU << (point_count % 8U));
    if ((payload.back() & unused_mask) != 0) throw std::runtime_error("nonzero M2 v2 hit-bit padding");
  }
  for (std::uint32_t index = 0; index < point_count; ++index) {
    const std::size_t point_offset = point_data + static_cast<std::size_t>(index) * 12U;
    ScanPoint point;
    for (std::size_t axis = 0; axis < 3; ++axis) {
      point.xyz[axis] = read_le_float32(&payload[point_offset + axis * 4U]);
      if (!std::isfinite(point.xyz[axis])) throw std::runtime_error("non-finite M2 v2 point");
    }
    point.has_hit = ((payload[hit_data + index / 8U] >> (index % 8U)) & 1U) != 0;
    request.scan.points.push_back(point);
  }
  return request;
}

void emit_binary_update(const UpdateResult& update, const BinaryM2Request& request,
                        const std::string& response_mode) {
  std::cout << "{\"revision\":" << update.revision;
  if (response_mode == "debug") {
    std::cout << ",\"free_keys\":"; emit_keys(update.free_keys);
    std::cout << ",\"occupied_keys\":"; emit_keys(update.occupied_keys);
    std::cout << ",\"observations\":"; emit_observations(update.observations);
  } else {
    std::cout << ",\"free_key_count\":" << update.free_keys.size()
              << ",\"occupied_key_count\":" << update.occupied_keys.size();
  }
  std::cout << ",\"request_id\":" << request.request_id << ",\"frame_id\":";
  emit_json_string(request.frame_id);
  std::cout << ",\"frame_index\":" << request.frame_index
            << ",\"capture_monotonic_ns\":" << request.capture_monotonic_ns
            << ",\"package_sha256\":\"" << request.package_sha256 << "\",\"projection_mode\":\""
            << (request.projection_mode == 1 ? "full_refresh" :
                (request.projection_mode == 2 ? "partial_update" : "clean_full_refresh")) << "\"}\n" << std::flush;
}

int run_server(int argc, char** argv) {
  double resolution = 0.05;
  double prob_hit = 0.7;
  double prob_miss = 0.4;
  double clamping_min = 0.12;
  double clamping_max = 0.97;
  double occupancy_threshold = 0.5;
  double projection_min_free_height = 0.3;
  int projection_minimum_occupancy = 5;
  std::string m2_response_mode = "compact";
  std::string m2_input_mode = "ascii";
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
    else if (option == "--m2-input-mode") {
      if (index + 1 >= argc) throw std::runtime_error("missing value for --m2-input-mode");
      m2_input_mode = argv[++index];
      if (m2_input_mode != "ascii" && m2_input_mode != "binary")
        throw std::runtime_error("M2 input mode must be ascii or binary");
    }
    else if (option == "--m2-response-mode") {
      if (index + 1 >= argc) throw std::runtime_error("missing value for --m2-response-mode");
      m2_response_mode = argv[++index];
      if (m2_response_mode != "compact" && m2_response_mode != "debug")
        throw std::runtime_error("M2 response mode must be compact or debug");
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
  while (true) {
    std::cin >> std::ws;
    if (std::cin.peek() == std::char_traits<char>::eof()) break;
    if (m2_input_mode == "binary" && std::cin.peek() == '3') {
      const BinaryM2Request request = read_binary_m2_request();
      const UpdateResult update = map.integrate(request.scan);
      emit_binary_update(update, request, m2_response_mode);
      continue;
    }
    if (!(std::cin >> tag)) break;
    if (tag == "snapshot") {
      emit_snapshot(map.snapshot_leaves());
      std::cout << "\n" << std::flush;
      continue;
    }
#ifdef STANDALONE_SG_WITH_M3
    if (tag == "project") {
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
        emit_projection(clean_result, "clean_full_refresh", &clean_compare, clean_input.leaves.size(),
                        fingerprint_leaves(clean_input.leaves), "new");
      } else {
        const bool full = mode == "full";
        const SourceUpdate source_input = prepare_pinned_callback_update(map.tree(), *source_previous, full);
        const auto result = projection.project(map.tree(), map.revision(), full);
        if (source_input.changed && !source_input.leaves.empty()) source_oracle.updateMap(source_input.leaves,
            std::vector<double>(source_input.bounds.begin(), source_input.bounds.end()));
        const auto comparison = compare_with_pinned_mapconverter(result, source_oracle, source_input);
        emit_projection(result, "", &comparison, source_input.leaves.size(),
                        fingerprint_leaves(source_input.leaves), "retained");
        source_previous.reset(new octomap::OcTree(map.tree()));
      }
      std::cout << "\n" << std::flush;
      continue;
    }
#endif
    if (tag != "scan") throw std::runtime_error("server expected scan, snapshot or project command");
    if (m2_input_mode == "binary") throw std::runtime_error("ASCII scan is disabled in binary M2 mode");
    const UpdateResult update = map.integrate(read_scan_body(std::cin));
    if (m2_response_mode == "debug") emit_update(update);
    else emit_compact_update(update);
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
