#include "inc_decomp.hpp"
#include "preprocessing.hpp"
#include <json/json.h>
#include <opencv2/imgproc.hpp>
#include <cmath>
#include <iostream>
#include <limits>
#include <new>
#include <set>
#include <sstream>
#include <string>

namespace {
constexpr int kProtocol = 1;
constexpr float kThresholdM = 1.5f;
constexpr float kResolutionDefault = 0.05f;
Incremental_Decomposer decomposer;
bool configured = false;
int last_width = 0, last_height = 0;
float last_resolution = 0, last_threshold = kThresholdM;
std::string last_preprocessing_policy = "source";

Json::Value error(const Json::Value& req, const std::string& code, const std::string& message) {
  Json::Value out; out["protocol_version"] = kProtocol;
  if (req.isMember("request_id")) out["request_id"] = req["request_id"];
  out["status"] = "error"; out["code"] = code; out["message"] = message; return out;
}
bool finite_number(const Json::Value& v) { return v.isNumeric() && std::isfinite(v.asDouble()); }
Json::Value process(const Json::Value& req) {
  if (!req.isObject() || !req.isMember("protocol_version") || !req["protocol_version"].isInt() || req["protocol_version"].asInt()!=kProtocol)
    return error(req, "protocol_version", "expected protocol_version=1");
  if (!req.isMember("op") || !req["op"].isString()) return error(req,"request_shape","op must be a string");
  if (req["op"].asString()=="close") { Json::Value r; r["protocol_version"]=kProtocol; r["status"]="closed"; return r; }
  if (req["op"].asString()=="reset") { decomposer.~Incremental_Decomposer(); new (&decomposer) Incremental_Decomposer(); configured=false; last_width=last_height=0; last_preprocessing_policy="source"; Json::Value r; r["protocol_version"]=kProtocol; r["status"]="reset"; return r; }
  if (req["op"].asString()!="integrate") return error(req,"unknown_operation","supported operations: integrate, reset, close");
  if (!req.isMember("request_id") || !req["request_id"].isString() || !req.isMember("revision") || !req["revision"].isInt64() || req["revision"].asInt64()<0)
    return error(req,"request_shape","request_id string and nonnegative integer revision required");
  if (!req.isMember("grid") || !req["grid"].isObject()) return error(req,"grid_shape","grid object required");
  const auto& g=req["grid"];
  if (!g["width"].isInt() || !g["height"].isInt() || !g["width"].asInt() || !g["height"].asInt()) return error(req,"grid_shape","positive integer width and height required");
  const int width=g["width"].asInt(), height=g["height"].asInt();
  if (width>10000 || height>10000 || static_cast<uint64_t>(width)*height>50000000ULL) return error(req,"grid_limit","grid exceeds worker limits");
  if (!finite_number(g["resolution_m"]) || g["resolution_m"].asDouble()<=0 || !g["origin_xy"].isArray() || g["origin_xy"].size()!=2 || !finite_number(g["origin_xy"][0]) || !finite_number(g["origin_xy"][1])) return error(req,"grid_geometry","finite positive resolution and finite origin_xy required");
  if (!g["values"].isArray() || g["values"].size()!=static_cast<Json::ArrayIndex>(width*height)) return error(req,"grid_values","row-major values count must equal width*height");
  std::vector<int8_t> values; values.reserve(g["values"].size());
  for (const auto& v:g["values"]) { if (!v.isInt() || v.asInt() < -1 || v.asInt()>100) return error(req,"grid_values","each occupancy value must be integer -1..100"); values.push_back(static_cast<int8_t>(v.asInt())); }
  float resolution=static_cast<float>(g["resolution_m"].asDouble()), threshold=kThresholdM;
  if (!req.isMember("config")) return error(req,"config_required","config is required on every integrate request");
  {
    const auto& c=req["config"];
    if (!c.isObject() || !finite_number(c["decomp_threshold_m"]) || c["decomp_threshold_m"].asDouble()<=0 || !finite_number(c["safety_distance_m"]) || c["safety_distance_m"].asDouble()<0) return error(req,"config_invalid","positive decomp_threshold_m and nonnegative safety_distance_m required");
    threshold=static_cast<float>(c["decomp_threshold_m"].asDouble());
    const float safety=static_cast<float>(c["safety_distance_m"].asDouble());
    if (configured && (threshold!=last_threshold || safety!=decomposer.safety_distance)) return error(req,"config_changed_requires_reset","decomposition parameters are fixed until reset");
    if (c.isMember("preprocessing_policy") && !c["preprocessing_policy"].isString()) return error(req,"config_invalid","preprocessing_policy must be a string");
    const std::string policy = c.get("preprocessing_policy", "source").asString();
    if (policy != "source" && policy != "extended_unknown_boundary") return error(req,"config_invalid","preprocessing_policy must be source or extended_unknown_boundary");
    if (configured && policy != last_preprocessing_policy) return error(req,"config_changed_requires_reset","preprocessing policy is fixed until reset");
    last_preprocessing_policy = policy;
    decomposer.safety_distance=safety;
  }
  if (configured && (resolution!=last_resolution)) return error(req,"resolution_changed_requires_reset","resolution change requires explicit reset and replay");
  if (!req.isMember("frame_id") || !req["frame_id"].isString()) return error(req,"grid_geometry","frame_id string required");
  if (req.isMember("debug_capture_preprocessing") && !req["debug_capture_preprocessing"].isBool())
    return error(req,"request_shape","debug_capture_preprocessing must be boolean");
  const bool capture_preprocessed=req.get("debug_capture_preprocessing",false).asBool();
  const bool preserve_unknown_boundary = last_preprocessing_policy == "extended_unknown_boundary";
  const auto prep=m4_preprocess(values,width,height,resolution,threshold,preserve_unknown_boundary);
  cv::Mat received(height, width, CV_8UC1);
  for (int y=0; y<height; ++y) for (int x=0; x<width; ++x)
    received.at<std::uint8_t>(y,x) = static_cast<std::uint8_t>(values[static_cast<size_t>(y)*width+x]);
  const cv::Point2f origin(static_cast<float>(g["origin_xy"][0].asDouble()),static_cast<float>(g["origin_xy"][1].asDouble()));
  const Stable_graph& stable=decomposer.decompose_image(prep.image_cleaned,prep.pixel_tau,origin,resolution);
  configured=true; last_width=width; last_height=height; last_resolution=resolution; last_threshold=threshold;
  Json::Value out; out["protocol_version"]=kProtocol; out["request_id"]=req["request_id"]; out["input_revision"]=req["revision"];
  out["protocol_status"]="ok"; out["branch"]=decomposer.last_branch_; out["rejection_reason"]=decomposer.last_rejection_reason_;
  const std::string branch=decomposer.last_branch_;
  const bool rejected=branch.rfind("reject_",0)==0;
  const bool fallback=branch=="fallback_full_decomposition" || branch=="accept_free_contours_fallback";
  out["status"]=rejected ? "rejected" : (fallback ? "fallback" : "success");
  out["diagnostics"]["kernel_invoked"]=(branch=="accept_incremental" || branch=="fallback_full_decomposition");
  out["frame_id"]=req["frame_id"]; out["units"]="meters"; out["coordinate_convention"]="world_x=origin_x+col*resolution; world_y=origin_y+row*resolution";
  out["diagnostics"]["crop"] = Json::Value(Json::arrayValue); for(int v:{prep.crop.x,prep.crop.y,prep.crop.width,prep.crop.height}) out["diagnostics"]["crop"].append(v);
  out["diagnostics"]["unknown_byte_value"]=255; out["diagnostics"]["pixel_tau"]=prep.pixel_tau;
  out["diagnostics"]["preprocessed_free_pixels"]=cv::countNonZero(prep.image_cleaned);
  out["diagnostics"]["black_pixels"]=cv::countNonZero(prep.black_image);
  out["diagnostics"]["preprocessing_policy"]=last_preprocessing_policy;
  cv::Mat unknown = received >= 101, unknown_overlap;
  cv::bitwise_and(prep.image_cleaned, unknown, unknown_overlap);
  out["diagnostics"]["preprocessed_unknown_pixels"]=cv::countNonZero(unknown_overlap);
  if (capture_preprocessed) {
    out["diagnostics"]["preprocessed_mask"]=Json::Value(Json::arrayValue);
    out["diagnostics"]["black_mask"]=Json::Value(Json::arrayValue);
    for(int y=0;y<height;++y) {
      Json::Value row(Json::arrayValue);
      Json::Value black_row(Json::arrayValue);
      for(int x=0;x<width;++x) {
        row.append(prep.image_cleaned.at<uint8_t>(y,x)!=0);
        black_row.append(prep.black_image.at<uint8_t>(y,x)!=0);
      }
      out["diagnostics"]["preprocessed_mask"].append(row);
      out["diagnostics"]["black_mask"].append(black_row);
    }
  }
  out["diagnostics"]["stale_or_reused"]=(decomposer.last_branch_.find("reuse")!=std::string::npos || decomposer.last_branch_.find("preserve_previous")!=std::string::npos);
  out["regions"]=Json::Value(Json::arrayValue);
  std::vector<int> raw_id_map(stable.Region_contour.size(),-1);
  int next_raw_id=0;
  for(size_t i=0;i<stable.Region_contour.size();++i) { const auto& c=stable.Region_contour[i]; if(c.size()>=3 && std::isfinite(cv::contourArea(c)) && std::abs(cv::contourArea(c))>1.0 && cv::boundingRect(c).area()>0) raw_id_map[i]=next_raw_id++; }
  std::vector<std::vector<int>> adjacency(stable.Region_contour.size());
  for(size_t i=0;i<stable.Region_contour.size();++i) for(size_t j=i+1;j<stable.Region_contour.size();++j) if(raw_id_map[i]>=0 && raw_id_map[j]>=0) { cv::Point c; int n=0; decomposer.are_contours_connected(stable.Region_contour[i],stable.Region_contour[j],c,n); if(n>0){adjacency[i].push_back(raw_id_map[j]);adjacency[j].push_back(raw_id_map[i]);} }
  for(size_t i=0;i<stable.Region_contour.size();++i) {
    const auto& contour=stable.Region_contour[i];
    if(raw_id_map[i]<0) continue;
    Json::Value r; r["raw_id"]=raw_id_map[i]; r["outer"]=Json::Value(Json::arrayValue);
    for(const auto& p:contour){Json::Value xy(Json::arrayValue); xy.append(origin.x+p.x*resolution); xy.append(origin.y+p.y*resolution); r["outer"].append(xy);}
    const auto m=cv::moments(contour,true); const cv::Point2d cent=m.m00 ? cv::Point2d(m.m10/m.m00,m.m01/m.m00) : cv::Point2d(contour.front());
    r["centroid_xy"][0]=origin.x+cent.x*resolution; r["centroid_xy"][1]=origin.y+cent.y*resolution;
    r["area_m2"]=std::abs(cv::contourArea(contour))*resolution*resolution;
    r["holes_supported"]=false; r["adjacent_raw_ids"]=Json::Value(Json::arrayValue);
    for(int j:adjacency[i]) r["adjacent_raw_ids"].append(j);
    out["regions"].append(r);
  }
  out["diagnostics"]["holes_contract"]="pinned Stable_graph/build_raw_region_message expose outer contours only; holes are not emitted";
  out["diagnostics"]["state_dimensions"][0]=last_width; out["diagnostics"]["state_dimensions"][1]=last_height;
  return out;
}
}

int main() {
  std::string line;
  while (std::getline(std::cin,line)) {
    Json::CharReaderBuilder builder; Json::Value request; std::string errs;
    Json::Value response;
    try {
      std::istringstream input(line);
      if (!Json::parseFromStream(builder,input,&request,&errs)) response=error(Json::Value(Json::objectValue),"json_parse",errs);
      else response=process(request);
    } catch (const std::exception& e) { response=error(request,"worker_exception",e.what()); }
      catch (...) { response=error(request,"worker_exception","unknown native exception"); }
    Json::StreamWriterBuilder writer; writer["indentation"]=""; std::cout<<Json::writeString(writer,response)<<'\n'<<std::flush;
    if (request.isMember("op") && request["op"].isString() && request["op"].asString()=="close") break;
  }
}
