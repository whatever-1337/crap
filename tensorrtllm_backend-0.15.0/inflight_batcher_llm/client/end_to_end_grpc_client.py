#!/usr/bin/python

import os
import sys
from functools import partial

sys.path.append(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import argparse
import json
import queue
import sys

import numpy as np
import tritonclient.grpc as grpcclient
from tritonclient.utils import InferenceServerException, np_to_triton_dtype


def prepare_tensor(name, input):
    t = grpcclient.InferInput(name, input.shape,
                              np_to_triton_dtype(input.dtype))
    t.set_data_from_numpy(input)
    return t


class UserData:

    def __init__(self):
        self._completed_requests = queue.Queue()


def callback(user_data, result, error):
    if error:
        user_data._completed_requests.put(error)
    else:
        user_data._completed_requests.put(result)


def prepare_inputs(prompt,
                   output_len,
                   repetition_penalty,
                   presence_penalty,
                   frequency_penalty,
                   temperature,
                   stop_words,
                   bad_words,
                   embedding_bias_words,
                   embedding_bias_weights,
                   streaming,
                   beam_width,
                   return_log_probs_data,
                   return_context_logits_data,
                   return_generation_logits_data,
                   end_id,
                   pad_id,
                   num_draft_tokens=0,
                   use_draft_logits=None,
                   num_return_sequences=1,
                   lora_dir=None,
                   lora_task_id=None):

    input0 = [[prompt]]
    input0_data = np.array(input0).astype(object)
    output0_len = np.ones_like(input0).astype(np.int32) * output_len
    streaming_data = np.array([[streaming]], dtype=bool)
    beam_width_data = np.array([[beam_width]], dtype=np.int32)
    temperature_data = np.array([[temperature]], dtype=np.float32)

    inputs = {
        "text_input": input0_data,
        "max_tokens": output0_len,
        "stream": streaming_data,
        "beam_width": beam_width_data,
        "temperature": temperature_data,
    }

    if num_return_sequences is not None:
        inputs["num_return_sequences"] = np.array([[num_return_sequences]],
                                                  dtype=np.int32)

    if num_draft_tokens > 0:
        inputs["num_draft_tokens"] = np.array([[num_draft_tokens]],
                                              dtype=np.int32)
    if use_draft_logits is not None:
        inputs["use_draft_logits"] = np.array([[use_draft_logits]], dtype=bool)

    if bad_words:
        bad_words_list = np.array([bad_words], dtype=object)
        inputs["bad_words"] = bad_words_list

    if stop_words:
        stop_words_list = np.array([stop_words], dtype=object)
        inputs["stop_words"] = stop_words_list

    if repetition_penalty is not None:
        repetition_penalty = [[repetition_penalty]]
        repetition_penalty_data = np.array(repetition_penalty,
                                           dtype=np.float32)
        inputs["repetition_penalty"] = repetition_penalty_data

    if presence_penalty is not None:
        presence_penalty = [[presence_penalty]]
        presence_penalty_data = np.array(presence_penalty, dtype=np.float32)
        inputs["presence_penalty"] = presence_penalty_data

    if frequency_penalty is not None:
        frequency_penalty = [[frequency_penalty]]
        frequency_penalty_data = np.array(frequency_penalty, dtype=np.float32)
        inputs["frequency_penalty"] = frequency_penalty_data

    if return_log_probs_data is not None:
        inputs["return_log_probs"] = return_log_probs_data

    if return_context_logits_data is not None:
        inputs["return_context_logits"] = return_context_logits_data

    if return_generation_logits_data is not None:
        inputs["return_generation_logits"] = return_generation_logits_data

    if (embedding_bias_words is not None and embedding_bias_weights is None
        ) or (embedding_bias_words is None
              and embedding_bias_weights is not None):
        assert 0, "Both embedding bias words and weights must be specified"

    if (embedding_bias_words is not None
            and embedding_bias_weights is not None):
        assert len(embedding_bias_words) == len(
            embedding_bias_weights
        ), "Embedding bias weights and words must have same length"
        embedding_bias_words_data = np.array([embedding_bias_words],
                                             dtype=object)
        embedding_bias_weights_data = np.array([embedding_bias_weights],
                                               dtype=np.float32)
        inputs["embedding_bias_words"] = embedding_bias_words_data
        inputs["embedding_bias_weights"] = embedding_bias_weights_data
    if end_id is not None:
        end_id_data = np.array([[end_id]], dtype=np.int32)
        inputs["end_id"] = end_id_data

    if pad_id is not None:
        pad_id_data = np.array([[pad_id]], dtype=np.int32)
        inputs["pad_id"] = pad_id_data

    if lora_dir and lora_task_id:
        inputs["lora_weights"] = np.load(
            os.path.join(lora_dir, "model.lora_weights.npy"))
        try:
            inputs["lora_config"] = np.load(
                os.path.join(lora_dir, "model.lora_config.npy"))
        except Exception:
            inputs["lora_config"] = np.load(
                os.path.join(lora_dir, "model.lora_keys.npy"))

        inputs["lora_task_id"] = np.array([[lora_task_id]], dtype=np.uint64)

    return inputs


def run_inference(triton_client,
                  prompt,
                  output_len,
                  request_id,
                  repetition_penalty,
                  presence_penalty,
                  frequency_penalty,
                  temperature,
                  stop_words,
                  bad_words,
                  embedding_bias_words,
                  embedding_bias_weights,
                  model_name,
                  streaming,
                  beam_width,
                  overwrite_output_text,
                  return_log_probs_data,
                  return_context_logits_data,
                  return_generation_logits_data,
                  end_id,
                  pad_id,
                  batch_inputs,
                  verbose,
                  num_draft_tokens=0,
                  use_draft_logits=None,
                  num_return_sequences=None,
                  lora_dir=None,
                  lora_task_id=None):

    try:
        prompts = json.loads(prompt)
    except:
        prompts = [prompt]

    bs1_inputs = []
    for prompt in prompts:
        bs1_inputs.append(
            prepare_inputs(prompt, output_len, repetition_penalty,
                           presence_penalty, frequency_penalty, temperature,
                           stop_words, bad_words, embedding_bias_words,
                           embedding_bias_weights, streaming, beam_width,
                           return_log_probs_data, return_context_logits_data,
                           return_generation_logits_data, end_id, pad_id,
                           num_draft_tokens, use_draft_logits,
                           num_return_sequences, lora_dir, lora_task_id))

    if batch_inputs:
        multiple_inputs = []
        for key in bs1_inputs[0].keys():
            stackable_values = [value[key] for value in bs1_inputs]
            stacked_values = np.concatenate(tuple(stackable_values), axis=0)
            multiple_inputs.append(prepare_tensor(key, stacked_values))
        multiple_inputs = [multiple_inputs]
    else:
        multiple_inputs = []
        for bs1_input in bs1_inputs:
            multiple_inputs.append([
                prepare_tensor(key, value)
                for (key, value) in bs1_input.items()
            ])

    if beam_width > 1 and FLAGS.check_outputs:
        raise Exception(
            "check_outputs flag only works with beam_width == 1 currently")

    #Only include needed outputs
    outputs = []
    outputs.append(grpcclient.InferRequestedOutput("text_output"))
    if return_log_probs_data is not None:
        outputs.append(grpcclient.InferRequestedOutput("cum_log_probs"))
        outputs.append(grpcclient.InferRequestedOutput("output_log_probs"))
    if return_context_logits_data is not None:
        outputs.append(grpcclient.InferRequestedOutput("context_logits"))
    if return_generation_logits_data is not None:
        outputs.append(grpcclient.InferRequestedOutput("generation_logits"))
    if num_return_sequences is not None:
        outputs.append(grpcclient.InferRequestedOutput("sequence_index"))
    if batch_inputs:
        outputs.append(grpcclient.InferRequestedOutput("batch_index"))

    output_texts = []
    user_data = UserData()
    for inputs in multiple_inputs:
        # Establish stream
        triton_client.start_stream(callback=partial(callback, user_data))

        # Send request
        batch_size = inputs[0].shape()[0]
        triton_client.async_stream_infer(model_name,
                                         inputs,
                                         outputs=outputs,
                                         request_id=request_id)

        #Wait for server to close the stream
        triton_client.stop_stream()

        # Parse the responses
        if num_return_sequences is None:
            num_generations = beam_width
        else:
            num_generations = num_return_sequences
            assert beam_width == 1

        batch_output_text = [[''] * num_generations for _ in range(batch_size)]
        while True:
            try:
                result = user_data._completed_requests.get(block=False)
            except Exception:
                break

            if type(result) == InferenceServerException:
                print("Received an error from server:")
                print(result)
            else:
                output = result.as_numpy('text_output')

                batch_index = result.as_numpy('batch_index')
                batch_index = 0 if batch_index is None else batch_index[0][0]

                seq_index = result.as_numpy('sequence_index')
                seq_index = 0 if seq_index is None else seq_index[0][0]

                if streaming and beam_width == 1:
                    if verbose and seq_index == 0:
                        print(batch_index, output, flush=True)
                    new_output = output[0].decode("utf-8")
                    if overwrite_output_text:
                        batch_output_text[batch_index][seq_index] = new_output
                    else:
                        batch_output_text[batch_index][seq_index] += new_output
                else:
                    output_text = output[0].decode("utf-8")
                    batch_output_text[batch_index][seq_index] = output_text
                    if verbose:
                        if num_generations > 1:
                            print(
                                f"{batch_index} [{seq_index}]: {output_text}",
                                flush=True)
                        else:
                            print(f"{batch_index}: {output_text}", flush=True)

                output_log_probs = result.as_numpy('output_log_probs')
                if output_log_probs is not None and verbose:
                    print(f"output_log_probs.shape: {output_log_probs.shape}")
                    print(f"output_log_probs: {output_log_probs}")

                context_logits = result.as_numpy('context_logits')
                if context_logits is not None and verbose:
                    print(f"context_logits.shape: {context_logits.shape}")
                    print(f"context_logits: {context_logits}")

                generation_logits = result.as_numpy("generation_logits")
                if generation_logits is not None and verbose:
                    print(
                        f"generation_logits.shape: {generation_logits.shape}")
                    print(f"generation_logits: {generation_logits}")

        if streaming and beam_width == 1:
            if verbose:
                for output_text in batch_output_text:
                    print('\n'.join(output_text))

        for output_text in batch_output_text:
            output_texts.extend(output_text)

    return output_texts


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-v',
                        '--verbose',
                        action="store_true",
                        required=False,
                        default=False,
                        help='Enable verbose output')
    parser.add_argument('-u',
                        '--url',
                        type=str,
                        required=False,
                        help='Inference server URL.')

    parser.add_argument(
        '--expected-outputs',
        type=str,
        required=False,
        help=
        'Expected outputs either a single string or a list of json encoded strings.'
    )

    parser.add_argument(
        '--check-outputs',
        action="store_true",
        required=False,
        default=False,
        help=
        'Boolean that indicates if outputs should be compared with expected outputs (passed via --expected-outputs)'
    )

    parser.add_argument(
        '-p',
        '--prompt',
        type=str,
        required=True,
        help=
        'Input prompt(s), either a single string or a list of json encoded strings.'
    )

    parser.add_argument('--model-name',
                        type=str,
                        required=False,
                        default="ensemble",
                        choices=["ensemble", "tensorrt_llm_bls"],
                        help='Name of the Triton model to send request to')

    parser.add_argument(
        "-S",
        "--streaming",
        action="store_true",
        required=False,
        default=False,
        help="Enable streaming mode. Default is False.",
    )

    parser.add_argument(
        "-n",
        "--num-return-sequences",
        type=int,
        required=False,
        default=None,
        help="Number of sequences to generate.",
    )

    parser.add_argument(
        "-b",
        "--beam-width",
        required=False,
        type=int,
        default=1,
        help="Beam width value",
    )

    parser.add_argument(
        "--temperature",
        type=float,
        required=False,
        default=1.0,
        help="temperature value",
    )

    parser.add_argument(
        "--repetition-penalty",
        type=float,
        required=False,
        default=None,
        help="The repetition penalty value",
    )

    parser.add_argument(
        "--presence-penalty",
        type=float,
        required=False,
        default=None,
        help="The presence penalty value",
    )

    parser.add_argument(
        "--frequency-penalty",
        type=float,
        required=False,
        default=None,
        help="The frequency penalty value",
    )

    parser.add_argument('-o',
                        '--output-len',
                        type=int,
                        default=100,
                        required=False,
                        help='Specify output length')

    parser.add_argument('--request-id',
                        type=str,
                        default='',
                        required=False,
                        help='The request_id for the stop request')

    parser.add_argument('--stop-words',
                        nargs='+',
                        default=[],
                        help='The stop words')

    parser.add_argument('--bad-words',
                        nargs='+',
                        default=[],
                        help='The bad words')

    parser.add_argument('--embedding-bias-words',
                        nargs='+',
                        default=[],
                        help='The biased words')

    parser.add_argument('--embedding-bias-weights',
                        nargs='+',
                        default=[],
                        help='The biased words weights')

    parser.add_argument(
        '--overwrite-output-text',
        action="store_true",
        required=False,
        default=False,
        help=
        'In streaming mode, overwrite previously received output text instead of appending to it'
    )

    parser.add_argument(
        "--return-log-probs",
        action="store_true",
        required=False,
        default=False,
        help="Return log probs",
    )

    parser.add_argument(
        "--return-context-logits",
        action="store_true",
        required=False,
        default=False,
        help=
        "Return context logits, the engine must be built with gather_context_logits or gather_all_token_logits",
    )

    parser.add_argument(
        "--return-generation-logits",
        action="store_true",
        required=False,
        default=False,
        help=
        "Return generation logits, the engine must be built with gather_ generation_logits or gather_all_token_logits",
    )

    parser.add_argument(
        '--batch-inputs',
        action="store_true",
        required=False,
        default=False,
        help='Whether inputs should be batched or processed individually.')

    parser.add_argument('--end-id',
                        type=int,
                        required=False,
                        help='The token id for end token.')

    parser.add_argument('--pad-id',
                        type=int,
                        required=False,
                        help='The token id for pad token.')

    parser.add_argument("--lora-path",
                        type=str,
                        required=False,
                        help="path to LoRA dir")
    parser.add_argument("--lora-task-id",
                        type=int,
                        required=False,
                        help="LoRA task ID")

    FLAGS = parser.parse_args()
    if FLAGS.url is None:
        FLAGS.url = "localhost:8001"

    embedding_bias_words = FLAGS.embedding_bias_words if FLAGS.embedding_bias_words else None
    embedding_bias_weights = FLAGS.embedding_bias_weights if FLAGS.embedding_bias_weights else None

    lora_dir = FLAGS.lora_path if FLAGS.lora_path else None
    lora_id = FLAGS.lora_task_id if FLAGS.lora_task_id else None

    try:
        client = grpcclient.InferenceServerClient(url=FLAGS.url)
    except Exception as e:
        print("client creation failed: " + str(e))
        sys.exit(1)

    return_log_probs_data = None
    if FLAGS.return_log_probs:
        return_log_probs_data = np.array([[FLAGS.return_log_probs]],
                                         dtype=bool)

    return_context_logits_data = None
    if FLAGS.return_context_logits:
        return_context_logits_data = np.array([[FLAGS.return_context_logits]],
                                              dtype=bool)

    return_generation_logits_data = None
    if FLAGS.return_generation_logits:
        return_generation_logits_data = np.array(
            [[FLAGS.return_generation_logits]], dtype=bool)

    output_texts = run_inference(
        client,
        FLAGS.prompt,
        FLAGS.output_len,
        FLAGS.request_id,
        FLAGS.repetition_penalty,
        FLAGS.presence_penalty,
        FLAGS.frequency_penalty,
        FLAGS.temperature,
        FLAGS.stop_words,
        FLAGS.bad_words,
        embedding_bias_words,
        embedding_bias_weights,
        FLAGS.model_name,
        FLAGS.streaming,
        FLAGS.beam_width,
        FLAGS.overwrite_output_text,
        return_log_probs_data,
        return_context_logits_data,
        return_generation_logits_data,
        FLAGS.end_id,
        FLAGS.pad_id,
        FLAGS.batch_inputs,
        True,
        num_return_sequences=FLAGS.num_return_sequences)

    if FLAGS.check_outputs:
        expected_outputs = json.loads(FLAGS.expected_outputs)
        n = FLAGS.num_return_sequences or FLAGS.beam_width
        assert len(expected_outputs) * n == len(output_texts)

        batched_output_texts = [
            output_texts[i:i + n] for i in range(0, len(output_texts), n)
        ]
        for out_texts, expected in zip(batched_output_texts, expected_outputs):
            assert all([out == expected for out in out_texts])
