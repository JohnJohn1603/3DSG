#include "MapConverter.hh"
#include "octomap/octomap_types.h"
#include "octomap_msgs/msg/octomap.hpp"
#include <cmath>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <limits>
#include <mapconversion_msgs/msg/height_map.hpp>
#include <mapconversion_msgs/msg/slope_map.hpp>
#include <memory>
#include <nav_msgs/msg/occupancy_grid.hpp>
#include <nav_msgs/msg/odometry.hpp>
#include <octomap/AbstractOcTree.h>
#include <octomap/OcTree.h>
#include <octomap/octomap.h>
#include <octomap_msgs/conversions.h>
#include <octomap_msgs/msg/octomap.h>
#include <rclcpp/executors.hpp>
#include <rclcpp/logging.hpp>
#include <rclcpp/rclcpp.hpp>
#include <rclcpp/utilities.hpp>
#include <rmw/types.h>
#include <sstream>
#include <sys/stat.h>
#include <thread>
#include <unistd.h>
#include <vector>

using namespace std;

namespace {
class RuntimeProfiler {
public:
  RuntimeProfiler(const string &node_name, const string &package_name,
                  const string &file_tag, bool enabled, const string &output_path,
                  const string &run_name, bool save_on_shutdown, int discard_first_n)
      : node_name_(node_name), package_name_(package_name), file_tag_(file_tag),
        enabled_(enabled), output_path_(output_path), run_name_(run_name),
        save_on_shutdown_(save_on_shutdown),
        discard_first_n_(std::max(0, discard_first_n)), started_at_(timestamp()) {}

  ~RuntimeProfiler() {
    if (save_on_shutdown_)
      save();
  }

  void record(const string &stage_name, double elapsed_ms) {
    if (!enabled_ || !std::isfinite(elapsed_ms))
      return;
    samples_[stage_name].push_back(elapsed_ms);
  }

  void save() {
    if (!enabled_ || saved_)
      return;
    const string base_path = output_path_.empty() ? "." : output_path_;
    makeDirectories(base_path);
    const string path = base_path + "/" + run_name_ + "." + file_tag_ + ".json";
    ofstream out(path);
    if (!out)
      return;
    ended_at_ = timestamp();
    out << "{\n";
    out << "  \"run_name\": " << jsonString(run_name_) << ",\n";
    out << "  \"node_name\": " << jsonString(node_name_) << ",\n";
    out << "  \"package_name\": " << jsonString(package_name_) << ",\n";
    out << "  \"discarded_warmup_count\": " << discard_first_n_ << ",\n";
    out << "  \"started_at\": " << jsonString(started_at_) << ",\n";
    out << "  \"ended_at\": " << jsonString(ended_at_) << ",\n";
    out << "  \"metadata\": " << metadataJson() << ",\n";
    out << "  \"stages\": {\n";
    size_t stage_index = 0;
    for (const auto &entry : samples_) {
      out << "    " << jsonString(entry.first) << ": {\n";
      out << "      \"samples_ms\": [";
      for (size_t i = 0; i < entry.second.size(); ++i) {
        if (i > 0)
          out << ", ";
        out << std::fixed << std::setprecision(6) << entry.second[i];
      }
      out << "],\n";
      out << "      \"summary\": " << summaryJson(entry.second) << "\n";
      out << "    }" << (++stage_index < samples_.size() ? "," : "") << "\n";
    }
    out << "  }\n";
    out << "}\n";
    saved_ = true;
  }

private:
  static string timestamp() {
    const auto now = std::time(nullptr);
    std::tm tm{};
    gmtime_r(&now, &tm);
    char buffer[32];
    std::strftime(buffer, sizeof(buffer), "%Y-%m-%dT%H:%M:%SZ", &tm);
    return string(buffer);
  }

  static string jsonString(const string &value) {
    ostringstream out;
    out << '"';
    for (const char c : value) {
      switch (c) {
      case '"': out << "\\\""; break;
      case '\\': out << "\\\\"; break;
      case '\n': out << "\\n"; break;
      case '\r': out << "\\r"; break;
      case '\t': out << "\\t"; break;
      default: out << c; break;
      }
    }
    out << '"';
    return out.str();
  }

  static void makeDirectories(const string &path) {
    if (path.empty())
      return;
    string current;
    for (const char c : path) {
      current.push_back(c);
      if (c == '/' && current.size() > 1)
        mkdir(current.c_str(), 0755);
    }
    mkdir(path.c_str(), 0755);
  }

  static string firstCpuModel() {
    ifstream in("/proc/cpuinfo");
    string line;
    while (std::getline(in, line)) {
      const string key = "model name";
      if (line.compare(0, key.size(), key) == 0) {
        const auto pos = line.find(':');
        if (pos != string::npos)
          return line.substr(pos + 2);
      }
    }
    return "";
  }

  static double ramGb() {
    ifstream in("/proc/meminfo");
    string key, unit;
    double kb = 0.0;
    while (in >> key >> kb >> unit) {
      if (key == "MemTotal:")
        return kb / (1024.0 * 1024.0);
    }
    return 0.0;
  }

  string metadataJson() const {
    char hostname[256] = "";
    gethostname(hostname, sizeof(hostname) - 1);
    const char *ros_distro = std::getenv("ROS_DISTRO");
    ostringstream out;
    out << "{";
    out << "\"hostname\": " << jsonString(hostname) << ", ";
    out << "\"cpu_model\": " << jsonString(firstCpuModel()) << ", ";
    out << "\"logical_cores\": " << std::thread::hardware_concurrency() << ", ";
    out << "\"ram_gb\": " << std::fixed << std::setprecision(3) << ramGb() << ", ";
    out << "\"ros_distro\": " << jsonString(ros_distro ? ros_distro : "") << ", ";
    out << "\"date_time\": " << jsonString(timestamp()) << ", ";
    out << "\"free_space_projection_definition\": "
        << jsonString("update2Dmap plus publication work in mapCallback");
    out << "}";
    return out.str();
  }

  string summaryJson(const vector<double> &raw_values) const {
    vector<double> values;
    for (size_t i = static_cast<size_t>(discard_first_n_); i < raw_values.size(); ++i)
      values.push_back(raw_values[i]);
    if (values.empty())
      return "{\"n\": 0, \"mean_ms\": null, \"std_ms\": null, \"min_ms\": null, \"max_ms\": null}";
    double sum = 0.0, min_value = values.front(), max_value = values.front();
    for (const double value : values) {
      sum += value;
      min_value = std::min(min_value, value);
      max_value = std::max(max_value, value);
    }
    const double mean = sum / static_cast<double>(values.size());
    double variance = 0.0;
    for (const double value : values)
      variance += (value - mean) * (value - mean);
    variance /= static_cast<double>(values.size());
    ostringstream out;
    out << std::fixed << std::setprecision(6)
        << "{\"n\": " << values.size()
        << ", \"mean_ms\": " << mean
        << ", \"std_ms\": " << std::sqrt(std::max(0.0, variance))
        << ", \"min_ms\": " << min_value
        << ", \"max_ms\": " << max_value << "}";
    return out.str();
  }

  string node_name_;
  string package_name_;
  string file_tag_;
  bool enabled_{false};
  string output_path_;
  string run_name_;
  bool save_on_shutdown_{true};
  int discard_first_n_{0};
  string started_at_;
  string ended_at_;
  bool saved_{false};
  map<string, vector<double>> samples_;
};
} // namespace

class MapToMap : public rclcpp::Node {
private:
  // subs
  rclcpp::Subscription<octomap_msgs::msg::Octomap>::SharedPtr subOctMap;

  // pub
  rclcpp::Publisher<nav_msgs::msg::OccupancyGrid>::SharedPtr pubMapUGV,
      pubMapUAV, pubMapFloor, pubMapCeiling, pubMapSlopeVis;
  rclcpp::Publisher<mapconversion_msgs::msg::HeightMap>::SharedPtr pubHeightMap;
  rclcpp::Publisher<mapconversion_msgs::msg::SlopeMap>::SharedPtr pubMapSlope;
  // msg
  nav_msgs::msg::OccupancyGrid mapMsg;
  mapconversion_msgs::msg::HeightMap heightMsg;
  mapconversion_msgs::msg::SlopeMap slopeMsg;

  std::unique_ptr<MapConverter> MC;
  std::unique_ptr<octomap::OcTree> OcMap;

  double resolution;
  double slopeMax;
  int slopeEstimationSize;
  double minimumZ;
  int minimumOccupancy;
  string mapFrame;
  double mapZpos;
  bool partial_map_updates;
  bool sub_qos_reliable;
  bool sub_qos_transient_local;
  bool pub_qos_reliable;
  bool pub_qos_transient_local;
  std::unique_ptr<RuntimeProfiler> profiler_;

public:
  MapToMap() : Node("map_conversion") {
    slopeMax = this->declare_parameter("max_slope_ugv", INFINITY);
    slopeEstimationSize = this->declare_parameter("slope_estimation_size", 1);
    slopeEstimationSize = max(slopeEstimationSize, 2);
    minimumZ = this->declare_parameter("minimum_z", 1.0);
    minimumOccupancy = this->declare_parameter("minimum_occupancy", 10);
    mapFrame = this->declare_parameter("map_frame", string("map"));
    mapZpos = this->declare_parameter("map_position_z", 0.0);
    partial_map_updates = this->declare_parameter("partial_map_updates", false);
    sub_qos_reliable = this->declare_parameter("subscriber_qos_reliable", true);
    pub_qos_reliable = this->declare_parameter("publisher_qos_reliable", true);
    sub_qos_transient_local =
        this->declare_parameter("subscriber_qos_transient_local", false);
    pub_qos_transient_local =
        this->declare_parameter("publisher_qos_transient_local", false);
    const bool enable_profiling =
        this->declare_parameter<bool>("enable_profiling", false);
    const string profiling_output_path =
        this->declare_parameter<string>("profiling_output_path", "");
    const string profiling_run_name =
        this->declare_parameter<string>("profiling_run_name", "run");
    const bool profiling_save_on_shutdown =
        this->declare_parameter<bool>("profiling_save_on_shutdown", true);
    const int profiling_discard_first_n =
        this->declare_parameter<int>("profiling_discard_first_n", 5);
    profiler_ = std::make_unique<RuntimeProfiler>(
        "map_conversion_node", "mapconversion", "mapconversion",
        enable_profiling, profiling_output_path, profiling_run_name,
        profiling_save_on_shutdown, profiling_discard_first_n);

    // QoS profiles
    rclcpp::QoS sub_qos_profile = rclcpp::QoS(rclcpp::KeepLast(5));
    if (sub_qos_reliable)
      sub_qos_profile.reliability(RMW_QOS_POLICY_RELIABILITY_RELIABLE);
    else
      sub_qos_profile.reliability(RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT);
    if (sub_qos_transient_local)
      sub_qos_profile.durability(RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL);
    else
      sub_qos_profile.durability(RMW_QOS_POLICY_DURABILITY_VOLATILE);

    rclcpp::QoS pub_qos_profile = rclcpp::QoS(rclcpp::KeepLast(5));
    if (pub_qos_reliable)
      pub_qos_profile.reliability(RMW_QOS_POLICY_RELIABILITY_RELIABLE);
    else
      pub_qos_profile.reliability(RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT);
    if (pub_qos_transient_local)
      pub_qos_profile.durability(RMW_QOS_POLICY_DURABILITY_TRANSIENT_LOCAL);
    else
      pub_qos_profile.durability(RMW_QOS_POLICY_DURABILITY_VOLATILE);

    // Subscribers and publishers
    subOctMap = this->create_subscription<octomap_msgs::msg::Octomap>(
        "octomap", sub_qos_profile,
        std::bind(&MapToMap::mapCallback, this, std::placeholders::_1));
    pubMapUGV = this->create_publisher<nav_msgs::msg::OccupancyGrid>(
        "/mapUGV", pub_qos_profile);
    pubMapUAV = this->create_publisher<nav_msgs::msg::OccupancyGrid>(
        "/mapUAV", pub_qos_profile);
    pubMapFloor = this->create_publisher<nav_msgs::msg::OccupancyGrid>(
        "/visualization_floor_map", 5);
    pubMapCeiling = this->create_publisher<nav_msgs::msg::OccupancyGrid>(
        "/visualization_ceiling_map", 5);
    pubHeightMap = this->create_publisher<mapconversion_msgs::msg::HeightMap>(
        "/heightMap", pub_qos_profile);
    pubMapSlopeVis = this->create_publisher<nav_msgs::msg::OccupancyGrid>(
        "/visualization_slope_map", 5);
    pubMapSlope = this->create_publisher<mapconversion_msgs::msg::SlopeMap>(
        "/slopeMap", pub_qos_profile);
  }

  ~MapToMap() = default;

  void mapCallback(const octomap_msgs::msg::Octomap::SharedPtr msg) {
    const auto callback_start = std::chrono::steady_clock::now();
    if (!std::isfinite(msg->resolution) || msg->resolution <= 0.0) {
      RCLCPP_ERROR(this->get_logger(),
                   "Received octomap with invalid resolution %.6f",
                   msg->resolution);
      return;
    }

    if (MC == nullptr) {
      resolution = msg->resolution;
      MC = std::unique_ptr<MapConverter>(new MapConverter(
          resolution, slopeEstimationSize, minimumZ, minimumOccupancy));
    } else if (std::abs(msg->resolution - resolution) > 1e-9) {
      RCLCPP_WARN(this->get_logger(),
                  "Octomap resolution changed from %.6f to %.6f; rebuilding "
                  "the converted map",
                  resolution, msg->resolution);
      resolution = msg->resolution;
      MC = std::unique_ptr<MapConverter>(new MapConverter(
          resolution, slopeEstimationSize, minimumZ, minimumOccupancy));
      OcMap.reset();
    }

    // Convert ROS message to Octomap
    const auto msg_to_map_start = std::chrono::steady_clock::now();
    std::unique_ptr<octomap::AbstractOcTree> tree(octomap_msgs::msgToMap(*msg));
    profiler_->record(
        "mapconversion_msg_to_map_ms",
        std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - msg_to_map_start).count());
    if (tree == nullptr) {
      RCLCPP_ERROR(this->get_logger(),
                   "Failed to convert octomap message: msgToMap returned NULL");
      return;
    }
    octomap::OcTree *newOcMap = dynamic_cast<octomap::OcTree *>(tree.get());
    if (!(newOcMap)) {
      RCLCPP_ERROR(this->get_logger(),
                   "Failed to cast converted octomap type '%s' to OcTree",
                   msg->id.c_str());
      return;
    }

    // Check if tree is empty
    if (newOcMap->size() == 0) {
      RCLCPP_WARN_THROTTLE(this->get_logger(), *this->get_clock(), 5000,
                            "Received empty octomap, skipping processing");
      return;
    }

    double min_x, min_y, min_z;
    double max_x, max_y, max_z;

    newOcMap->getMetricMin(min_x, min_y, min_z);
    newOcMap->getMetricMax(max_x, max_y, max_z);

    // Validate min/max values (check for NaN and invalid ranges)
    if (std::isnan(min_x) || std::isnan(min_y) || std::isnan(min_z) ||
        std::isnan(max_x) || std::isnan(max_y) || std::isnan(max_z)) {
      RCLCPP_ERROR(this->get_logger(), "Octomap contains NaN values in bounds");
      return;
    }

    if (min_x > max_x || min_y > max_y || min_z > max_z) {
      RCLCPP_ERROR(this->get_logger(),
                   "Invalid octomap bounds: min > max (x: %.2f > %.2f, y: %.2f > %.2f, z: %.2f > %.2f)",
                   min_x, max_x, min_y, max_y, min_z, max_z);
      return;
    }

    vector<double> minMax(6);
    bool has_update_region = true;
    if (OcMap == nullptr || !partial_map_updates) {
      minMax[0] = min_x;
      minMax[1] = max_x;
      minMax[2] = min_y;
      minMax[3] = max_y;
      OcMap.reset(newOcMap);
      tree.release();
    } else {
      const auto bounding_start = std::chrono::steady_clock::now();
      has_update_region = computeBoundingBox(minMax, newOcMap, OcMap.get());
      profiler_->record(
          "mapconversion_compute_bounding_box_ms",
          std::chrono::duration<double, std::milli>(
              std::chrono::steady_clock::now() - bounding_start).count());
      OcMap.reset(newOcMap);
      tree.release();
    }
    minMax[4] = min_z;
    minMax[5] = max_z;

    double update2d_ms = 0.0;
    if (has_update_region) {
      const auto update_start = std::chrono::steady_clock::now();
      update2Dmap(minMax);
      update2d_ms = std::chrono::duration<double, std::milli>(
          std::chrono::steady_clock::now() - update_start).count();
      profiler_->record("mapconversion_update2dmap_ms", update2d_ms);
    }
    const auto publish_start = std::chrono::steady_clock::now();
    pub();
    const double publish_ms = std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now() - publish_start).count();
    profiler_->record("mapconversion_publish_ms", publish_ms);
    profiler_->record("free_space_projection_ms", update2d_ms + publish_ms);
    profiler_->record(
        "mapconversion_callback_total_ms",
        std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - callback_start).count());
  }

  bool computeBoundingBox(vector<double> &minMax, octomap::OcTree *tree1,
                          octomap::OcTree *tree2) {
    // Variables to hold min and max coordinates
    double min_x = INFINITY;
    double min_y = INFINITY;
    double min_z = INFINITY;
    double max_x = -INFINITY;
    double max_y = -INFINITY;
    double max_z = -INFINITY;
    bool changed = false;

    // Iterate over all new leaf nodes and find added or occupancy-changed cells.
    for (octomap::OcTree::leaf_iterator it = tree1->begin_leafs(),
                                        end = tree1->end_leafs();
         it != end; ++it) {
      octomap::OcTreeKey key = it.getKey();
      octomap::OcTreeNode *node2 = tree2->search(key);
      if (node2 != nullptr) {
        bool occupied1 = tree1->isNodeOccupied(*it);
        bool occupied2 = tree2->isNodeOccupied(node2);
        if (occupied1 != occupied2) {
          // Node occupancy has changed
          double x = it.getX();
          double y = it.getY();
          double z = it.getZ();
          updateBoundingBox(x, y, z, min_x, min_y, min_z, max_x, max_y, max_z);
          changed = true;
        }
      } else {
        // Node not found in tree2, so it is new.
        double x = it.getX();
        double y = it.getY();
        double z = it.getZ();
        updateBoundingBox(x, y, z, min_x, min_y, min_z, max_x, max_y, max_z);
        changed = true;
      }
    }

    // Iterate over old leaf nodes to find cells removed from the new map.
    for (octomap::OcTree::leaf_iterator it = tree2->begin_leafs(),
                                        end = tree2->end_leafs();
         it != end; ++it) {
      octomap::OcTreeKey key = it.getKey();
      if (tree1->search(key) != nullptr)
        continue;

      double x = it.getX();
      double y = it.getY();
      double z = it.getZ();
      updateBoundingBox(x, y, z, min_x, min_y, min_z, max_x, max_y, max_z);
      changed = true;
    }

    if (!changed)
      return false;

    minMax[0] = min_x;
    minMax[1] = max_x;
    minMax[2] = min_y;
    minMax[3] = max_y;
    minMax[4] = min_z;
    minMax[5] = max_z;
    return true;
  }

  void updateBoundingBox(double x, double y, double z, double &min_x,
                         double &min_y, double &min_z, double &max_x,
                         double &max_y, double &max_z) {
    if (x < min_x)
      min_x = x;
    if (y < min_y)
      min_y = y;
    if (z < min_z)
      min_z = z;
    if (x > max_x)
      max_x = x;
    if (y > max_y)
      max_y = y;
    if (z > max_z)
      max_z = z;
  }

  // Update 2D map in the region of the aabb
  void update2Dmap(vector<double> minMax) {
    // Validate inputs
    if (OcMap == nullptr || MC == nullptr) {
      RCLCPP_ERROR(this->get_logger(), "OcMap or MC is NULL in update2Dmap");
      return;
    }

    // Check for infinity and NaN values
    for (size_t i = 0; i < minMax.size(); ++i) {
      if (std::isinf(minMax[i]) || std::isnan(minMax[i])) {
        RCLCPP_DEBUG(this->get_logger(),
                     "Invalid minMax value at index %zu: %.2f (inf=%d, nan=%d)",
                     i, minMax[i], std::isinf(minMax[i]), std::isnan(minMax[i]));
        return;
      }
    }

    vector<voxel> voxelList;
    // get all free and occupide voxels in boundign box
    octomap::point3d minPoint(minMax[0], minMax[2], minMax[4]);
    octomap::point3d maxPoint(minMax[1], minMax[3], minMax[5]);

    // Validate bounding box points
    if (minPoint.x() > maxPoint.x() || minPoint.y() > maxPoint.y() || minPoint.z() > maxPoint.z()) {
      RCLCPP_ERROR(this->get_logger(),
                   "Invalid bounding box: min point (%.2f, %.2f, %.2f) > max point (%.2f, %.2f, %.2f)",
                   minPoint.x(), minPoint.y(), minPoint.z(),
                   maxPoint.x(), maxPoint.y(), maxPoint.z());
      return;
    }

    for (auto it = OcMap->begin_leafs_bbx(minPoint, maxPoint),
              it_end = OcMap->end_leafs_bbx();
         it != it_end; ++it) {
      voxel v;
      v.position.x = it.getX();
      v.position.y = it.getY();
      v.position.z = it.getZ();
      v.halfSize = it.getSize() / 2;
      v.occupied = OcMap->isNodeOccupied(*it);
      voxelList.push_back(v);
    }
    const auto mc_update_start = std::chrono::steady_clock::now();
    MC->updateMap(voxelList, minMax);
    profiler_->record(
        "mapconversion_mc_update_map_ms",
        std::chrono::duration<double, std::milli>(
            std::chrono::steady_clock::now() - mc_update_start).count());
  }

  void pub() {
    // Safety check for null pointers
    if (MC == nullptr || OcMap == nullptr) {
      RCLCPP_ERROR(this->get_logger(), "MC or OcMap is NULL in pub()");
      return;
    }

    mapMsg.header.stamp = this->now();
    mapMsg.header.frame_id = mapFrame;
    heightMsg.header = mapMsg.header;
    const int map_width = MC->map.sizeX();
    const int map_height = MC->map.sizeY();
    const double map_resolution = MC->map.getResulution();
    if (map_width <= 0 || map_height <= 0 || map_resolution <= 0 ||
        !std::isfinite(map_resolution)) {
      RCLCPP_ERROR(this->get_logger(),
                   "Invalid map dimensions: width=%d, height=%d, "
                   "resolution=%.4f",
                   map_width, map_height, map_resolution);
      return;
    }

    const auto width = static_cast<size_t>(map_width);
    const auto height = static_cast<size_t>(map_height);
    if (width > std::numeric_limits<uint32_t>::max() ||
        height > std::numeric_limits<uint32_t>::max() ||
        width > std::numeric_limits<size_t>::max() / height) {
      RCLCPP_ERROR(this->get_logger(),
                   "Map dimensions are too large to publish: width=%d, "
                   "height=%d",
                   map_width, map_height);
      return;
    }

    const size_t cell_count = width * height;
    mapMsg.info.width = static_cast<uint32_t>(map_width);
    mapMsg.info.height = static_cast<uint32_t>(map_height);
    mapMsg.info.resolution = MC->map.getResulution();

    mapMsg.info.origin.position.x = MC->map.offsetX();
    mapMsg.info.origin.position.y = MC->map.offsetY();
    mapMsg.info.origin.position.z = mapZpos;
    mapMsg.info.origin.orientation.x = 0;
    mapMsg.info.origin.orientation.y = 0;
    mapMsg.info.origin.orientation.z = 0;
    mapMsg.info.origin.orientation.w = 1;
    heightMsg.info = mapMsg.info;
    slopeMsg.info = mapMsg.info;
    mapMsg.data.resize(cell_count);
    heightMsg.top.resize(cell_count);
    heightMsg.bottom.resize(cell_count);
    slopeMsg.slope.resize(cell_count);

    // pub map for UGV
    if (pubMapUGV->get_subscription_count() != 0 || pub_qos_transient_local) {
      if (isinf(slopeMax)) {
        RCLCPP_WARN(
            this->get_logger(),
            "The max slope UGV is not set; obstacles will not be included in "
            "the UGV map. \nAdd \'_max_slope_ugv:=x\' to rosrun command, or "
            "\'<param name=\"max_slope_ugv\" value=\"x\"/>\' in launch file.");
      }

      mapMsg.header.stamp = this->now();
      for (int y = 0; y < MC->map.sizeY(); y++) {
        for (int x = 0; x < MC->map.sizeX(); x++) {
          size_t index = static_cast<size_t>(x) +
                         static_cast<size_t>(y) * width;

          mapMsg.data[index] = MC->map.get(x, y, slopeMax);
        }
      }
      pubMapUGV->publish(mapMsg);
    }

    // pub map for UAV
    if (pubMapUAV->get_subscription_count() != 0 || pub_qos_transient_local) {
      for (int y = 0; y < MC->map.sizeY(); y++) {
        for (int x = 0; x < MC->map.sizeX(); x++) {
          size_t index = static_cast<size_t>(x) +
                         static_cast<size_t>(y) * width;

          mapMsg.data[index] = MC->map.get(x, y);
        }
      }
      pubMapUAV->publish(mapMsg);
    }

    // pub rviz visualization for floor heigth map
    if (pubMapFloor->get_subscription_count() != 0) {
      if (MC->map.sizeY() == 0 || MC->map.sizeX() == 0)
        return;
      double vMax = NAN, vMin = NAN;
      // find max min for normalization
      for (int y = 0; y < MC->map.sizeY(); y++) {
        for (int x = 0; x < MC->map.sizeX(); x++) {
          double v = MC->map.getHeight(x, y);
          if (isnan(v))
            continue;
          if (isnan(vMax))
            vMax = v;
          else
            vMax = max(vMax, v);
          if (isnan(vMin))
            vMin = v;
          else
            vMin = min(vMin, v);
        }
      }

      for (int y = 0; y < MC->map.sizeY(); y++) {
        for (int x = 0; x < MC->map.sizeX(); x++) {
          size_t index = static_cast<size_t>(x) +
                         static_cast<size_t>(y) * width;
          double value = MC->map.getHeight(x, y);
          if (!isnan(value)) {
            value = (value - vMin) / (vMax - vMin) * 199;
            if (value > 99)
              value -= 199; // To get the full color range in the rviz cost map
          }
          mapMsg.data[index] = value;
        }
      }

      pubMapFloor->publish(mapMsg);
    }
    // pub rviz visualization for cieling heigth map
    if (pubMapCeiling->get_subscription_count() != 0) {
      if (MC->map.sizeY() == 0 || MC->map.sizeX() == 0)
        return;
      double vMax = NAN, vMin = NAN;
      // find max min for normalization
      for (int y = 0; y < MC->map.sizeY(); y++) {
        for (int x = 0; x < MC->map.sizeX(); x++) {
          double v = MC->map.getHeightTop(x, y);
          if (isnan(v))
            continue;
          if (isnan(vMax))
            vMax = v;
          else
            vMax = max(vMax, v);
          if (isnan(vMin))
            vMin = v;
          else
            vMin = min(vMin, v);
        }
      }

      for (int y = 0; y < MC->map.sizeY(); y++) {
        for (int x = 0; x < MC->map.sizeX(); x++) {
          size_t index = static_cast<size_t>(x) +
                         static_cast<size_t>(y) * width;
          double value = MC->map.getHeightTop(x, y);
          if (!isnan(value)) {
            value = (value - vMin) / (vMax - vMin) * 199;
            if (value > 99)
              value -= 199; // To get the full color range in the rviz cost map
          }
          mapMsg.data[index] = value;
        }
      }

      pubMapCeiling->publish(mapMsg);
    }

    // pub height map
    if (pubHeightMap->get_subscription_count() != 0 ||
        pub_qos_transient_local) {
      for (int y = 0; y < MC->map.sizeY(); y++) {
        for (int x = 0; x < MC->map.sizeX(); x++) {
          size_t index = static_cast<size_t>(x) +
                         static_cast<size_t>(y) * width;

          heightMsg.bottom[index] = MC->map.getHeight(x, y);
          heightMsg.top[index] = MC->map.getHeightTop(x, y);
        }
      }
      pubHeightMap->publish(heightMsg);
    }

    // pub rviz visualization fro slope map
    if (pubMapSlopeVis->get_subscription_count() != 0) {
      double vMax = slopeMax * 2, vMin = 0;

      for (int y = 0; y < MC->map.sizeY(); y++) {
        for (int x = 0; x < MC->map.sizeX(); x++) {
          size_t index = static_cast<size_t>(x) +
                         static_cast<size_t>(y) * width;
          double value = min(MC->map.getSlope(x, y), slopeMax * 2);
          if (!isnan(value)) {
            value = (value - vMin) / (vMax - vMin) * 198 + 1;
            if (value > 99)
              value -= 199; // soooo basically, rviz costmap are weird...
          }
          mapMsg.data[index] = value;
        }
      }
      pubMapSlopeVis->publish(mapMsg);
    }

    // pub slope map
    if (pubMapSlope->get_subscription_count() != 0 || pub_qos_transient_local) {
      for (int y = 0; y < MC->map.sizeY(); y++) {
        for (int x = 0; x < MC->map.sizeX(); x++) {
          size_t index = static_cast<size_t>(x) +
                         static_cast<size_t>(y) * width;

          slopeMsg.slope[index] = MC->map.getSlope(x, y);
        }
      }
      pubMapSlope->publish(slopeMsg);
    }
  }
};

int main(int argc, char **argv) {
  rclcpp::init(argc, argv);

  auto Node = make_shared<MapToMap>();
  rclcpp::spin(Node);
  rclcpp::shutdown();

  return 0;
}
