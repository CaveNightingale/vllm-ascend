#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# Copyright 2023 The vLLM team.
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# Adapted from vllm/model_executor/models/qwen2_vl.py
# This file is a part of the vllm-ascend project.

import vllm
import vllm.envs as envs
from torch.distributed import ProcessGroup
from vllm.config import ParallelConfig
from vllm.distributed.utils import \
    stateless_init_torch_distributed_process_group


def ascend_destroy_model_parallel():
    """Set the groups to none and destroy them."""
    from vllm.distributed.parallel_state import _DP, _EP, _PP, _TP
    if _TP:
        _TP.destroy()
    _TP = None
    if _PP:
        _PP.destroy()
    _PP = None
    if _DP:
        _DP.destroy()
    _DP = None
    if _EP:
        _EP.destroy()
    _EP = None
    from vllm_ascend.distributed.parallel_state import \
        destroy_ascend_model_parallel
    destroy_ascend_model_parallel()


def parallel_config_get_dp_port(self) -> int:
    """
    We might need to initialize process groups in multiple
    processes that is related to data parallelism,
    e.g. both in the worker and in the engine, which
    can live in different processes. To avoid port conflicts, we
    increment the port number each time we need to initialize a
    new process group related to data parallelism.
    """
    answer = self.data_parallel_master_port
    self.data_parallel_master_port += 1

    # NOTE: Get port from envs directly when using torchrun
    port = envs.VLLM_DP_MASTER_PORT if envs.VLLM_DP_MASTER_PORT else answer
    return port


def stateless_init_dp_group(self) -> "ProcessGroup":
    # TODO(Yizhou): Currently we have to set the backend to gloo
    # because in vllm.config.ParallelConfig.has_unfinished_dp the
    # device is set to cpu. We need to fix this in the future.
    # We need to compare the performance of gloo and hccl and then
    # decide which one to use.
    dp_group = stateless_init_torch_distributed_process_group(
        self.data_parallel_master_ip,
        self.get_next_dp_init_port(),
        self.data_parallel_rank,
        self.data_parallel_size,
        backend="gloo")

    return dp_group


vllm.distributed.parallel_state.destroy_model_parallel = ascend_destroy_model_parallel
ParallelConfig.get_next_dp_init_port = parallel_config_get_dp_port
ParallelConfig.stateless_init_dp_group = stateless_init_dp_group
