// Direct oracle: pinned Incremental_Decomposer with independently reproduced
// ROS callback preprocessing and independent contour/world-coordinate mapping.
#include "inc_decomp.hpp"
#include <json/json.h>
#include <opencv2/imgproc.hpp>
#include <cmath>
#include <iostream>
#include <string>
#include <vector>

namespace {
std::vector<int8_t> fixture(const std::string& name, int width, int height) {
  std::vector<int8_t> values(static_cast<size_t>(width)*height, -1);
  auto fill=[&](int y0,int y1,int x0,int x1,int value=0) {
    for(int y=y0;y<y1;++y) for(int x=x0;x<x1;++x)
      values[static_cast<size_t>(y)*width+x]=static_cast<int8_t>(value);
  };
  if(name=="rectangle") fill(15,105,15,105);
  else if(name=="l_shape") { fill(15,105,15,48); fill(72,105,15,105); }
  else if(name=="doorway") { fill(15,105,10,52); fill(15,105,68,110); fill(52,68,48,72); }
  else if(name=="obstacle") { fill(15,105,15,105); fill(45,75,45,75,100); }
  else if(name=="unknown") {}
  else if(name=="t1_partial_free") fill(25,135,18,85);
  else if(name=="t2_room_expanded") fill(18,142,12,98);
  else if(name=="t3_doorway_revealed") {
    fill(18,142,12,98); fill(18,142,110,150); fill(70,90,95,116);
  }
  else if(name=="doorway_hidden") fill(40,140,15,90);
  else if(name=="doorway_revealed") {
    fill(40,140,15,90); fill(50,130,110,175); fill(85,95,90,111);
  }
  else throw std::runtime_error("unknown direct-source fixture: "+name);
  return values;
}

Json::Value run(const std::string& name, int width, int height, float resolution,
                float origin_x, float origin_y, int revision,
                Incremental_Decomposer& decomposer) {
  constexpr float threshold=1.5f;
  const auto values=fixture(name,width,height);
  // Independent reproduction of callback preprocessing; no worker helper or
  // worker request/response conversion is linked into this oracle path.
  cv::Mat raw(height,width,CV_8UC1);
  for(int y=0;y<height;++y) for(int x=0;x<width;++x)
    raw.at<uint8_t>(y,x)=static_cast<uint8_t>(values[static_cast<size_t>(y)*width+x]);
  cv::Mat valid=raw<101;
  std::vector<std::vector<cv::Point>> valid_contours;
  cv::findContours(valid,valid_contours,cv::RETR_EXTERNAL,cv::CHAIN_APPROX_SIMPLE);
  cv::Rect crop(0,0,width,height);
  if(!valid_contours.empty()) {
    crop=cv::boundingRect(valid_contours.front());
    for(size_t i=1;i<valid_contours.size();++i) crop|=cv::boundingRect(valid_contours[i]);
  }
  cv::Mat occ=raw(crop).clone(), open_space=occ<10;
  cv::Mat black=(occ>90)&(occ<=100), temp, median_image;
  cv::boxFilter(black,temp,-1,cv::Size(2,2),cv::Point(-1,-1),false,cv::BORDER_DEFAULT);
  black=temp>2;
  cv::dilate(black,black,cv::Mat(),cv::Point(-1,-1),4,cv::BORDER_CONSTANT,cv::morphologyDefaultBorderValue());
  cv::boxFilter(open_space,temp,-1,cv::Size(10,10),cv::Point(-1,-1),false,cv::BORDER_DEFAULT);
  median_image=(temp>50)|open_space; cv::dilate(median_image,median_image,cv::Mat());
  cv::Mat cleaned=cv::Mat::zeros(height,width,CV_8UC1), cleaned_crop=median_image&~black;
  cleaned_crop.copyTo(cleaned(crop));

  const auto& graph=decomposer.decompose_image(cleaned,threshold/resolution,
                                                cv::Point2f(origin_x,origin_y),resolution);
  Json::Value out; out["input_revision"]=revision; out["branch"]=decomposer.last_branch_;
  out["status"]=decomposer.last_branch_.rfind("reject_",0)==0 ? "rejected" :
    ((decomposer.last_branch_=="fallback_full_decomposition" ||
      decomposer.last_branch_=="accept_free_contours_fallback") ? "fallback" : "success");
  out["diagnostics"]["preprocessed_free_pixels"]=cv::countNonZero(cleaned);
  out["diagnostics"]["preprocessed_mask"]=Json::Value(Json::arrayValue);
  for(int y=0;y<height;++y) { Json::Value row(Json::arrayValue);
    for(int x=0;x<width;++x) row.append(cleaned.at<uint8_t>(y,x)!=0);
    out["diagnostics"]["preprocessed_mask"].append(row); }
  out["diagnostics"]["crop"]=Json::Value(Json::arrayValue);
  for(int v:{crop.x,crop.y,crop.width,crop.height}) out["diagnostics"]["crop"].append(v);
  out["regions"]=Json::Value(Json::arrayValue);
  std::vector<int> ids;
  for(size_t i=0;i<graph.Region_contour.size();++i)
    if(graph.Region_contour[i].size()>=3 && std::abs(cv::contourArea(graph.Region_contour[i]))>1.0)
      ids.push_back(static_cast<int>(i));
  for(size_t k=0;k<ids.size();++k) {
    const auto& contour=graph.Region_contour[ids[k]]; Json::Value r;
    r["raw_id"]=static_cast<int>(k); r["outer"]=Json::Value(Json::arrayValue);
    for(const auto& p:contour) { Json::Value xy(Json::arrayValue);
      xy.append(origin_x+p.x*resolution); xy.append(origin_y+p.y*resolution); r["outer"].append(xy); }
    r["area_m2"]=std::abs(cv::contourArea(contour))*resolution*resolution;
    r["adjacent_raw_ids"]=Json::Value(Json::arrayValue);
    for(size_t j=0;j<ids.size();++j) if(j!=k) { cv::Point c; int n=0;
      decomposer.are_contours_connected(contour,graph.Region_contour[ids[j]],c,n);
      if(n>0) r["adjacent_raw_ids"].append(static_cast<int>(j)); }
    out["regions"].append(r);
  }
  return out;
}
void emit(const Json::Value& value) {
  Json::StreamWriterBuilder writer; writer["indentation"]="";
  std::cout<<Json::writeString(writer,value)<<'\n'<<std::flush;
}
}

int main(int argc,char** argv) {
  if(argc==2 && std::string(argv[1])=="demo_sequence") {
    Incremental_Decomposer live;
    const std::vector<std::string> frames={"unknown","t1_partial_free","t2_room_expanded",
                                           "t3_doorway_revealed","t1_partial_free"};
    for(size_t i=0;i<frames.size();++i)
      emit(run(frames[i],160,160,.05f,-4.f,-2.f,static_cast<int>(i+1),live));
    return 0;
  }
  if(argc==2 && std::string(argv[1])=="doorway_sequence") {
    Incremental_Decomposer live;
    emit(run("doorway_hidden",180,180,.05f,-4.f,-2.f,1,live));
    emit(run("doorway_revealed",180,180,.05f,-4.f,-2.f,2,live));
    return 0;
  }
  if(argc==2 && std::string(argv[1])=="demo_fresh_final") {
    Incremental_Decomposer fresh;
    emit(run("t3_doorway_revealed",160,160,.05f,-4.f,-2.f,4,fresh));
    return 0;
  }
  if(argc!=2) return 64;
  try {
    Incremental_Decomposer one;
    emit(run(argv[1],120,120,.05f,-1.f,2.f,1,one));
  } catch(const std::exception& e) { std::cerr<<e.what()<<'\n'; return 65; }
}
