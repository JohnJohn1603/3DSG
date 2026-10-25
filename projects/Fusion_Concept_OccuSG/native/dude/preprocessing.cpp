#include "preprocessing.hpp"
#include <opencv2/imgproc.hpp>
#include <stdexcept>

namespace {
cv::Mat clean_image2(const cv::Mat& occ, cv::Mat& black_image) {
  cv::Mat open_space = occ < 10;
  black_image = (occ > 90) & (occ <= 100);
  cv::Mat median_image, out_image, temp_image;
  int filter_size = 2;
  cv::boxFilter(black_image, temp_image, -1, cv::Size(filter_size, filter_size),
                cv::Point(-1,-1), false, cv::BORDER_DEFAULT);
  black_image = temp_image > (filter_size*filter_size/2);
  cv::dilate(black_image, black_image, cv::Mat(), cv::Point(-1,-1), 4,
             cv::BORDER_CONSTANT, cv::morphologyDefaultBorderValue());
  filter_size = 10;
  cv::boxFilter(open_space, temp_image, -1, cv::Size(filter_size, filter_size),
                cv::Point(-1,-1), false, cv::BORDER_DEFAULT);
  median_image = temp_image > (filter_size*filter_size/2);
  median_image = median_image | open_space;
  cv::dilate(median_image, median_image, cv::Mat());
  out_image = median_image & ~black_image;
  return out_image;
}
}

M4Preprocessed m4_preprocess(const std::vector<std::int8_t>& values,
                             int width, int height, float resolution,
                             float threshold_m,
                             bool preserve_unknown_boundary) {
  if (width <= 0 || height <= 0 || values.size() != static_cast<size_t>(width)*height)
    throw std::invalid_argument("grid shape/value count mismatch");
  cv::Mat received(height, width, CV_8UC1);
  for (int y=0; y<height; ++y) for (int x=0; x<width; ++x)
    received.at<std::uint8_t>(y,x) = static_cast<std::uint8_t>(values[static_cast<size_t>(y)*width+x]);
  // Exact active-source test: byte 255 (unknown -1) is excluded; 101 is not an unknown mapping.
  cv::Mat valid_image = received < 101;
  std::vector<std::vector<cv::Point>> contours;
  cv::findContours(valid_image, contours, cv::RETR_EXTERNAL, cv::CHAIN_APPROX_SIMPLE);
  cv::Rect crop(0,0,width,height);
  if (!contours.empty()) {
    crop = cv::boundingRect(contours.front());
    for (size_t i=1; i<contours.size(); ++i) crop |= cv::boundingRect(contours[i]);
  }
  if (crop.width <= 0 || crop.height <= 0) throw std::runtime_error("empty source valid-image crop");
  cv::Mat cropped = received(crop).clone(), black2;
  cv::Mat cleaned2 = clean_image2(cropped, black2);
  if (preserve_unknown_boundary) {
    // Extended policy only: source morphology may dilate free pixels into
    // the unknown part of M3.  Keep the source pipeline untouched by making
    // this an explicit opt-in mask policy rather than a default rewrite.
    cleaned2.setTo(0, cropped >= 101);
  }
  M4Preprocessed result;
  result.image_cleaned = cv::Mat::zeros(height, width, CV_8UC1);
  result.black_image = cv::Mat::zeros(height, width, CV_8UC1);
  cleaned2.copyTo(result.image_cleaned(crop));
  black2.copyTo(result.black_image(crop));
  result.crop = crop;
  result.pixel_tau = threshold_m / resolution;
  return result;
}
