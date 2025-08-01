# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# Copyright 2023 The vLLM team.
# Copyright 2023 DeepSeek-AI and the HuggingFace Inc. team. All rights reserved.
#
# This code is based on EleutherAI's GPT-NeoX library and the GPT-NeoX
# and OPT implementations in this library. It has been modified from its
# original forms to accommodate minor architectural differences compared
# to GPT-NeoX and OPT used by the Meta AI team that trained the model.
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
# # Adapted from
# # vllm-project/vllm/blob/main/vllm/model_executor/models/deepseek_v2.py
# # https://github.com/huggingface/transformers/blob/v4.28.0/src/transformers/models/llama/modeling_llama.py
# # vllm-project/vllm/vllm/model_executor/models/deepseek_v2.py
# """Inference-only DeepseekV2/DeepseekV3 model."""

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

import torch
import torch_npu
import vllm.envs as envs
from torch import nn
from torch.nn.parameter import Parameter
from transformers import PretrainedConfig
from vllm.attention import Attention, AttentionMetadata
from vllm.config import (CacheConfig, ModelConfig, VllmConfig,
                         get_current_vllm_config)
from vllm.distributed import (get_dp_group, get_pp_group,
                              get_tensor_model_parallel_rank,
                              get_tensor_model_parallel_world_size,
                              get_tp_group, split_tensor_along_last_dim,
                              tensor_model_parallel_reduce_scatter)
from vllm.forward_context import get_forward_context
from vllm.model_executor.layers.activation import SiluAndMul
from vllm.model_executor.layers.layernorm import RMSNorm
from vllm.model_executor.layers.linear import (ColumnParallelLinear,
                                               MergedColumnParallelLinear,
                                               ReplicatedLinear,
                                               RowParallelLinear,
                                               UnquantizedLinearMethod)
from vllm.model_executor.layers.logits_processor import LogitsProcessor
from vllm.model_executor.layers.quantization import QuantizationConfig
from vllm.model_executor.layers.rotary_embedding import get_rope
from vllm.model_executor.layers.sampler import get_sampler
from vllm.model_executor.layers.vocab_parallel_embedding import (
    ParallelLMHead, VocabParallelEmbedding)
from vllm.model_executor.models.deepseek_v2 import \
    DeepseekV2ForCausalLM  # noqa: E501
from vllm.model_executor.models.deepseek_v2 import \
    yarn_get_mscale  # noqa: E501
from vllm.model_executor.models.deepseek_v2 import (DeepseekV2Attention,
                                                    DeepseekV2DecoderLayer,
                                                    DeepseekV2MLAAttention)
from vllm.model_executor.models.utils import (
    PPMissingLayer, make_empty_intermediate_tensors_factory, make_layers,
    maybe_prefix)
from vllm.sequence import IntermediateTensors

from vllm_ascend.ascend_config import get_ascend_config
from vllm_ascend.ops.fused_moe import AscendFusedMoE
from vllm_ascend.quantization.quant_config import AscendLinearMethod
from vllm_ascend.quantization.w8a8_dynamic import AscendW8A8DynamicLinearMethod
from vllm_ascend.utils import dispose_tensor, npu_prefetch


class RowParallelScatterLinear(RowParallelLinear):

    def forward(
        self, input_
    ) -> Union[torch.Tensor, tuple[torch.Tensor, Optional[Parameter]]]:
        if self.input_is_parallel:
            input_parallel = input_
        else:
            tp_rank = get_tensor_model_parallel_rank()
            splitted_input = split_tensor_along_last_dim(
                input_, num_partitions=self.tp_size)
            input_parallel = splitted_input[tp_rank].contiguous()

        # Matrix multiply.
        assert self.quant_method is not None
        # Only fuse bias add into GEMM for rank 0 (this ensures that
        # bias will not get added more than once in TP>1 case)
        bias_ = None if (self.tp_rank > 0 or self.skip_bias_add) else self.bias
        output_parallel = self.quant_method.apply(self,
                                                  input_parallel,
                                                  bias=bias_)
        if self.reduce_results and self.tp_size > 1:
            num_tokens, _ = output_parallel.shape
            if num_tokens % self.tp_size:
                output_parallel = nn.functional.pad(
                    output_parallel, (0, 0, 0, -num_tokens % self.tp_size))
            output = tensor_model_parallel_reduce_scatter(output_parallel,
                                                          dim=0)
        else:
            output = output_parallel

        output_bias = self.bias if self.skip_bias_add else None
        if not self.return_bias:
            return output
        return output, output_bias


class CustomDeepseekV2SiluAndMul(SiluAndMul):

    def __init__(self,
                 *,
                 weight_scale: Optional[Callable[[], torch.Tensor]] = None):
        super().__init__()
        self.weight_scale = weight_scale

    def forward_oot(self, x: Union[torch.Tensor, Tuple[torch.Tensor,
                                                       torch.Tensor]]):
        if isinstance(x, tuple):
            assert self.weight_scale is not None
            # For AscendW8A8DynamicLinearMethod:
            # a dynamic scale is passed along with the quantized value.
            quantized_x, dynamic_scale = x
            return torch_npu.npu_dequant_swiglu_quant(
                x=quantized_x,
                weight_scale=self.weight_scale(),
                activation_scale=dynamic_scale,
                activate_left=True,
                quant_mode=1)
        else:
            return super().forward_oot(x)


class CustomDeepseekV2MergedReplicatedLinear(ReplicatedLinear):

    def __init__(
        self,
        input_size: int,
        output_sizes: list[int],
        bias: bool = True,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ):
        self.output_sizes = output_sizes
        super().__init__(input_size,
                         sum(output_sizes),
                         bias=bias,
                         quant_config=quant_config,
                         prefix=prefix)

    def weight_loader(self, param: torch.nn.Parameter,
                      loaded_weight: torch.Tensor, loaded_shard_id: int):
        # With no support for GGUF format yet.
        assert not getattr(param, "is_gguf_weight", False)
        assert not getattr(param, "is_gguf_weight_type", False)

        assert loaded_shard_id < len(self.output_sizes)
        shard_offset = sum(self.output_sizes[:loaded_shard_id])
        shard_size = self.output_sizes[loaded_shard_id]
        shard = param.data.narrow(param.output_dim, shard_offset, shard_size)

        assert shard.size() == loaded_weight.size(), (
            f"Tried to load weights of size {loaded_weight.size()}"
            f"to a parameter shard of id {loaded_shard_id} size {shard.size()}"
        )
        shard.copy_(loaded_weight)


class CustomDeepseekV2MLP(nn.Module):

    def __init__(
        self,
        hidden_size: int,
        intermediate_size: int,
        hidden_act: str,
        quant_config: Optional[QuantizationConfig] = None,
        reduce_results: bool = True,
        force_replicate: bool = False,
        prefix: str = "",
    ) -> None:
        super().__init__()
        if not force_replicate:
            self.gate_up_proj = MergedColumnParallelLinear(
                hidden_size, [intermediate_size] * 2,
                bias=False,
                quant_config=quant_config,
                prefix=f"{prefix}.gate_up_proj")
            self.down_proj = RowParallelLinear(intermediate_size,
                                               hidden_size,
                                               bias=False,
                                               quant_config=quant_config,
                                               reduce_results=reduce_results,
                                               prefix=f"{prefix}.down_proj")
        else:
            self.gate_up_proj = CustomDeepseekV2MergedReplicatedLinear(
                hidden_size, [intermediate_size] * 2,
                bias=False,
                quant_config=quant_config,
                prefix=f"{prefix}.gate_up_proj")
            self.down_proj = ReplicatedLinear(intermediate_size,
                                              hidden_size,
                                              bias=False,
                                              quant_config=quant_config,
                                              prefix=f"{prefix}.down_proj")
        if hidden_act != "silu":
            raise ValueError(f"Unsupported activation: {hidden_act}. "
                             "Only silu is supported for now.")

        quant_method = self.gate_up_proj.quant_method
        if isinstance(quant_method, UnquantizedLinearMethod):
            self.act_fn = CustomDeepseekV2SiluAndMul()
        elif (isinstance(quant_method, AscendLinearMethod) and isinstance(
                quant_method.quant_method, AscendW8A8DynamicLinearMethod)):
            # TODO(sdmyzlp): Currently preserved as before:
            # 1. The only quantization supported for silu is W8A8Dynamic
            # 2. Output dtype of gate_up/down is fixed to be int32/bfloat16
            #
            # Maybe one can implement a better and more general configuration
            # scheme, e.g. by somehow passing around the tweaked `quant_config`
            self.act_fn = CustomDeepseekV2SiluAndMul(
                # Use lazy binding, for `weight_scale_fp32` is accessible
                # only after `process_weights_after_loading`.
                weight_scale=lambda: self.gate_up_proj.weight_scale_fp32)
            # To be consumed by AscendW8A8DynamicLinearMethod.apply()
            self.gate_up_proj._ascend_quant_config = {
                "output_dtype": torch.int32,
                "pertoken_scale": False,
                "return_scale": True,
            }
            self.down_proj._ascend_quant_config = {
                "output_dtype": torch.bfloat16,
                "pertoken_scale": True,
                "return_scale": False,
            }
        else:
            raise NotImplementedError(
                f"Quantization with [{type(quant_method)}] is NOT supported")

    def forward(self, x):
        gate_up, _ = self.gate_up_proj(x)
        x = self.act_fn(gate_up)
        x, _ = self.down_proj(x)
        return x


class CustomDeepseekV2MoE(nn.Module):

    top_k: int

    def __init__(
        self,
        config: PretrainedConfig,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ):
        super().__init__()
        self.tp_size = get_tensor_model_parallel_world_size()
        self.routed_scaling_factor = config.routed_scaling_factor
        self.n_shared_experts = config.n_shared_experts
        self.routed_scaling_factor = config.routed_scaling_factor
        if self.tp_size > config.n_routed_experts:
            raise ValueError(
                f"Tensor parallel size {self.tp_size} is greater than "
                f"the number of experts {config.n_routed_experts}.")

        if config.hidden_act != "silu":
            raise ValueError(f"Unsupported activation: {config.hidden_act}. "
                             "Only silu is supported for now.")

        ascend_config = get_ascend_config()
        self.torchair_graph_enabled = ascend_config.torchair_graph_config.enabled
        self.enable_multistream_moe = \
            ascend_config.torchair_graph_config.enable_multistream_moe and \
            self.torchair_graph_enabled
        self.enable_super_kernel = \
            ascend_config.torchair_graph_config.enable_super_kernel and \
            self.enable_multistream_moe
        self.params_dtype = torch.get_default_dtype()

        self.gate = ReplicatedLinear(
            config.hidden_size,
            config.n_routed_experts,
            bias=False,
            quant_config=None,
            params_dtype=torch.float32
            if self.enable_super_kernel else self.params_dtype,
            prefix=f"{prefix}.gate")
        if config.topk_method == "noaux_tc":
            self.gate.e_score_correction_bias = nn.Parameter(
                torch.empty(config.n_routed_experts,
                            dtype=torch.float if self.enable_super_kernel else
                            self.params_dtype))
        else:
            self.gate.e_score_correction_bias = None

        self.experts = AscendFusedMoE(
            num_experts=config.n_routed_experts,
            top_k=config.num_experts_per_tok,
            hidden_size=config.hidden_size,
            intermediate_size=config.moe_intermediate_size,
            reduce_results=False,
            renormalize=config.norm_topk_prob,
            quant_config=quant_config,
            use_grouped_topk=True,
            num_expert_group=config.n_group,
            topk_group=config.topk_group,
            prefix=f"{prefix}.experts",
            scoring_func=config.scoring_func,
            e_score_correction_bias=self.gate.e_score_correction_bias)

        if config.n_shared_experts is not None:
            intermediate_size = (config.moe_intermediate_size *
                                 config.n_shared_experts)
            self.shared_experts = CustomDeepseekV2MLP(
                hidden_size=config.hidden_size,
                intermediate_size=intermediate_size,
                hidden_act=config.hidden_act,
                quant_config=quant_config,
                reduce_results=True,
                force_replicate=self.enable_multistream_moe
                or ascend_config.enable_prefill_optimizations,
                prefix=f"{prefix}.shared_experts",
            )
        else:
            self.shared_experts = None  # type: ignore
        CustomDeepseekV2MoE.top_k = config.num_experts_per_tok

        self.dp_size = get_dp_group().world_size

        self.tp_group = get_tp_group().device_group
        self.tp_rank = get_tp_group().rank_in_group
        self.kv_consumer = None
        transfer_config = get_current_vllm_config().kv_transfer_config
        if transfer_config is not None:
            self.kv_consumer = transfer_config.kv_role == "kv_consumer"

    def forward(
            self,
            hidden_states: torch.Tensor,
            attn_metadata: Optional[AttentionMetadata] = None) -> torch.Tensor:
        forward_context = get_forward_context()
        if attn_metadata is None:
            attn_metadata = forward_context.attn_metadata

        # when profile runs, force experts to load balanced tokens
        # to avoid high memory consumption on a single rank.
        enable_force_load_balance = forward_context.in_profile_run

        is_prefill = forward_context.with_prefill
        # If this node is kv_consumer, we force the moe always runs in decode path to make sure
        # the behaviour aligned between dummy_run and normal model_execute.
        if self.kv_consumer:
            is_prefill = False

        # router_logits: (num_tokens, n_experts)
        if self.enable_multistream_moe:
            router_logits = None
        else:
            router_logits, _ = self.gate(hidden_states)

        experts_hidden_states = self.experts(
            hidden_states=hidden_states,
            router_logits=router_logits,
            is_prefill=is_prefill,
            top_k=CustomDeepseekV2MoE.top_k,
            enable_force_load_balance=enable_force_load_balance,
            shared_experts=self.shared_experts,
            gate=self.gate if self.enable_multistream_moe else None,
        )

        hidden_states = (
            experts_hidden_states[0] * self.routed_scaling_factor +
            experts_hidden_states[1])

        return hidden_states


class CustomDeepseekV2MLAAttention(DeepseekV2MLAAttention):

    def __init__(
        self,
        config: PretrainedConfig,
        hidden_size: int,
        num_heads: int,
        qk_nope_head_dim: int,
        qk_rope_head_dim: int,
        v_head_dim: int,
        q_lora_rank: Optional[int],
        kv_lora_rank: int,
        rope_theta: float = 10000,
        rope_scaling: Optional[Dict[str, Any]] = None,
        max_position_embeddings: int = 8192,
        cache_config: Optional[CacheConfig] = None,
        quant_config: Optional[QuantizationConfig] = None,
        prefix: str = "",
    ) -> None:
        nn.Module.__init__(self)
        self.hidden_size = hidden_size
        self.qk_nope_head_dim = qk_nope_head_dim
        self.qk_rope_head_dim = qk_rope_head_dim
        self.qk_head_dim = qk_nope_head_dim + qk_rope_head_dim
        self.v_head_dim = v_head_dim

        self.q_lora_rank = q_lora_rank
        self.kv_lora_rank = kv_lora_rank

        self.num_heads = num_heads
        self.tp_size = get_tensor_model_parallel_world_size()
        assert num_heads % self.tp_size == 0
        self.num_local_heads = num_heads // self.tp_size

        self.scaling = self.qk_head_dim**-0.5
        self.rope_theta = rope_theta
        self.max_position_embeddings = max_position_embeddings

        if self.q_lora_rank is not None:
            self.q_a_proj = ReplicatedLinear(self.hidden_size,
                                             self.q_lora_rank,
                                             bias=False,
                                             quant_config=quant_config,
                                             prefix=f"{prefix}.q_a_proj")
            self.q_a_layernorm = RMSNorm(self.q_lora_rank,
                                         eps=config.rms_norm_eps)
            self.q_b_proj = ColumnParallelLinear(q_lora_rank,
                                                 self.num_heads *
                                                 self.qk_head_dim,
                                                 bias=False,
                                                 quant_config=quant_config,
                                                 prefix=f"{prefix}.q_b_proj")
        else:
            self.q_proj = ColumnParallelLinear(self.hidden_size,
                                               self.num_heads *
                                               self.qk_head_dim,
                                               bias=False,
                                               quant_config=quant_config,
                                               prefix=f"{prefix}.q_proj")

        self.kv_a_proj_with_mqa = ReplicatedLinear(
            self.hidden_size,
            self.kv_lora_rank + self.qk_rope_head_dim,
            bias=False,
            quant_config=quant_config,
            prefix=f"{prefix}.kv_a_proj_with_mqa")
        self.kv_a_layernorm = RMSNorm(self.kv_lora_rank,
                                      eps=config.rms_norm_eps)
        self.kv_b_proj = ColumnParallelLinear(
            self.kv_lora_rank,
            self.num_heads * (self.qk_nope_head_dim + self.v_head_dim),
            bias=False,
            quant_config=quant_config,
            prefix=f"{prefix}.kv_b_proj")

        ascend_config = get_ascend_config()
        self.enable_prefill_optimizations = ascend_config.enable_prefill_optimizations

        if not self.enable_prefill_optimizations or int(
                prefix.split(".")[-2]) < 3:
            self.o_proj = RowParallelLinear(self.num_heads * self.v_head_dim,
                                            self.hidden_size,
                                            bias=False,
                                            quant_config=quant_config,
                                            prefix=f"{prefix}.o_proj")
        else:
            self.o_proj = RowParallelScatterLinear(self.num_heads *
                                                   self.v_head_dim,
                                                   self.hidden_size,
                                                   bias=False,
                                                   quant_config=quant_config,
                                                   prefix=f"{prefix}.o_proj")

        if rope_scaling:
            rope_scaling["rope_type"] = 'deepseek_yarn'
        self.rotary_emb = get_rope(qk_rope_head_dim,
                                   rotary_dim=qk_rope_head_dim,
                                   max_position=max_position_embeddings,
                                   base=rope_theta,
                                   rope_scaling=rope_scaling,
                                   is_neox_style=False)
        if rope_scaling:
            mscale_all_dim = rope_scaling.get("mscale_all_dim", False)
            scaling_factor = rope_scaling["factor"]
            mscale = yarn_get_mscale(scaling_factor, float(mscale_all_dim))
            self.scaling = self.scaling * mscale * mscale

        # In the MLA backend, kv_cache includes both k_c and
        # pe (i.e. decoupled position embeddings). In particular,
        # the concat_and_cache_mla op requires
        #     k_c.size(1) + k_pe.size(1) == kv_cache.size(2)
        # i.e.
        #     kv_lora_rank + qk_rope_head_dim == head_size
        self.mla_attn = Attention(
            num_heads=self.num_local_heads,
            head_size=self.kv_lora_rank + self.qk_rope_head_dim,
            scale=self.scaling,
            num_kv_heads=1,
            cache_config=cache_config,
            quant_config=quant_config,
            prefix=f"{prefix}.attn",
            use_mla=True,
            # MLA Args
            q_lora_rank=self.q_lora_rank,
            kv_lora_rank=self.kv_lora_rank,
            qk_nope_head_dim=self.qk_nope_head_dim,
            qk_rope_head_dim=self.qk_rope_head_dim,
            qk_head_dim=self.qk_head_dim,
            v_head_dim=self.v_head_dim,
            rotary_emb=self.rotary_emb,
            q_proj=self.q_proj if self.q_lora_rank is None else self.q_b_proj,
            kv_a_proj_with_mqa=self.kv_a_proj_with_mqa,
            kv_a_layernorm=self.kv_a_layernorm,
            kv_b_proj=self.kv_b_proj,
            o_proj=self.o_proj,
        )

        self.prefix = prefix
        self.debug_layer_idx = int(self.prefix.split(".")[-2])

        ascend_config = get_ascend_config()
        self.torchair_graph_enabled = ascend_config.torchair_graph_config.enabled
        self.enable_multistream_mla = \
            ascend_config.torchair_graph_config.enable_multistream_mla and \
            self.torchair_graph_enabled

    def forward(
            self,
            positions: torch.Tensor,
            hidden_states: torch.Tensor,
            kv_cache: Optional[torch.Tensor] = None,
            attn_metadata: Optional[AttentionMetadata] = None) -> torch.Tensor:
        forward_context = get_forward_context()
        enable_multistream_mla = (self.enable_multistream_mla
                                  and attn_metadata is not None
                                  and not forward_context.with_prefill
                                  and attn_metadata.num_decodes > 0)
        forward_kwargs = {"enable_multistream_mla": enable_multistream_mla}
        if self.q_lora_rank is not None:
            npu_prefetch(self.q_a_proj.weight,
                         hidden_states,
                         enabled=enable_multistream_mla)
            ckq = self.q_a_proj(hidden_states)[0]
            hidden_states_or_q_c = self.q_a_layernorm(ckq)
        else:
            hidden_states_or_q_c = hidden_states
        if self.torchair_graph_enabled:
            if envs.VLLM_USE_V1:
                output_shape = hidden_states.shape
                output = torch.empty(output_shape,
                                     dtype=hidden_states_or_q_c.dtype,
                                     device=hidden_states_or_q_c.device)
                forward_kwargs['output'] = output

            output = self.mla_attn.impl.forward(self.mla_attn,
                                                hidden_states_or_q_c,
                                                hidden_states, None, kv_cache,
                                                attn_metadata,
                                                **forward_kwargs)
            if envs.VLLM_USE_V1:
                output = output.view(-1, output_shape[-1])
            return output
        else:
            kv_no_split = self.kv_a_proj_with_mqa(hidden_states)[0]
            if self.enable_prefill_optimizations and self.debug_layer_idx > 3 and self.debug_layer_idx < 61:
                hidden_states_or_q_c = get_tp_group().all_gather(
                    hidden_states_or_q_c, 0)
                kv_no_split = get_tp_group().all_gather(kv_no_split, 0)

                attn_metadata = forward_context.attn_metadata
                if attn_metadata is not None:
                    num_tokens = attn_metadata.num_actual_tokens
                else:
                    num_tokens = hidden_states_or_q_c.shape[0]

            kv_c, k_pe = kv_no_split.split(
                [self.kv_lora_rank, self.qk_rope_head_dim], dim=-1)
            kv_c_normed = self.kv_a_layernorm(kv_c.contiguous())
            if not self.enable_prefill_optimizations or self.debug_layer_idx < 3:
                output_shape = hidden_states.shape
            else:
                num_tokens = hidden_states_or_q_c.shape[0]
                rows = num_tokens // self.tp_size
                if num_tokens % self.tp_size:
                    rows += 1
                output_shape = (rows, hidden_states.shape[1])
            return self.mla_attn(hidden_states_or_q_c,
                                 kv_c_normed,
                                 k_pe,
                                 output_shape=output_shape)


class CustomDeepseekV2DecoderLayer(DeepseekV2DecoderLayer):

    def __init__(
        self,
        config: PretrainedConfig,
        prefix: str,
        model_config: ModelConfig,
        cache_config: Optional[CacheConfig] = None,
        quant_config: Optional[QuantizationConfig] = None,
    ) -> None:
        nn.Module.__init__(self)
        self.hidden_size = config.hidden_size
        rope_theta = getattr(config, "rope_theta", 10000)
        rope_scaling = getattr(config, "rope_scaling", None)
        max_position_embeddings = getattr(config, "max_position_embeddings",
                                          8192)
        # DecoderLayers are created with `make_layers` which passes the prefix
        # with the layer's index.
        layer_idx = int(prefix.split(sep='.')[-1])
        self.layer_idx = layer_idx
        # TODO: enable mla in vllm-ascend
        if model_config.use_mla:
            attn_cls = CustomDeepseekV2MLAAttention
        else:
            attn_cls = DeepseekV2Attention
        self.self_attn = attn_cls(
            config=config,
            hidden_size=self.hidden_size,
            num_heads=config.num_attention_heads,
            qk_nope_head_dim=config.qk_nope_head_dim,
            qk_rope_head_dim=config.qk_rope_head_dim,
            v_head_dim=config.v_head_dim,
            q_lora_rank=config.q_lora_rank
            if hasattr(config, "q_lora_rank") else None,
            kv_lora_rank=config.kv_lora_rank,
            rope_theta=rope_theta,
            rope_scaling=rope_scaling,
            max_position_embeddings=max_position_embeddings,
            cache_config=cache_config,
            quant_config=quant_config,
            prefix=f"{prefix}.self_attn",
        )

        if (config.n_routed_experts is not None
                and layer_idx >= config.first_k_dense_replace
                and layer_idx % config.moe_layer_freq == 0):
            self.mlp = CustomDeepseekV2MoE(
                config=config,
                quant_config=quant_config,
                prefix=f"{prefix}.mlp",
            )
        else:
            self.mlp = CustomDeepseekV2MLP(
                hidden_size=config.hidden_size,
                intermediate_size=config.intermediate_size,
                hidden_act=config.hidden_act,
                quant_config=quant_config,
                prefix=f"{prefix}.mlp",
            )
        self.input_layernorm = RMSNorm(config.hidden_size,
                                       eps=config.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(config.hidden_size,
                                                eps=config.rms_norm_eps)
        self.routed_scaling_factor = config.routed_scaling_factor
        self.tp_group = get_tp_group().device_group
        ascend_config = get_ascend_config()
        self.enable_prefill_optimizations = ascend_config.enable_prefill_optimizations

    def forward(
        self,
        positions: torch.Tensor,
        hidden_states: torch.Tensor,
        residual: Optional[torch.Tensor],
        kv_cache: Optional[torch.Tensor] = None,
        attn_metadata: Optional[AttentionMetadata] = None,
    ) -> torch.Tensor:
        # Self Attention
        if residual is None:
            residual = hidden_states
            hidden_states = self.input_layernorm(hidden_states)
        else:
            previous_hidden_states, previous_residual = hidden_states, residual
            hidden_states, residual = self.input_layernorm(
                hidden_states, residual)
            # Dispose hidden_states and residual from the previous layer
            # to save npu memory because they're no longer used.
            dispose_tensor(previous_hidden_states)
            dispose_tensor(previous_residual)

        hidden_states = self.self_attn(
            positions=positions,
            hidden_states=hidden_states,
            kv_cache=kv_cache,
            attn_metadata=attn_metadata,
        )

        if hidden_states.dtype == torch.float16:
            # Fix FP16 overflow
            # We scale both hidden_states and residual before
            # rmsnorm, and rmsnorm result would not affect by scale.
            hidden_states *= 1. / self.routed_scaling_factor
            if self.layer_idx == 0:
                # The residual is shared by all layers, we only scale it on
                # first layer.
                residual *= 1. / self.routed_scaling_factor

        tp_size = get_tensor_model_parallel_world_size()
        if self.enable_prefill_optimizations and (
                self.layer_idx == 3 or self.layer_idx == 61) and tp_size > 1:
            num_tokens, _ = residual.shape
            if num_tokens % tp_size:
                residual = nn.functional.pad(residual,
                                             (0, 0, 0, -num_tokens % tp_size))
            chunk_residual = torch.tensor_split(residual, tp_size, dim=0)
            tp_rank = get_tensor_model_parallel_rank()
            residual = chunk_residual[tp_rank]

        # Fully Connected
        hidden_states, residual = self.post_attention_layernorm(
            hidden_states, residual)

        if isinstance(self.mlp, CustomDeepseekV2MoE):
            hidden_states = self.mlp(hidden_states, attn_metadata)
        else:
            hidden_states = self.mlp(hidden_states)

        if isinstance(
                self.mlp,
                CustomDeepseekV2MLP) and hidden_states.dtype == torch.float16:
            # Fix FP16 overflow
            # Scaling the DeepseekV2MLP output, it is the input of
            # input_layernorm of next decoder layer.
            # The scaling of DeepseekV2MOE output would be done in the forward
            # of DeepseekV2MOE
            hidden_states *= 1. / self.routed_scaling_factor

        # for last layer of main model and mtp layer.
        if self.enable_prefill_optimizations and self.layer_idx >= 60 and tp_size > 1:
            hidden_states = get_tp_group().all_gather(hidden_states, 0)
            residual = get_tp_group().all_gather(residual, 0)

            attn_metadata = get_forward_context().attn_metadata
            if attn_metadata is not None:
                num_tokens = attn_metadata.num_actual_tokens
            else:
                num_tokens = hidden_states.shape[0]

            if num_tokens < hidden_states.shape[0]:
                hidden_states = hidden_states[:num_tokens]
                residual = residual[:num_tokens]

        return hidden_states, residual


class CustomDeepseekV2Model(nn.Module):

    fall_back_to_pt_during_load = False

    def __init__(self, *, vllm_config: VllmConfig, prefix: str = ""):
        super().__init__()

        config = vllm_config.model_config.hf_config
        model_config = vllm_config.model_config
        cache_config = vllm_config.cache_config
        quant_config = vllm_config.quant_config

        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size

        if get_pp_group().is_first_rank:
            self.embed_tokens = VocabParallelEmbedding(
                config.vocab_size,
                config.hidden_size,
                quant_config=quant_config,
                prefix=f"{prefix}.embed_tokens")
        else:
            self.embed_tokens = PPMissingLayer()

        self.start_layer, self.end_layer, self.layers = make_layers(
            config.num_hidden_layers,
            lambda prefix: CustomDeepseekV2DecoderLayer(
                config,
                prefix,
                model_config=model_config,
                cache_config=cache_config,
                quant_config=quant_config,
            ),
            prefix=f"{prefix}.layers")

        if get_pp_group().is_last_rank:
            self.norm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        else:
            self.norm = PPMissingLayer()
        self.make_empty_intermediate_tensors = (
            make_empty_intermediate_tensors_factory(
                ["hidden_states", "residual"], config.hidden_size))

    def get_input_embeddings(self, input_ids: torch.Tensor) -> torch.Tensor:
        return self.embed_tokens(input_ids)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: Optional[List[torch.Tensor]] = None,
        attn_metadata: Optional[AttentionMetadata] = None,
        intermediate_tensors: Optional[IntermediateTensors] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
    ) -> Union[torch.Tensor, IntermediateTensors]:
        if get_pp_group().is_first_rank:
            if inputs_embeds is not None:
                hidden_states = inputs_embeds
            else:
                hidden_states = self.get_input_embeddings(input_ids)
            residual = None
        else:
            assert intermediate_tensors is not None
            hidden_states = intermediate_tensors["hidden_states"]
            residual = intermediate_tensors["residual"]

        for i in range(self.start_layer, self.end_layer):
            layer = self.layers[i]
            hidden_states, residual = layer(
                positions, hidden_states, residual,
                kv_caches[i -
                          self.start_layer] if kv_caches is not None else None,
                attn_metadata)

        if not get_pp_group().is_last_rank:
            return IntermediateTensors({
                "hidden_states": hidden_states,
                "residual": residual
            })

        hidden_states, _ = self.norm(hidden_states, residual)
        return hidden_states


class CustomDeepseekV2ForCausalLM(DeepseekV2ForCausalLM):
    # add `packed_modules_mapping` in `DeepseekV2ForCausalLM` to support weight merging
    packed_modules_mapping = {
        "gate_up_proj": ["gate_proj", "up_proj"],
        "experts":
        ["experts.0.gate_proj", "experts.0.up_proj", "experts.0.down_proj"]
    }

    def __init__(self, *, vllm_config: VllmConfig, prefix: str = ""):
        nn.Module.__init__(self)
        config = vllm_config.model_config.hf_config
        quant_config = vllm_config.quant_config
        self.config = config
        self.quant_config = quant_config
        self.num_dense_layers = self.config.first_k_dense_replace
        self.num_moe_layers = self.config.num_hidden_layers - self.num_dense_layers
        self.model = CustomDeepseekV2Model(vllm_config=vllm_config,
                                           prefix=maybe_prefix(
                                               prefix, "model"))
        if get_pp_group().is_last_rank:
            self.lm_head = ParallelLMHead(config.vocab_size,
                                          config.hidden_size,
                                          quant_config=quant_config,
                                          prefix=maybe_prefix(
                                              prefix, "lm_head"))
        else:
            self.lm_head = PPMissingLayer()
        self.logits_processor = LogitsProcessor(config.vocab_size)
        self.sampler = get_sampler()
        self.make_empty_intermediate_tensors = (
            self.model.make_empty_intermediate_tensors)

    def forward(
        self,
        input_ids: torch.Tensor,
        positions: torch.Tensor,
        kv_caches: Optional[List[torch.Tensor]] = None,
        attn_metadata: Optional[AttentionMetadata] = None,
        intermediate_tensors: Optional[IntermediateTensors] = None,
        inputs_embeds: Optional[torch.Tensor] = None,
    ) -> Union[torch.Tensor, IntermediateTensors]:
        hidden_states = self.model(input_ids, positions, kv_caches,
                                   attn_metadata, intermediate_tensors,
                                   inputs_embeds)
        return hidden_states

    def load_weights(self, weights: Iterable[tuple[str,
                                                   torch.Tensor]]) -> set[str]:
        weights = filter(lambda x: ".module." not in x[0], weights)
        # weights = ((name, data) for name, data in weights if ".module." not in name)
        loaded_params = super().load_weights(weights)

        return loaded_params

    def get_expert_map(self, layer_id):
        return self.model.layers[layer_id].mlp.experts.get_map()

    def get_log2phy_map(self, layer_id):
        return self.model.layers[layer_id].mlp.experts.get_log2phy_map()

    def get_all_expert_map(self, num_moe_layers):
        all_loads = []
        for layer_id in range(num_moe_layers):
            load_tensor = self.get_expert_map(
                layer_id + self.num_dense_layers)  # (num_experts_per_layer,)
            all_loads.append(load_tensor)

        return torch.stack(all_loads, dim=0)

    def get_all_moe_loads(self):
        all_moe_loads = torch.stack(
            [self.model.layers[layer_id + self.num_dense_layers].mlp.experts.moe_load \
                for layer_id in range(self.num_moe_layers)],
            dim=0
        )
        return all_moe_loads

    def clear_all_moe_loads(self):
        for layer_id in range(self.num_moe_layers):
            self.model.layers[
                layer_id + self.num_dense_layers].mlp.experts.clear_moe_load()


class CustomDeepseekV3ForCausalLM(CustomDeepseekV2ForCausalLM):
    pass
