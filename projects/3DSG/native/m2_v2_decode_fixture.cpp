#include "m2_v2_codec.hpp"

#include <fstream>
#include <iomanip>
#include <iostream>
#include <iterator>

namespace {

const char* mode_name(threedsg::m2v2::ProjectionMode mode) {
  using threedsg::m2v2::ProjectionMode;
  switch (mode) {
    case ProjectionMode::kFullRefresh: return "full_refresh";
    case ProjectionMode::kPartialUpdate: return "partial_update";
    case ProjectionMode::kCleanFullRefresh: return "clean_full_refresh";
  }
  return "unknown";
}

}  // namespace

int main(int argc, char** argv) {
  if (argc != 2) {
    std::cerr << "usage: m2_v2_decode_fixture PACKET\n";
    return 2;
  }
  try {
    std::ifstream input(argv[1], std::ios::binary);
    if (!input) throw std::runtime_error("cannot open packet");
    const std::vector<std::uint8_t> packet(
        (std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>{});
    const auto request = threedsg::m2v2::decode_integrate_project(packet);
    std::size_t hit_count = 0;
    for (const auto& point : request.points) hit_count += point.has_hit ? 1U : 0U;
    std::cout << std::setprecision(9)
              << "request_id=" << request.request_id << '\n'
              << "frame_id=" << request.frame_id << '\n'
              << "frame_index=" << request.frame_index << '\n'
              << "capture_monotonic_ns=" << request.capture_monotonic_ns << '\n'
              << "projection_mode=" << mode_name(request.projection_mode) << '\n'
              << "point_count=" << request.points.size() << '\n'
              << "hit_count=" << hit_count << '\n'
              << "no_return_count=" << request.points.size() - hit_count << '\n'
              << "max_range_m=" << request.max_range_m << '\n'
              << "origin_xyz=" << request.origin_xyz[0] << ','
              << request.origin_xyz[1] << ',' << request.origin_xyz[2] << '\n';
    if (!request.points.empty()) {
      const auto& first = request.points.front();
      const auto& last = request.points.back();
      std::cout << "first_xyz=" << first.x << ',' << first.y << ',' << first.z << '\n'
                << "last_xyz=" << last.x << ',' << last.y << ',' << last.z << '\n';
    }
    std::cout << "package_sha256=";
    for (const auto byte : request.package_sha256) {
      std::cout << std::hex << std::setw(2) << std::setfill('0')
                << static_cast<unsigned>(byte);
    }
    std::cout << '\n';
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "decode failed: " << error.what() << '\n';
    return 1;
  }
}
