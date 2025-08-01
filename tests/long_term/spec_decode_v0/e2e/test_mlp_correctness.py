#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# This file is a part of the vllm-ascend project.
# Adapted from vllm-project/vllm/tests/spec_decode/e2e/test_mlp_correctness.py
# Copyright 2023 The vLLM team.
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
#
"""This docstring details important information on the testing methodology.

Most of the tests rely on "greedy equality", where we expect the output of
speculative decoding on a sequence to exactly match the output of normal non-
speculative decoding.

Since speculative decoding with rejection sampling guarantees that the output
distribution matches the target model's output distribution (up to hardware
numerics, see https://arxiv.org/pdf/2302.01318.pdf), we can expect greedy
equality.

However, we still need to verify below scenario could be passed:
    * Batch size 1 greedy equality
    * Batch size >1 greedy equality
    * Test greedy equality under preemption
    * Test greedy equality under various number of speculative tokens.

With those tests, we can say at least, MLPSpeculator would not break the
correctness for the target model outputs.
"""

import pytest
from vllm.model_executor.layers.vocab_parallel_embedding import \
    pad_vocab_size  # noqa: F401

from tests.long_term.spec_decode_v0.e2e.conftest import \
    run_equality_correctness_test
from tests.long_term.spec_decode_v0.utils import maybe_enable_chunked_prefill

# main model
MAIN_MODEL = "JackFram/llama-160m"

# speculative model
SPEC_MODEL = "ibm-ai-platform/llama-160m-accelerator"

# max. number of speculative tokens: this corresponds to
# n_predict in the config.json of the speculator model.
MAX_SPEC_TOKENS = 3

PREFILL_CHUNK_SIZE_1 = [
    -1,
    # TODO:enable chunked prefill when it is supported
    #   4
]
PREFILL_CHUNK_SIZE_2 = [
    -1,
    # TODO:enable chunked prefill when it is supported
    #   32
]
# precision
# TODO: The vLLM here uses float32, but some op on the vllm-ascend
# do not support float32, such as ROPE, When it is fixed, it is
# recommended to change this to float32 to keep it consistent
# with vLLM.
PRECISION = "float16"


@pytest.mark.parametrize(
    "common_llm_kwargs",
    [{
        # Skip cuda graph recording for fast test.
        "enforce_eager": True,

        # Print spec metrics.
        "disable_log_stats": False,

        # Precision
        "dtype": PRECISION,

        # Main model
        "model_name": MAIN_MODEL,
    }])
@pytest.mark.parametrize("per_test_common_llm_kwargs", [{}])
@pytest.mark.parametrize("baseline_llm_kwargs", [{}])
@pytest.mark.parametrize("test_llm_kwargs", [
    {
        "speculative_config": {
            "model": SPEC_MODEL,
        },
    },
])
@pytest.mark.parametrize("output_len", [
    128,
])
@pytest.mark.parametrize("batch_size", [4, 32])
@pytest.mark.parametrize("seed", [1])
@pytest.mark.parametrize("prefill_chunk_size", PREFILL_CHUNK_SIZE_2)
def test_mlp_e2e_greedy_correctness(vllm_runner, common_llm_kwargs,
                                    per_test_common_llm_kwargs,
                                    baseline_llm_kwargs, test_llm_kwargs,
                                    batch_size: int, output_len: int,
                                    seed: int, prefill_chunk_size: int):
    """Verify greedy equality with different batch size."""
    maybe_enable_chunked_prefill(prefill_chunk_size, test_llm_kwargs)
    run_equality_correctness_test(vllm_runner,
                                  common_llm_kwargs,
                                  per_test_common_llm_kwargs,
                                  baseline_llm_kwargs,
                                  test_llm_kwargs,
                                  batch_size,
                                  max_output_len=output_len,
                                  seed=seed,
                                  temperature=0.0)


@pytest.mark.parametrize(
    "common_llm_kwargs",
    [{
        # Skip cuda graph recording for fast test.
        "enforce_eager": True,

        # Print spec metrics.
        "disable_log_stats": False,

        # Precision
        "dtype": PRECISION,

        # Main model
        "model_name": MAIN_MODEL,
    }])
@pytest.mark.parametrize("per_test_common_llm_kwargs", [{}])
@pytest.mark.parametrize("baseline_llm_kwargs", [{}])
@pytest.mark.parametrize("test_llm_kwargs", [
    {
        "speculative_config": {
            "model": SPEC_MODEL,
            "disable_logprobs": False,
        },
    },
    {
        "speculative_config": {
            "model": SPEC_MODEL,
            "disable_logprobs": True,
        },
    },
])
@pytest.mark.parametrize("output_len", [8])
@pytest.mark.parametrize("batch_size", [8])
@pytest.mark.parametrize("seed", [1])
@pytest.mark.parametrize("logprobs", [1, 6])
@pytest.mark.parametrize("prefill_chunk_size", PREFILL_CHUNK_SIZE_1)
def test_mlp_e2e_greedy_logprobs(vllm_runner, common_llm_kwargs,
                                 per_test_common_llm_kwargs,
                                 baseline_llm_kwargs, test_llm_kwargs,
                                 batch_size: int, output_len: int, seed: int,
                                 logprobs: int, prefill_chunk_size: int):
    """Verify greedy equality with different batch size."""
    maybe_enable_chunked_prefill(prefill_chunk_size, test_llm_kwargs)
    # NOTE Test is sensitive enough st if we don't enable chunked prefill
    # scheduling on baseline too, we get slightly different logprobs, ending
    # up sampling different tokens at the tail (ie top tokens don't change).
    # TL;DR: sd+cp == org+cp but sd+cp != org..is this expected?
    maybe_enable_chunked_prefill(prefill_chunk_size, baseline_llm_kwargs)
    run_equality_correctness_test(
        vllm_runner,
        common_llm_kwargs,
        per_test_common_llm_kwargs,
        baseline_llm_kwargs,
        test_llm_kwargs,
        batch_size,
        max_output_len=output_len,
        seed=seed,
        temperature=0.0,
        logprobs=logprobs,
        prompt_logprobs=logprobs,
        disable_logprobs=test_llm_kwargs["speculative_config"]
        ["disable_logprobs"])


@pytest.mark.parametrize(
    "common_llm_kwargs",
    [{
        # Skip cuda graph recording for fast test.
        "enforce_eager": True,

        # Print spec metrics.
        "disable_log_stats": False,

        # Precision
        "dtype": PRECISION,

        # Main model
        "model_name": MAIN_MODEL,
    }])
@pytest.mark.parametrize("per_test_common_llm_kwargs", [{}])
@pytest.mark.parametrize("baseline_llm_kwargs", [{}])
@pytest.mark.parametrize("test_llm_kwargs", [
    {
        "speculative_config": {
            "model": SPEC_MODEL,
        },
    },
])
@pytest.mark.parametrize("output_len", [2048])
@pytest.mark.parametrize("batch_size", [1, 32])
@pytest.mark.parametrize("seed", [1])
@pytest.mark.parametrize("prefill_chunk_size", PREFILL_CHUNK_SIZE_1)
def test_mlp_e2e_acceptance_rate(vllm_runner, common_llm_kwargs,
                                 per_test_common_llm_kwargs,
                                 baseline_llm_kwargs, test_llm_kwargs,
                                 batch_size: int, output_len: int,
                                 prefill_chunk_size: int, seed: int):
    """Verify acceptance rate with different batch size and large output 
    length."""
    maybe_enable_chunked_prefill(prefill_chunk_size, test_llm_kwargs)
    run_equality_correctness_test(vllm_runner,
                                  common_llm_kwargs,
                                  per_test_common_llm_kwargs,
                                  baseline_llm_kwargs,
                                  test_llm_kwargs,
                                  batch_size,
                                  max_output_len=output_len,
                                  temperature=0.0,
                                  seed=seed,
                                  expected_acceptance_rate=0.48)


@pytest.mark.parametrize(
    "common_llm_kwargs",
    [{
        # Skip cuda graph recording for fast test.
        "enforce_eager": True,

        # Print spec metrics.
        "disable_log_stats": False,

        # Precision
        "dtype": PRECISION,

        # Main model
        "model_name": MAIN_MODEL,

        # Speculative config
        "speculative_config": {
            "model": SPEC_MODEL,
        },
    }])
@pytest.mark.parametrize("per_test_common_llm_kwargs", [{}])
@pytest.mark.parametrize("baseline_llm_kwargs", [{"seed": 1}])
@pytest.mark.parametrize("test_llm_kwargs", [{"seed": 5}])
@pytest.mark.parametrize("output_len", [64])
@pytest.mark.parametrize("batch_size", [1, 32])
@pytest.mark.parametrize("temperature", [1.0])
@pytest.mark.parametrize("prefill_chunk_size", PREFILL_CHUNK_SIZE_1)
@pytest.mark.parametrize("seed", [1])
def test_mlp_e2e_seeded_correctness(vllm_runner, common_llm_kwargs,
                                    per_test_common_llm_kwargs,
                                    baseline_llm_kwargs, test_llm_kwargs,
                                    batch_size: int, output_len: int,
                                    temperature: float,
                                    prefill_chunk_size: int, seed: int):
    """Verify seeded runs produce the same output."""
    maybe_enable_chunked_prefill(prefill_chunk_size, test_llm_kwargs)
    maybe_enable_chunked_prefill(prefill_chunk_size, baseline_llm_kwargs)
    run_equality_correctness_test(vllm_runner,
                                  common_llm_kwargs,
                                  per_test_common_llm_kwargs,
                                  baseline_llm_kwargs,
                                  test_llm_kwargs,
                                  batch_size,
                                  max_output_len=output_len,
                                  temperature=temperature,
                                  seed=seed)

    # Ensure this same test does fail if we _don't_ include per-request seeds
    with pytest.raises(AssertionError):
        run_equality_correctness_test(vllm_runner,
                                      common_llm_kwargs,
                                      per_test_common_llm_kwargs,
                                      baseline_llm_kwargs,
                                      test_llm_kwargs,
                                      batch_size,
                                      max_output_len=output_len,
                                      temperature=temperature,
                                      seed=seed,
                                      disable_seed=True)


@pytest.mark.skipif(True, reason="Open it when preempt ready.")
@pytest.mark.parametrize(
    "common_llm_kwargs",
    [{
        "block_size": 16,
        # 2 for small prompt, 256//8 for generated.
        "num_gpu_blocks_override": 2 + 256 // 8,
        "max_model_len": (2 + 256 // 8) * 8,

        # Skip cuda graph recording for fast test.
        "enforce_eager": True,

        # Precision
        "dtype": PRECISION,

        # Main model
        "model_name": MAIN_MODEL,
    }])
@pytest.mark.parametrize("per_test_common_llm_kwargs", [{}])
@pytest.mark.parametrize("baseline_llm_kwargs", [{}])
@pytest.mark.parametrize("test_llm_kwargs", [
    {
        "speculative_config": {
            "model": SPEC_MODEL,
        },
    },
])
@pytest.mark.parametrize(
    "output_len",
    [
        # Use small output len for fast test.
        128,
    ])
@pytest.mark.parametrize("batch_size", [4])
@pytest.mark.parametrize("prefill_chunk_size", PREFILL_CHUNK_SIZE_1)
@pytest.mark.parametrize("seed", [1])
def test_mlp_e2e_greedy_correctness_with_preemption(
        vllm_runner, common_llm_kwargs, per_test_common_llm_kwargs,
        baseline_llm_kwargs, test_llm_kwargs, batch_size: int, output_len: int,
        prefill_chunk_size: int, seed: int):
    """Verify greedy equality, even when some sequences are preempted mid-
    generation.
    """
    maybe_enable_chunked_prefill(prefill_chunk_size, test_llm_kwargs)
    run_equality_correctness_test(vllm_runner,
                                  common_llm_kwargs,
                                  per_test_common_llm_kwargs,
                                  baseline_llm_kwargs,
                                  test_llm_kwargs,
                                  batch_size,
                                  max_output_len=output_len,
                                  seed=seed,
                                  temperature=0.0)


@pytest.mark.skipif(True, reason="Open it when preempt ready.")
@pytest.mark.parametrize(
    "common_llm_kwargs",
    [{
        "block_size": 16,
        # 2 for small prompt, 256//8 for generated.
        "num_gpu_blocks_override": 2 + 256 // 8,
        "max_model_len": (2 + 256 // 8) * 8,

        # Skip cuda graph recording for fast test.
        "enforce_eager": True,

        # Precision
        "dtype": PRECISION,

        # Main model
        "model_name": MAIN_MODEL,
    }])
@pytest.mark.parametrize("per_test_common_llm_kwargs", [{}])
@pytest.mark.parametrize("baseline_llm_kwargs", [{}])
@pytest.mark.parametrize("test_llm_kwargs", [
    {
        "speculative_config": {
            "model": SPEC_MODEL,
        },
    },
])
@pytest.mark.parametrize(
    "output_len",
    [
        # Use small output len for fast test.
        128,
    ])
@pytest.mark.parametrize("batch_size", [4])
@pytest.mark.parametrize("seed", [1])
@pytest.mark.parametrize("prefill_chunk_size", PREFILL_CHUNK_SIZE_1)
def test_mlp_e2e_greedy_correctness_with_padding(
        vllm_runner, common_llm_kwargs, per_test_common_llm_kwargs,
        baseline_llm_kwargs, test_llm_kwargs, batch_size: int, output_len: int,
        prefill_chunk_size: int, seed: int):
    """Verify greedy equality when the vocab dimension is padded
    """
    maybe_enable_chunked_prefill(prefill_chunk_size, test_llm_kwargs)

    # Default pad_to is 64, test model has vocab_size of 32000
    def patched_pad_vocab_size(vocab_size, pad_to=None):
        return pad_vocab_size(vocab_size, pad_to=32064)

    # NOTE: Compared with vLLM, the patch method has been modified
    pad_vocab_size = patched_pad_vocab_size  # noqa: F811
    run_equality_correctness_test(vllm_runner,
                                  common_llm_kwargs,
                                  per_test_common_llm_kwargs,
                                  baseline_llm_kwargs,
                                  test_llm_kwargs,
                                  batch_size,
                                  max_output_len=output_len,
                                  seed=seed,
                                  temperature=0.0)


@pytest.mark.parametrize(
    "common_llm_kwargs",
    [{
        # Skip cuda graph recording for fast test.
        "enforce_eager": True,

        # Precision
        "dtype": PRECISION,

        # Main model
        "model_name": MAIN_MODEL,
    }])
@pytest.mark.parametrize("per_test_common_llm_kwargs", [{}])
@pytest.mark.parametrize("baseline_llm_kwargs", [{}])
@pytest.mark.parametrize(
    "test_llm_kwargs",
    [
        {
            "speculative_config": {
                "model": SPEC_MODEL,
                "num_speculative_tokens": k,
            },
        }
        # Try a range of num. speculative tokens
        for k in range(1, 1 + MAX_SPEC_TOKENS)
    ])
@pytest.mark.parametrize("batch_size", [2])
@pytest.mark.parametrize(
    "output_len",
    [
        # Use smaller output len for fast test.
        32,
    ])
@pytest.mark.parametrize("prefill_chunk_size", PREFILL_CHUNK_SIZE_1)
@pytest.mark.parametrize("seed", [1])
def test_mlp_different_k(vllm_runner, common_llm_kwargs,
                         per_test_common_llm_kwargs, baseline_llm_kwargs,
                         test_llm_kwargs, batch_size: int,
                         prefill_chunk_size: int, seed: int, output_len: int):
    """Verify that mlp speculative decoding produces exact equality
    to without spec decode with different values of num_speculative_tokens.
    """
    maybe_enable_chunked_prefill(prefill_chunk_size, test_llm_kwargs)
    run_equality_correctness_test(vllm_runner,
                                  common_llm_kwargs,
                                  per_test_common_llm_kwargs,
                                  baseline_llm_kwargs,
                                  test_llm_kwargs,
                                  batch_size,
                                  max_output_len=output_len,
                                  seed=seed,
                                  temperature=0.0)


@pytest.mark.parametrize(
    "common_llm_kwargs",
    [{
        # Skip cuda graph recording for fast test.
        "enforce_eager": True,

        # Precision
        "dtype": PRECISION,

        # Main model
        "model_name": MAIN_MODEL,
    }])
@pytest.mark.parametrize("per_test_common_llm_kwargs", [{}])
@pytest.mark.parametrize("baseline_llm_kwargs", [{}])
@pytest.mark.parametrize("test_llm_kwargs", [{
    "speculative_config": {
        "model": SPEC_MODEL,
        "disable_by_batch_size": 4,
    },
}])
@pytest.mark.parametrize("batch_size", [1, 5])
@pytest.mark.parametrize(
    "output_len",
    [
        # Use smaller output len for fast test.
        32,
    ])
# Speculative decoding is disabled when sequences reach decoding and the batch
# consists of single-token requests. Hence we set `max_num_seqs`
# >= `speculative_disable_by_batch_size` to test feature interaction.
@pytest.mark.parametrize("prefill_chunk_size", PREFILL_CHUNK_SIZE_1)
@pytest.mark.parametrize("seed", [1])
def test_mlp_disable_queue(vllm_runner, common_llm_kwargs,
                           per_test_common_llm_kwargs, baseline_llm_kwargs,
                           test_llm_kwargs, batch_size: int,
                           prefill_chunk_size: int, seed: int,
                           output_len: int):
    """Verify that mlp speculative decoding produces exact equality
    to without spec decode when speculation is disabled for large
    batch sizes.
    """
    maybe_enable_chunked_prefill(prefill_chunk_size, test_llm_kwargs)
    run_equality_correctness_test(vllm_runner,
                                  common_llm_kwargs,
                                  per_test_common_llm_kwargs,
                                  baseline_llm_kwargs,
                                  test_llm_kwargs,
                                  batch_size,
                                  max_output_len=output_len,
                                  seed=seed,
                                  temperature=0.0)


@pytest.mark.parametrize(
    "common_llm_kwargs",
    [{
        "model_name": MAIN_MODEL,

        # Skip cuda graph recording for fast test.
        "enforce_eager": True,
    }])
@pytest.mark.parametrize("per_test_common_llm_kwargs", [{}])
@pytest.mark.parametrize("baseline_llm_kwargs", [{}])
@pytest.mark.parametrize("test_llm_kwargs", [{
    "speculative_config": {
        "model": SPEC_MODEL,
        "disable_mqa_scorer": True,
    },
}])
@pytest.mark.parametrize("batch_size", [1, 5])
@pytest.mark.parametrize(
    "output_len",
    [
        # Use smaller output len for fast test.
        32,
    ])
@pytest.mark.parametrize("prefill_chunk_size", PREFILL_CHUNK_SIZE_1)
@pytest.mark.parametrize("seed", [1])
def test_mqa_scorer(vllm_runner, common_llm_kwargs, per_test_common_llm_kwargs,
                    baseline_llm_kwargs, test_llm_kwargs, batch_size: int,
                    output_len: int, prefill_chunk_size: int, seed: int):
    """Verify that speculative decoding generates the same output 
    with batch expansion scorer and mqa scorer.
    """
    maybe_enable_chunked_prefill(prefill_chunk_size, test_llm_kwargs)
    run_equality_correctness_test(vllm_runner,
                                  common_llm_kwargs,
                                  per_test_common_llm_kwargs,
                                  baseline_llm_kwargs,
                                  test_llm_kwargs,
                                  batch_size,
                                  max_output_len=output_len,
                                  seed=seed,
                                  temperature=0.0)
