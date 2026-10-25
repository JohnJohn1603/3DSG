"""Small, explicit CPU runtime settings for the isolated P2 ONNX worker."""

from __future__ import annotations


def cpu_session_options(ort, *, intra_op_threads: int, inter_op_threads: int):
    """Build a sequential ONNX Runtime session with bounded CPU threads."""
    if type(intra_op_threads) is not int or not 1 <= intra_op_threads <= 8:
        raise ValueError("P2 intra-op thread count must be an integer in [1, 8]")
    if type(inter_op_threads) is not int or not 1 <= inter_op_threads <= 4:
        raise ValueError("P2 inter-op thread count must be an integer in [1, 4]")
    options = ort.SessionOptions()
    options.intra_op_num_threads = intra_op_threads
    options.inter_op_num_threads = inter_op_threads
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return options
