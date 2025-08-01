#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# This file is a part of the vllm-ascend project.
# Adapted from vllm-project/vllm/tests/spec_decode/test_multi_step_worker.py
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

import random
from unittest.mock import MagicMock

import pytest
import torch
from vllm.model_executor.layers.sampler import SamplerOutput
from vllm.model_executor.utils import set_random_seed
from vllm.sequence import (ExecuteModelRequest, HiddenStates, Logprob,
                           get_all_seq_ids)
from vllm.spec_decode.multi_step_worker import MultiStepWorker
from vllm.spec_decode.top1_proposer import Top1Proposer

from tests.long_term.spec_decode_v0.utils import (
    assert_logprobs_dict_allclose, create_batch,
    create_seq_group_metadata_from_prompts, create_worker,
    patch_execute_model_with_seeds, zero_kv_cache)
from vllm_ascend.worker.draft_model_runner import TP1DraftModelRunner
from vllm_ascend.worker.worker import NPUWorker


@pytest.mark.parametrize('num_steps', list(range(1, 17)))
def test_assert_enough_kv_space(num_steps: int):
    """Test that the multi step worker checks for sufficient space in the KV
    cache. It should throw if it cannot run all the steps.
    """
    block_size = 16
    num_gpu_blocks = 2048 // block_size

    prompts = [
        list(range(block_size * 3)),
        list(range(block_size * 2)),
    ]

    prev_output_tokens = [
        list(range(block_size * 1)),
        list(range(block_size * 2)),
    ]

    final_prompt_lens = [
        len(prompt + output) + num_steps
        for prompt, output in zip(prompts, prev_output_tokens)
    ]

    inputs = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        final_prompt_lens,
        continuations=prev_output_tokens)

    assert_enough_kv_space = MultiStepWorker._assert_enough_kv_space  # pylint: disable=protected-access
    worker = MagicMock()
    worker.model_runner.block_size = block_size

    for seq_group_metadata in inputs:
        original_block_tables = seq_group_metadata.block_tables

        # No exception.
        assert_enough_kv_space(worker, inputs, num_steps)

        seq_group_metadata.block_tables = {
            seq_id: []
            for seq_id, physical_blocks in original_block_tables.items()
        }

        # Expect exception.
        with pytest.raises(ValueError,
                           match='times but found insufficient KV space for'):
            assert_enough_kv_space(worker, inputs, num_steps)

        seq_group_metadata.block_tables = original_block_tables


@torch.inference_mode()
def test_same_output_for_single_step():
    """Verify the multi step worker produces the same output as the normal
    worker for num_steps=1.
    """
    seed = 100
    model_name = 'JackFram/llama-68m'

    block_size = 32
    num_gpu_blocks = 2048 // block_size
    multi_step_worker = create_worker(
        MultiStepWorker,
        model_name,
        block_size,
        num_gpu_blocks,
        seed,
        model_runner_cls=TP1DraftModelRunner,
    )
    worker = create_worker(
        NPUWorker,
        model_name,
        block_size,
        num_gpu_blocks,
        seed,
    )
    # multi_step_worker.model_runner = worker.model_runner
    # multi_step_worker.cache_engine = worker.cache_engine

    num_steps = 1

    prompts = [
        [1, 2, 3, 4, 5],
        [6, 7, 8, 9, 10],
    ]

    final_prompt_lens = [len(prompt) + num_steps for prompt in prompts]

    multi_step_seq_group = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        final_prompt_lens=final_prompt_lens)

    zero_kv_cache(multi_step_worker.cache_engine)
    set_random_seed(seed)
    actual_output, _ = multi_step_worker.sampler_output(
        execute_model_req=ExecuteModelRequest(
            seq_group_metadata_list=multi_step_seq_group),
        sample_len=num_steps,
        seq_ids_with_bonus_token_in_last_step=set())
    assert len(actual_output) == num_steps
    actual_output = actual_output[0]

    single_step_seq_group = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        final_prompt_lens=final_prompt_lens)

    zero_kv_cache(worker.cache_engine)
    set_random_seed(seed)
    expected_output = worker.execute_model(
        execute_model_req=ExecuteModelRequest(
            seq_group_metadata_list=single_step_seq_group))[0]

    actual_token_ids = [
        output.samples[0].output_token for output in actual_output
    ]
    actual_logprobs = [output.samples[0].logprobs for output in actual_output]

    expected_token_ids = [
        output.samples[0].output_token for output in expected_output
    ]
    expected_logprobs = [
        output.samples[0].logprobs for output in expected_output
    ]

    assert actual_token_ids == expected_token_ids

    print(f'{actual_logprobs=}')
    print(f'{expected_logprobs=}')
    assert_logprobs_dict_allclose(actual_logprobs, expected_logprobs)


@torch.inference_mode()
def test_same_output_for_multi_step():
    """Verify the multi-step worker produces the same output as the normal
    worker when num_steps > 1. This test runs the multi-step worker once, and
    then runs the worker num_steps times, and compares the output.
    """
    seed = 100
    model_name = 'JackFram/llama-68m'

    block_size = 16
    num_gpu_blocks = 2048 // block_size
    multi_step_worker = create_worker(
        MultiStepWorker,
        model_name,
        block_size,
        num_gpu_blocks,
        seed,
    )

    worker = create_worker(
        NPUWorker,
        model_name,
        block_size,
        num_gpu_blocks,
        seed,
    )

    # Make sure we go over the block boundary.
    num_steps = block_size + 1

    random.seed(seed)
    prompts = [[
        random.randint(0, 1000) for _ in range(random.randint(10, 20))
    ] for _ in range(10)]

    final_prompt_lens = [len(prompt) + num_steps for prompt in prompts]

    rand_seeds = list(random.randint(0, 100) for _ in range(num_steps))
    multi_step_worker.execute_model = patch_execute_model_with_seeds(
        multi_step_worker, rand_seeds)
    worker.execute_model = patch_execute_model_with_seeds(worker, rand_seeds)

    continuations = [[1] for _ in prompts]
    seq_group_metadata_list = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        continuations=continuations,
        final_prompt_lens=final_prompt_lens)

    # Run multi-step.
    zero_kv_cache(multi_step_worker.cache_engine)
    set_random_seed(seed)
    multi_step_output, _ = multi_step_worker.sampler_output(
        execute_model_req=ExecuteModelRequest(
            seq_group_metadata_list=seq_group_metadata_list),
        sample_len=num_steps,
        seq_ids_with_bonus_token_in_last_step=set())

    # Run single-step repeatedly.
    zero_kv_cache(worker.cache_engine)
    single_step_output: list[SamplerOutput] = []
    continuations = [[1] for _ in prompts]
    set_random_seed(seed)

    for _ in multi_step_output:

        seq_group_metadata_list = create_seq_group_metadata_from_prompts(
            prompts,
            num_gpu_blocks,
            block_size,
            continuations=continuations,
            final_prompt_lens=final_prompt_lens)

        single_step_output.extend(
            worker.execute_model(execute_model_req=ExecuteModelRequest(
                seq_group_metadata_list=seq_group_metadata_list)))

        # Append output tokens to new sequence data.
        for i, seq_group_output in enumerate(single_step_output[-1]):
            continuations[i].append(seq_group_output.samples[0].output_token)

    # Get token ids and logprobs for comparison.
    multi_step_output_logprobs: list[list[dict[int,
                                               Logprob]]] = [[]
                                                             for _ in prompts]
    single_step_output_logprobs: list[list[dict[int,
                                                Logprob]]] = [[]
                                                              for _ in prompts]

    multi_step_output_token_ids: list[list[int]] = [[] for _ in prompts]
    single_step_output_token_ids: list[list[int]] = [[] for _ in prompts]
    for i, _ in enumerate(prompts):
        for multi_step, single_step in zip(multi_step_output,
                                           single_step_output):
            multi_step_output_token_ids[i].append(
                multi_step[i].samples[0].output_token)
            single_step_output_token_ids[i].append(
                single_step[i].samples[0].output_token)

            multi_step_output_logprobs[i].append(
                multi_step[i].samples[0].logprobs)
            single_step_output_logprobs[i].append(
                single_step[i].samples[0].logprobs)

    # Print per-sequence token ids
    for i, (multi_step_tokens, single_step_tokens) in enumerate(
            zip(multi_step_output_token_ids, single_step_output_token_ids)):
        print(f'{i=} {multi_step_tokens=}')
        print(f'{i=} {single_step_tokens=}')
        print(f'{i=} equal {multi_step_tokens == single_step_tokens}')

    # Assert token ids are equal.
    for multi_step_tokens, single_step_tokens in zip(
            multi_step_output_token_ids, single_step_output_token_ids):
        assert multi_step_tokens == single_step_tokens

    # Assert logprobs are equal.
    for multi_step_logprobs, single_step_logprobs in zip(
            multi_step_output_logprobs, single_step_output_logprobs):
        assert_logprobs_dict_allclose(multi_step_logprobs,
                                      single_step_logprobs)


@torch.inference_mode()
def test_multi_step_with_batch_expansion_correct_output():
    """
    In this test we verify that the MultiStepWorker is able to handle bonus
    tokens correctly. The test verifies that if a sequence has a
    bonus token then the MultiStepWorker is able to expand the batch by adding
    new sequences corresponding to the sequences with bonus tokens. The
    expanded batch is then used for predicting the next tokens.
    """
    seed = 100
    model_name = 'JackFram/llama-68m'

    block_size = 16
    num_gpu_blocks = 2048 // block_size
    batch_size = 128
    multi_step_worker = create_worker(
        MultiStepWorker,
        model_name,
        block_size,
        num_gpu_blocks,
        seed,
        model_runner_cls=TP1DraftModelRunner,
    )
    multi_step_worker.set_include_gpu_probs_tensor()
    worker = create_worker(
        NPUWorker,
        model_name,
        block_size,
        num_gpu_blocks,
        seed,
    )
    random.seed(seed)
    prompts = [[0] for _ in range(batch_size)]
    num_steps = 2
    final_prompt_lens = [(num_steps + 1) for prompt in prompts]
    rand_seeds = list(random.randint(0, 100) for _ in range(num_steps))
    multi_step_worker.execute_model = patch_execute_model_with_seeds(
        multi_step_worker, rand_seeds)
    worker.execute_model = patch_execute_model_with_seeds(worker, rand_seeds)
    # Create the test continuations
    continuations = [[random.randint(0, 1000)] for _ in prompts]
    seq_group_metadata_list = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        continuations=continuations,
        final_prompt_lens=final_prompt_lens)

    # Run single-step twice to generate 2 tokens. This
    # will simulate the bonus token case with the second token
    # being the bonus token.
    zero_kv_cache(worker.cache_engine)
    single_step_output: list[SamplerOutput] = []
    set_random_seed(seed)
    for _ in range(num_steps):
        seq_group_metadata_list = create_seq_group_metadata_from_prompts(
            prompts,
            num_gpu_blocks,
            block_size,
            continuations=continuations,
            final_prompt_lens=final_prompt_lens)
        single_step_output.extend(
            worker.execute_model(execute_model_req=ExecuteModelRequest(
                seq_group_metadata_list=seq_group_metadata_list)))
        # Append output tokens to new sequence data.
        for i, seq_group_output in enumerate(single_step_output[-1]):
            continuations[i].append(seq_group_output.samples[0].output_token)

    # Create continuations for the MultiStepWorker. The continuations have
    # 2 tokens in order to simulate the bonus token case.
    multi_step_continuations = []
    for continuation in continuations:
        multi_step_continuations.append(continuation[:2])
    seq_group_metadata_list = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        continuations=multi_step_continuations,
        final_prompt_lens=final_prompt_lens)

    # Run multi-step and verify that the third token prediction is accurate
    # for all sequences.
    zero_kv_cache(multi_step_worker.cache_engine)
    all_seq_ids = {i for i in range(batch_size)}
    multi_step_output, _ = multi_step_worker.sampler_output(
        execute_model_req=ExecuteModelRequest(
            seq_group_metadata_list=seq_group_metadata_list),
        sample_len=1,
        seq_ids_with_bonus_token_in_last_step=all_seq_ids)
    for index, output in enumerate(multi_step_output[-1].outputs):
        assert (continuations[index][-1] == output.samples[0].output_token)


@torch.inference_mode()
def test_multi_step_with_batch_expansion_incorrect_output():
    """
    Tests the MultiStepWorker's ability to handle batch expansion with bonus
    tokens in a negative case scenario. This test provides the MultiStepWorker
    with a batch containing sequences with bonus tokens but specifies the
    sequence IDs with bonus tokens incorrectly. The test verifies that the
    MultiStepWorker generates correct tokens for the sequences where the
    sequence ID is specified correctly and incorrect tokens for those where
    the sequence ID is specified incorrectly.
    """
    seed = 100
    model_name = 'JackFram/llama-68m'

    block_size = 16
    num_gpu_blocks = 2048 // block_size
    batch_size = 128
    multi_step_worker = create_worker(
        MultiStepWorker,
        model_name,
        block_size,
        num_gpu_blocks,
        seed,
        model_runner_cls=TP1DraftModelRunner,
    )
    multi_step_worker.set_include_gpu_probs_tensor()
    worker = create_worker(
        NPUWorker,
        model_name,
        block_size,
        num_gpu_blocks,
        seed,
    )
    random.seed(seed)
    prompts = [[0] for _ in range(batch_size)]
    num_steps = 2
    final_prompt_lens = [(num_steps + 1) for prompt in prompts]
    rand_seeds = list(random.randint(0, 100) for _ in range(num_steps))
    multi_step_worker.execute_model = patch_execute_model_with_seeds(
        multi_step_worker, rand_seeds)
    worker.execute_model = patch_execute_model_with_seeds(worker, rand_seeds)
    # Create the test continuations
    continuations = [[random.randint(0, 1000)] for _ in prompts]
    seq_group_metadata_list = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        continuations=continuations,
        final_prompt_lens=final_prompt_lens)
    # Run single-step twice to generate 2 tokens. This
    # will simulate the bonus token case with the second token
    # being the bonus token.
    zero_kv_cache(worker.cache_engine)
    single_step_output: list[SamplerOutput] = []
    set_random_seed(seed)
    for _ in range(num_steps):
        seq_group_metadata_list = create_seq_group_metadata_from_prompts(
            prompts,
            num_gpu_blocks,
            block_size,
            continuations=continuations,
            final_prompt_lens=final_prompt_lens)
        single_step_output.extend(
            worker.execute_model(execute_model_req=ExecuteModelRequest(
                seq_group_metadata_list=seq_group_metadata_list)))
        # Append output tokens to new sequence data.
        for i, seq_group_output in enumerate(single_step_output[-1]):
            continuations[i].append(seq_group_output.samples[0].output_token)

    # Create continuations for the MultiStepWorker. The continuations have
    # 2 tokens in order to simulate the bonus token case.
    multi_step_continuations = []
    for continuation in continuations:
        multi_step_continuations.append(continuation[:2])
    seq_group_metadata_list = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        continuations=multi_step_continuations,
        final_prompt_lens=final_prompt_lens)

    # Run multi-step. In this run INCORRECTLY specify that only the odd number
    # sequences have bonus tokens. Verify that with this setting the third token
    # prediction is accurate only for the odd numbered sequences. Also verify
    # that the prediction might be wrong for some of the even numbered
    # sequences.
    zero_kv_cache(multi_step_worker.cache_engine)
    set_random_seed(seed)
    odd_seq_ids = {i for i in range(batch_size) if i % 2 != 0}
    multi_step_output, _ = multi_step_worker.sampler_output(
        execute_model_req=ExecuteModelRequest(
            seq_group_metadata_list=seq_group_metadata_list),
        sample_len=1,
        seq_ids_with_bonus_token_in_last_step=odd_seq_ids)
    num_mismatch = 0
    for index, output in enumerate(multi_step_output[-1].outputs):
        if (index % 2) != 0:
            assert (continuations[index][-1] == output.samples[0].output_token)
        elif (continuations[index][-1] != output.samples[0].output_token):
            num_mismatch += 1
    # The prediction is accurate for some of the sequences even without proper
    # handling of the bonus tokens. Hence verify that the number of sequences
    # for which there is a mismatch is > 0.
    assert (num_mismatch > 0)


@torch.inference_mode()
@pytest.mark.parametrize('num_steps', [1, 2, 3, 4])
def test_multi_step_correct_kvcache(num_steps):
    """Verify that the KV cache of the draft model 
    is correctly updated for sequences with bonus token.
    """
    seed = 100
    model_name = "JackFram/llama-68m"

    block_size = 16
    num_gpu_blocks = 2048 // block_size
    batch_size = 1

    dtype = 'float16'
    multi_step_worker = create_worker(MultiStepWorker,
                                      model_name,
                                      block_size,
                                      num_gpu_blocks,
                                      seed,
                                      model_runner_cls=TP1DraftModelRunner,
                                      dtype=dtype)
    multi_step_worker.set_include_gpu_probs_tensor()
    worker = create_worker(NPUWorker,
                           model_name,
                           block_size,
                           num_gpu_blocks,
                           seed,
                           dtype=dtype)

    prompts = [[0] for _ in range(batch_size)]
    # Already generate two tokens for the sequence
    # so that we can simulate the bonus token case
    multi_step_continuations = [[
        random.randint(0, 1000),
        random.randint(0, 1000)
    ] for _ in prompts]
    final_prompt_lens = [len(prompt) + 2 + num_steps for prompt in prompts]

    seq_ids_with_bonus_token_in_last_step = set(range(batch_size))
    seq_group_metadata_list = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        continuations=multi_step_continuations,
        final_prompt_lens=final_prompt_lens)

    # Run multi-step.
    zero_kv_cache(multi_step_worker.cache_engine)
    multi_step_worker.sampler_output(execute_model_req=ExecuteModelRequest(
        seq_group_metadata_list=seq_group_metadata_list),
                                     sample_len=num_steps,
                                     seq_ids_with_bonus_token_in_last_step=
                                     seq_ids_with_bonus_token_in_last_step)

    # Run single-step repeatedly.
    zero_kv_cache(worker.cache_engine)
    # Generate the kv cache for the bonus token first
    single_step_continuations = [c[:1] for c in multi_step_continuations]
    seq_group_metadata_list = create_seq_group_metadata_from_prompts(
        prompts,
        num_gpu_blocks,
        block_size,
        continuations=single_step_continuations,
        final_prompt_lens=final_prompt_lens)
    single_step_output = worker.execute_model(
        execute_model_req=ExecuteModelRequest(
            seq_group_metadata_list=seq_group_metadata_list))
    for _ in range(num_steps):
        seq_group_metadata_list = create_seq_group_metadata_from_prompts(
            prompts,
            num_gpu_blocks,
            block_size,
            continuations=multi_step_continuations,
            final_prompt_lens=final_prompt_lens)

        single_step_output = worker.execute_model(
            execute_model_req=ExecuteModelRequest(
                seq_group_metadata_list=seq_group_metadata_list))

        for i, seq_group_output in enumerate(single_step_output[-1]):
            multi_step_continuations[i].append(
                seq_group_output.samples[0].output_token)

    # Verify that the KV cache of the single-step and
    # multi-step workers are the same.
    single_step_gpu_cache = worker.cache_engine[0].gpu_cache
    multi_step_gpu_cache = multi_step_worker.cache_engine[0].gpu_cache
    num_layers = len(single_step_gpu_cache)
    allclose = lambda a, b: torch.allclose(  # noqa: E731
        a.npu(), b.npu(), rtol=1e-2, atol=1e-2)
    for i in range(num_layers):
        assert allclose(single_step_gpu_cache[i][0],
                        multi_step_gpu_cache[i][0])
        assert allclose(single_step_gpu_cache[i][1],
                        multi_step_gpu_cache[i][1])


@torch.inference_mode()
def test_draft_proposals_full_speculation_len():
    """Verify Top1Proposer correctly handles case where all sequences
    can speculate.
    """
    k = 10
    batch_size = 32
    vocab_size = 32_000
    device = 'npu:0'

    draft_worker = MagicMock()
    proposer = Top1Proposer(
        worker=draft_worker,
        device=device,
        vocab_size=vocab_size,
        max_proposal_len=2048,
    )
    draft_worker.sampler_output.return_value = [
        SamplerOutput(
            outputs=[],
            sampled_token_probs=torch.rand(batch_size,
                                           vocab_size,
                                           device=device,
                                           dtype=torch.float32),
            logprobs=torch.rand(batch_size,
                                vocab_size,
                                device=device,
                                dtype=torch.float32),
            sampled_token_ids=torch.randint(low=0,
                                            high=vocab_size,
                                            size=(batch_size, ),
                                            device=device,
                                            dtype=torch.long),
        ) for _ in range(k)
    ], True

    seq_group_metadata_list, _, _ = create_batch(batch_size, k)

    proposals = proposer.get_spec_proposals(
        execute_model_req=ExecuteModelRequest(
            seq_group_metadata_list=seq_group_metadata_list,
            num_lookahead_slots=k),
        seq_ids_with_bonus_token_in_last_step=set())

    assert torch.is_tensor(proposals.proposal_token_ids)
    assert torch.is_tensor(proposals.proposal_probs)

    assert proposals.proposal_token_ids.shape == torch.Size([batch_size, k])
    assert proposals.proposal_probs.shape[:-1] == torch.Size([batch_size, k])

    assert proposals.proposal_lens.shape == torch.Size([batch_size])
    assert proposals.proposal_lens.tolist() == [k for _ in range(batch_size)]


@torch.inference_mode()
def test_draft_proposals_no_speculations():
    """Verify Top1Proposer correctly handles case where no sequences
    can speculate.
    """
    k = 10
    batch_size = 32
    vocab_size = 32_000
    device = 'npu:0'
    prompt_len = 10

    draft_worker = MagicMock()
    proposer = Top1Proposer(
        worker=draft_worker,
        device=device,
        vocab_size=vocab_size,
        max_proposal_len=prompt_len + k - 1,
    )

    seq_group_metadata_list, _, _ = create_batch(batch_size,
                                                 k,
                                                 prompt_len=prompt_len)

    proposals = proposer.get_spec_proposals(
        execute_model_req=ExecuteModelRequest(
            seq_group_metadata_list=seq_group_metadata_list,
            num_lookahead_slots=k),
        seq_ids_with_bonus_token_in_last_step=set())

    assert torch.is_tensor(proposals.proposal_token_ids)
    assert torch.is_tensor(proposals.proposal_probs)

    assert proposals.proposal_token_ids.shape == torch.Size([batch_size, k])
    assert proposals.proposal_probs.shape[:-1] == torch.Size([batch_size, k])

    assert proposals.proposal_lens.shape == torch.Size([batch_size])
    assert proposals.proposal_lens.tolist() == [0 for _ in range(batch_size)]


@torch.inference_mode()
def test_draft_proposals_mixed_k():
    """Verify Top1Proposer correctly handles case some sequences can
    speculate and some can't.
    """
    k = 10
    batch_size = 32
    vocab_size = 32_000
    device = 'npu:0'

    small_prompt_len = 5
    long_prompt_len = 10
    prev_output_token_len = 20

    expected_num_proposal_seqs = 6
    expected_num_no_proposal_seqs = batch_size - expected_num_proposal_seqs

    prompt_len = [
        small_prompt_len for _ in range(expected_num_proposal_seqs - 1)
    ] + [long_prompt_len
         for _ in range(expected_num_no_proposal_seqs)] + [small_prompt_len]

    draft_worker = MagicMock()
    proposer = Top1Proposer(
        worker=draft_worker,
        device=device,
        vocab_size=vocab_size,
        max_proposal_len=long_prompt_len + prev_output_token_len + k - 1,
    )

    draft_worker.sampler_output.return_value = [
        SamplerOutput(
            outputs=[],
            sampled_token_probs=torch.rand(expected_num_proposal_seqs,
                                           vocab_size,
                                           device=device,
                                           dtype=torch.float32),
            logprobs=torch.rand(expected_num_proposal_seqs,
                                vocab_size,
                                device=device,
                                dtype=torch.float32),
            sampled_token_ids=torch.randint(
                low=0,
                high=vocab_size,
                size=(expected_num_proposal_seqs, ),
                device=device,
                dtype=torch.long),
        ) for _ in range(k)
    ], True

    seq_group_metadata_list, _, _ = create_batch(
        batch_size,
        k,
        prompt_len=prompt_len,
        prev_output_token_len=prev_output_token_len,
    )

    proposals = proposer.get_spec_proposals(
        execute_model_req=ExecuteModelRequest(
            seq_group_metadata_list=seq_group_metadata_list,
            num_lookahead_slots=k),
        seq_ids_with_bonus_token_in_last_step=set())

    assert torch.is_tensor(proposals.proposal_token_ids)
    assert torch.is_tensor(proposals.proposal_probs)

    assert proposals.proposal_token_ids.shape == torch.Size([batch_size, k])
    assert proposals.proposal_probs.shape[:-1] == torch.Size([batch_size, k])

    assert proposals.proposal_lens.shape == torch.Size([batch_size])
    assert proposals.proposal_lens.tolist() == [
        k for _ in range(expected_num_proposal_seqs - 1)
    ] + [0 for _ in range(expected_num_no_proposal_seqs)] + [k]


@torch.inference_mode()
def test_use_draft_model_runner_advance_step():
    """Verify that draft model runner triggers advance step
    when applicable.
    """
    seed = 100
    model_name = 'JackFram/llama-68m'

    k = 5
    batch_size = 32
    block_size = 32
    num_gpu_blocks = 2048 // block_size
    worker = create_worker(
        MultiStepWorker,
        model_name,
        block_size,
        num_gpu_blocks,
        seed,
        model_runner_cls=TP1DraftModelRunner,
    )

    # Mock "_gpu_advance_step" to raise an exception when called.
    exception_secret = "artificial stop"
    worker.model_runner._gpu_advance_step = MagicMock()
    worker.model_runner._gpu_advance_step.side_effect = ValueError(
        exception_secret)

    seq_group_metadata_list, _, _ = create_batch(batch_size,
                                                 k,
                                                 block_size=block_size,
                                                 num_gpu_blocks=num_gpu_blocks)

    # Fallback (should not call) when num_steps=1.
    execute_model_req = ExecuteModelRequest(
        seq_group_metadata_list=seq_group_metadata_list,
        num_lookahead_slots=k,
        num_steps=1)
    worker.execute_model(execute_model_req=execute_model_req)

    # Expect exception if _gpu_advance_step is called.
    execute_model_req = ExecuteModelRequest(
        seq_group_metadata_list=seq_group_metadata_list,
        num_lookahead_slots=k,
        num_steps=k)

    with pytest.raises(ValueError, match=exception_secret):
        worker.execute_model(execute_model_req=execute_model_req)
    call_args_list = worker.model_runner._gpu_advance_step.call_args_list
    assert len(call_args_list) == 1


@torch.inference_mode()
def test_expand_execute_model_request_sync_with_expand_hidden_states():
    """
    In this test we verify that the logic for expanding the 
    seq_group_metadata_list remains in sync with the expansion logic of 
    the HiddenStates in _expand_execute_model_request.
    """
    k = 5
    batch_size = 16
    seq_with_bonus_token_in_last_step = [1, 3, 8, 10, 13, 15]

    seq_group_metadata_list, _, _ = create_batch(batch_size, k)

    execute_model_request = ExecuteModelRequest(
        seq_group_metadata_list,
        previous_hidden_states=HiddenStates(
            torch.arange(batch_size), seq_group_metadata_list,
            torch.arange(batch_size, 2 * batch_size)))

    expanded_execute_model_request, orig_seq_group_ids = MultiStepWorker.\
        _expand_execute_model_request(execute_model_request,
                                      seq_with_bonus_token_in_last_step)

    all_seq_ids = torch.tensor(
        get_all_seq_ids(
            expanded_execute_model_request.seq_group_metadata_list))
    ref_expanded_hidden_states = all_seq_ids + batch_size
    ref_expanded_hidden_states[orig_seq_group_ids] -= batch_size

    assert (ref_expanded_hidden_states == expanded_execute_model_request.
            previous_hidden_states.hidden_states).all().item()
