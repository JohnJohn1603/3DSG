#pragma once
#include <cstdio>
#include <iostream>

// Logging-only replacement for ROS macros; diagnostics never use stdout.
#define M4_LOG_STREAM(level) (std::cerr << "[DuDe " level "] ")
#define DUDE_ROS_DEBUG_STREAM() M4_LOG_STREAM("DEBUG")
#define DUDE_ROS_INFO_STREAM() M4_LOG_STREAM("INFO")
#define DUDE_ROS_WARN_STREAM() M4_LOG_STREAM("WARN")
#define DUDE_ROS_ERROR_STREAM() M4_LOG_STREAM("ERROR")
#define DUDE_ROS_FATAL_STREAM() M4_LOG_STREAM("FATAL")
#define M4_LOGF(level, ...) do { std::fprintf(stderr, "[DuDe " level "] "); std::fprintf(stderr, __VA_ARGS__); std::fputc('\n', stderr); } while (0)
#define DUDE_ROS_DEBUGF(...) M4_LOGF("DEBUG", __VA_ARGS__)
#define DUDE_ROS_INFOF(...) M4_LOGF("INFO", __VA_ARGS__)
#define DUDE_ROS_WARNF(...) M4_LOGF("WARN", __VA_ARGS__)
#define DUDE_ROS_ERRORF(...) M4_LOGF("ERROR", __VA_ARGS__)
#define DUDE_ROS_FATALF(...) M4_LOGF("FATAL", __VA_ARGS__)
#define DUDE_ROS_DEBUGF_THROTTLE(ms, ...) DUDE_ROS_DEBUGF(__VA_ARGS__)
#define DUDE_ROS_INFOF_THROTTLE(ms, ...) DUDE_ROS_INFOF(__VA_ARGS__)
#define DUDE_ROS_WARNF_THROTTLE(ms, ...) DUDE_ROS_WARNF(__VA_ARGS__)
#define DUDE_ROS_ERRORF_THROTTLE(ms, ...) DUDE_ROS_ERRORF(__VA_ARGS__)
