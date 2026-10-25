from exploration.resource_monitor import _parse_host_cpu_ticks


def test_parse_host_cpu_ticks_uses_aggregate_and_includes_iowait_as_idle() -> None:
    contents = "cpu 100 20 30 400 50 10 5 2 999 888\ncpu0 1 2 3 4 5 6 7 8\n"

    assert _parse_host_cpu_ticks(contents) == (617, 450)


def test_parse_host_cpu_ticks_rejects_missing_or_malformed_aggregate_line() -> None:
    assert _parse_host_cpu_ticks("") is None
    assert _parse_host_cpu_ticks("cpu0 1 2 3 4 5 6 7 8\n") is None
    assert _parse_host_cpu_ticks("cpu 1 2 broken 4 5\n") is None
