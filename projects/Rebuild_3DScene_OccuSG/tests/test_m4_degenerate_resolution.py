from __future__ import annotations

import numpy as np

from standalone_sg.mapping.projection import FreeSpaceGrid2D
from standalone_sg.regions.dude_client import DuDeWorkerClient
from test_m4_dude import BIN, grid, shape_fixture


def test_degenerate_single_pixel_and_second_resolution_after_reset():
    with DuDeWorkerClient(BIN) as worker:
        values = np.zeros((1, 1), dtype=np.int8); z = np.zeros((1, 1))
        tiny = FreeSpaceGrid2D(values, .05, (0., 0.), z, z + 1, 1)
        response, observations = worker.integrate(tiny, timestamp_s=1.)
        assert response["branch"] == "accept_empty_free_space"
        assert observations == ()
        assert worker.reset()["status"] == "reset"
        second = grid(shape_fixture("rectangle"), 2, resolution=.1)
        response, observations = worker.integrate(second, timestamp_s=2.)
    assert response["input_revision"] == 2
    assert response["diagnostics"]["pixel_tau"] == 15.0
    assert len(observations) == 1
