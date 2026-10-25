#pragma once

// Reference C++ decoder for the proposed 3DSG <-> OccuSG M2 v2 request.
// This is a workspace parity component; it is not yet wired into OccuSG.

#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace threedsg::m2v2 {

constexpr std::size_t kHeaderBytes = 32;
constexpr std::size_t kMaxFrameIdBytes = 1024;
constexpr std::uint64_t kDefaultMaxPayloadBytes = 128ULL * 1024ULL * 1024ULL;
constexpr std::uint16_t kVersion = 2;
constexpr std::uint16_t kIntegrateProjectOpcode = 1;

enum class ProjectionMode : std::uint8_t {
  kFullRefresh = 1,
  kPartialUpdate = 2,
  kCleanFullRefresh = 3,
};

struct Point {
  float x{};
  float y{};
  float z{};
  bool has_hit{};
};

struct IntegrateProjectRequest {
  std::uint64_t request_id{};
  double max_range_m{};
  std::array<float, 3> origin_xyz{};
  ProjectionMode projection_mode{};
  std::uint64_t frame_index{};
  std::uint64_t capture_monotonic_ns{};
  std::string frame_id;
  std::array<std::uint8_t, 32> package_sha256{};
  std::vector<Point> points;
};

namespace detail {

inline void require_range(std::size_t size, std::size_t offset, std::size_t count,
                          const char* field) {
  if (offset > size || count > size - offset) {
    throw std::runtime_error(std::string("truncated M2 v2 field: ") + field);
  }
}

template <typename UInt>
inline UInt read_unsigned_le(const std::vector<std::uint8_t>& bytes,
                             std::size_t offset, const char* field) {
  static_assert(std::numeric_limits<UInt>::is_integer &&
                !std::numeric_limits<UInt>::is_signed,
                "UInt must be an unsigned integer");
  require_range(bytes.size(), offset, sizeof(UInt), field);
  UInt value = 0;
  for (std::size_t index = 0; index < sizeof(UInt); ++index) {
    value |= static_cast<UInt>(bytes[offset + index]) << (index * 8U);
  }
  return value;
}

inline float read_float32_le(const std::vector<std::uint8_t>& bytes,
                             std::size_t offset, const char* field) {
  const std::uint32_t bits = read_unsigned_le<std::uint32_t>(bytes, offset, field);
  float value = 0.0F;
  std::memcpy(&value, &bits, sizeof(value));
  if (!std::isfinite(value)) {
    throw std::runtime_error(std::string("non-finite M2 v2 float: ") + field);
  }
  return value;
}

inline double read_float64_le(const std::vector<std::uint8_t>& bytes,
                              std::size_t offset, const char* field) {
  const std::uint64_t bits = read_unsigned_le<std::uint64_t>(bytes, offset, field);
  double value = 0.0;
  std::memcpy(&value, &bits, sizeof(value));
  if (!std::isfinite(value)) {
    throw std::runtime_error(std::string("non-finite M2 v2 float: ") + field);
  }
  return value;
}

}  // namespace detail

inline IntegrateProjectRequest decode_integrate_project(
    const std::vector<std::uint8_t>& packet,
    std::uint64_t max_payload_bytes = kDefaultMaxPayloadBytes) {
  if (packet.size() < kHeaderBytes) {
    throw std::runtime_error("truncated M2 v2 envelope");
  }
  if (packet[0] != '3' || packet[1] != 'S' || packet[2] != 'G' || packet[3] != 'M') {
    throw std::runtime_error("invalid M2 v2 magic");
  }
  const auto version = detail::read_unsigned_le<std::uint16_t>(packet, 4, "version");
  const auto opcode = detail::read_unsigned_le<std::uint16_t>(packet, 6, "opcode");
  const auto payload_bytes = detail::read_unsigned_le<std::uint64_t>(packet, 16, "payload_len");
  const auto flags = detail::read_unsigned_le<std::uint32_t>(packet, 24, "flags");
  const auto reserved = detail::read_unsigned_le<std::uint32_t>(packet, 28, "reserved");
  if (version != kVersion || opcode != kIntegrateProjectOpcode) {
    throw std::runtime_error("unsupported M2 v2 version/opcode");
  }
  if (flags != 0 || reserved != 0) {
    throw std::runtime_error("nonzero M2 v2 flags/reserved bits");
  }
  if (payload_bytes > max_payload_bytes ||
      payload_bytes != static_cast<std::uint64_t>(packet.size() - kHeaderBytes)) {
    throw std::runtime_error("invalid M2 v2 payload length");
  }

  IntegrateProjectRequest request;
  request.request_id = detail::read_unsigned_le<std::uint64_t>(packet, 8, "request_id");
  std::size_t offset = kHeaderBytes;
  request.max_range_m = detail::read_float64_le(packet, offset, "max_range_m");
  offset += sizeof(double);
  if (request.max_range_m <= 0.0) {
    throw std::runtime_error("M2 v2 max_range_m must be positive");
  }
  for (std::size_t axis = 0; axis < request.origin_xyz.size(); ++axis) {
    request.origin_xyz[axis] = detail::read_float32_le(
        packet, offset + axis * sizeof(float), "origin_xyz");
  }
  offset += 3 * sizeof(float);
  const std::uint32_t point_count =
      detail::read_unsigned_le<std::uint32_t>(packet, offset, "point_count");
  offset += sizeof(std::uint32_t);

  detail::require_range(packet.size(), offset, 1, "projection_mode");
  const auto mode = packet[offset++];
  if (mode < static_cast<std::uint8_t>(ProjectionMode::kFullRefresh) ||
      mode > static_cast<std::uint8_t>(ProjectionMode::kCleanFullRefresh)) {
    throw std::runtime_error("unknown M2 v2 projection mode");
  }
  request.projection_mode = static_cast<ProjectionMode>(mode);

  request.frame_index = detail::read_unsigned_le<std::uint64_t>(packet, offset, "frame_index");
  offset += sizeof(std::uint64_t);
  request.capture_monotonic_ns =
      detail::read_unsigned_le<std::uint64_t>(packet, offset, "capture_monotonic_ns");
  offset += sizeof(std::uint64_t);
  const std::uint16_t frame_id_bytes =
      detail::read_unsigned_le<std::uint16_t>(packet, offset, "frame_id_length");
  offset += sizeof(std::uint16_t);
  if (frame_id_bytes == 0 || frame_id_bytes > kMaxFrameIdBytes) {
    throw std::runtime_error("invalid M2 v2 frame_id length");
  }
  detail::require_range(packet.size(), offset, frame_id_bytes + 32, "frame identity");
  request.frame_id.assign(reinterpret_cast<const char*>(packet.data() + offset), frame_id_bytes);
  offset += frame_id_bytes;
  for (std::size_t index = 0; index < request.package_sha256.size(); ++index) {
    request.package_sha256[index] = packet[offset + index];
  }
  offset += request.package_sha256.size();

  const std::uint64_t xyz_bytes = static_cast<std::uint64_t>(point_count) * 3ULL * sizeof(float);
  const std::uint64_t hit_bytes = (static_cast<std::uint64_t>(point_count) + 7ULL) / 8ULL;
  const std::uint64_t expected_remaining = xyz_bytes + hit_bytes;
  if (expected_remaining != packet.size() - offset) {
    throw std::runtime_error("M2 v2 point count does not match payload length");
  }
  request.points.reserve(point_count);
  const std::size_t hits_offset = offset + static_cast<std::size_t>(xyz_bytes);
  for (std::uint32_t index = 0; index < point_count; ++index) {
    const std::size_t point_offset = offset + static_cast<std::size_t>(index) * 3 * sizeof(float);
    Point point;
    point.x = detail::read_float32_le(packet, point_offset, "point.x");
    point.y = detail::read_float32_le(packet, point_offset + sizeof(float), "point.y");
    point.z = detail::read_float32_le(packet, point_offset + 2 * sizeof(float), "point.z");
    const auto hit_byte = packet[hits_offset + index / 8U];
    point.has_hit = ((hit_byte >> (index % 8U)) & 1U) != 0;
    request.points.push_back(point);
  }
  if (point_count % 8U != 0U && hit_bytes != 0U) {
    const std::uint8_t used = static_cast<std::uint8_t>(point_count % 8U);
    const std::uint8_t unused_mask = static_cast<std::uint8_t>(0xFFU << used);
    if ((packet[hits_offset + static_cast<std::size_t>(hit_bytes) - 1] & unused_mask) != 0) {
      throw std::runtime_error("nonzero M2 v2 hit-bit padding");
    }
  }
  return request;
}

}  // namespace threedsg::m2v2
