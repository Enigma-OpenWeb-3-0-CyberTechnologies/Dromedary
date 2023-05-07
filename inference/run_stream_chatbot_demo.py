"""Chatbot Demo for Dromedary"""

import functools
from typing import Tuple
import os
import torch
import fire
import time
import json
import threading
import queue
from pathlib import Path

import gradio as gr

from fairscale.nn.model_parallel import initialize as mpu
from fairscale.nn.model_parallel.initialize import initialize_model_parallel

use_llama_dromedary = False
try:
    from llama_dromedary import ModelArgs, Transformer, Tokenizer, LLaMA
    use_llama_dromedary = True
except:
    from llama import ModelArgs, Transformer, Tokenizer, LLaMA


def setup_model_parallel() -> Tuple[int, int]:
    local_rank = int(os.environ.get("LOCAL_RANK", -1))
    global_rank = int(os.environ.get("RANK", -1))
    world_size = int(os.environ.get("WORLD_SIZE", -1))

    if world_size not in [1, 2, 4, 8, 16, 32] and not use_llama_dromedary:
        raise ValueError(
            "Only the following world sizes are supported: 1, 2, 4, 8, 16, 32 for the original llama code. "
            "For llama_dromedary, any world size is supported."
        )

    torch.distributed.init_process_group("nccl")
    initialize_model_parallel(world_size, pipeline_length=1)
    print("Model parallelism:", mpu.get_model_parallel_world_size())
    print("Global rank:", global_rank, "World size:", world_size)
    torch.cuda.set_device(local_rank)

    # seed must be the same in all processes
    torch.manual_seed(1)
    return global_rank, world_size


def load(
    ckpt_dir: str,
    tokenizer_path: str,
    global_rank: int,
    world_size: int,
    max_seq_len: int,
    max_batch_size: int,
    max_shared_seq_len: int,
) -> LLaMA:
    start_time = time.time()
    checkpoints = sorted(Path(ckpt_dir).glob("*.pth"))
    assert world_size == len(
        checkpoints
    ), f"Loading a checkpoint for MP={len(checkpoints)} but world size is {world_size}"
    ckpt_path = checkpoints[global_rank]
    print("Loading")
    checkpoint = torch.load(ckpt_path, map_location="cpu")
    with open(Path(ckpt_dir) / "params.json", "r") as f:
        params = json.loads(f.read())

    model_args: ModelArgs = ModelArgs(
        max_seq_len=max_seq_len, max_batch_size=max_batch_size, **params
    )
    tokenizer = Tokenizer(model_path=tokenizer_path)

    if use_llama_dromedary:
      model_args.vocab_size = tokenizer.n_words
      if model_args.qkv_dim != 0:
          print("Original n_heads:", model_args.n_heads)
          model_args.n_heads = (model_args.n_heads * model_args.qkv_dim) // model_args.dim
          print("New n_heads:", model_args.n_heads)
      model_args.max_shared_seq_len = max_shared_seq_len
      model_args.use_prefix_cache = True

    torch.set_default_tensor_type(torch.cuda.HalfTensor)
    model = Transformer(model_args)
    model.load_state_dict(checkpoint, strict=False)
    model.eval()
    model.half()

    generator = LLaMA(model, tokenizer)
    print(f"Loaded in {time.time() - start_time:.2f} seconds")
    return generator


title = """<h1 align="center">Dromedary (Self-Aligned LLaMa-65b)</h1>"""
description = """Gradio demo for Dromedary (Self-Align).
<br> <strong>Meta Prompt</strong>: Consider an AI assistant whose codename is Dromedary, developed by the Self-Align team. Dromedary is trained on data up until Sept-2021, and it endeavors to be a helpful, ethical and reliable assistant.
<br> <strong>Disclaimer</strong>: This is a research prototype and is not intended for production use. No data is collected."""


def main(
    ckpt_dir: str,
    tokenizer_path: str,
    max_seq_len: int = 512,
    max_batch_size: int = 32,
    max_shared_seq_len: int = 512,
    meta_prompt_file: str = "none",
    prompt_version: str = "dromedary",
):
    if meta_prompt_file != "none":
        with open(meta_prompt_file, "r") as f:
            data = f.readlines()
        meta_prompt = "".join(data)
        meta_prompt = meta_prompt.strip()
    else:
        raise ValueError("Unknown meta prompt file")

    if prompt_version == "dromedary":
        generate_prompt_fn = functools.partial(generate_prompt, meta_prompt=meta_prompt)
    else:
        raise ValueError("Unknown prompt version")

    global_rank, world_size = setup_model_parallel()

    t0 = time.time()
    generator = load(
        ckpt_dir, tokenizer_path, global_rank, world_size, max_seq_len, max_batch_size, max_shared_seq_len,
    )
    t1 = time.time()
    loading_time = t1-t0
    global_rank = torch.distributed.get_rank()
    print("Model loading time on %d: " % global_rank, loading_time)

    def evaluate(
        prompt,
        temperature=0.1,
        top_p=0.75,
        max_new_tokens=128,
        **kwargs,
    ):
        # sync the process with torch.distributed.barrier
        # torch.distributed.barrier()
        for i in range(world_size):
            if i != 0:
                torch.distributed.send(torch.tensor([1]), dst=i)

        # sync the prompt string across all processes, max_len=4096
        prompt_tensor = torch.zeros(4096, dtype=torch.long, device="cuda") + generator.tokenizer.pad_id
        tokenized_prompt = generator.tokenizer.encode(prompt, bos=True, eos=False)
        prompt_tensor[:len(tokenized_prompt)] = torch.tensor(tokenized_prompt, dtype=torch.long, device="cuda")
        torch.distributed.broadcast(prompt_tensor, 0)
        t = prompt_tensor.tolist()
        try:
            t = t[: t.index(generator.tokenizer.pad_id)]
        except ValueError:
            pass
        prompt = generator.tokenizer.decode(t)

        temperature_tensor = torch.tensor([temperature], dtype=torch.float, device="cuda")
        torch.distributed.broadcast(temperature_tensor, 0)

        top_p_tensor = torch.tensor([top_p], dtype=torch.float, device="cuda")
        torch.distributed.broadcast(top_p_tensor, 0)

        max_new_tokens_tensor = torch.tensor([max_new_tokens], dtype=torch.long, device="cuda")
        torch.distributed.broadcast(max_new_tokens_tensor, 0)

        def generate_output(prompt, max_gen_len, temperature, top_p, quadtoken_frequency_penalty, stream_queue):
            output = generator.generate(
                [prompt],
                max_gen_len=max_gen_len,
                temperature=temperature,
                top_p=top_p,
                stop="### User",
                quadtoken_frequency_penalty=quadtoken_frequency_penalty,
                stream_queue=stream_queue,
            )[0]

        stream_queue = queue.Queue()
        generate_thread = threading.Thread(target=generate_output, args=(
            prompt, max_new_tokens_tensor[0], temperature_tensor[0], top_p_tensor[0], 1.0, stream_queue))
        generate_thread.start()

        while True:
            words = stream_queue.get()
            if words is None:
                break
            output = generator.tokenizer.decode(words[0])
            output.split("### User")[0].strip()

            if output.endswith("\n\n###") or output.endswith("\n\n##") or output.endswith("\n\n#"):
                output = output.rsplit("\n\n", 1)[0].strip()
            yield output

    def run_fake_evaluate():
        while True:
            prompt = "Fake prompt"
            # sync the process with torch.distributed.barrier
            # TODO(zhiqings): find a better way to sync the processes, and avoid timeout in barrier
            # torch.distributed.barrier()
            fake_tensor = torch.zeros(1, dtype=torch.long, device="cuda")
            torch.distributed.recv(fake_tensor, src=0)

            # sync the prompt string across all processes
            prompt_tensor = torch.zeros(4096, dtype=torch.long, device="cuda") + generator.tokenizer.pad_id
            tokenized_prompt = generator.tokenizer.encode(prompt, bos=True, eos=False)
            prompt_tensor[:len(tokenized_prompt)] = torch.tensor(tokenized_prompt, dtype=torch.long, device="cuda")
            torch.distributed.broadcast(prompt_tensor, 0)
            t = prompt_tensor.tolist()
            try:
                t = t[: t.index(generator.tokenizer.pad_id)]
            except ValueError:
                pass
            prompt = generator.tokenizer.decode(t)

            temperature_tensor = torch.tensor([0.0], dtype=torch.float, device="cuda")
            torch.distributed.broadcast(temperature_tensor, 0)

            top_p_tensor = torch.tensor([0.0], dtype=torch.float, device="cuda")
            torch.distributed.broadcast(top_p_tensor, 0)

            max_new_tokens_tensor = torch.tensor([0], dtype=torch.long, device="cuda")
            torch.distributed.broadcast(max_new_tokens_tensor, 0)

            # time.sleep(0.1 * global_rank)
            output = generator.generate(
                [prompt],
                max_gen_len=max_new_tokens_tensor[0],
                temperature=temperature_tensor[0],
                top_p=top_p_tensor[0],
                stop="### User",
                quadtoken_frequency_penalty=1.0,
            )[0]

    if global_rank != 0:
        run_fake_evaluate()

    def inference_chat(
        history,
        chat,
        temperature,
        top_p,
        max_new_tokens,
        history_length=10,
    ):
        if len(history) > history_length * 2:
            history = history[-history_length * 2:]

        if len(history) == 0:
            print("No history, probably a heart beat.")
            history = ["Hello", None]
        del history[-1]

        # history should be prompted by "\n### User\n" and "\n### Watson\n" in an interleaved manner.
        prompted_history = []
        for i in range(len(history)):
            if i % 2 == 0:
                if i > 0:
                    prompted_history.append("\n\n### User\n")
            else:
                prompted_history.append("\n\n### Dromedary\n")
            prompted_history.append(history[i])
        prompted_history = "".join(prompted_history)
        prompted_history = generate_prompt_fn(prompted_history)
        print("Prompt:")
        print(prompted_history)
        history.append(None)

        for output in evaluate(
            prompted_history,
            temperature=temperature,
            top_p=top_p,
            max_new_tokens=max_new_tokens,
        ):
            history[-1] = output
            chat = [
                (history[i], history[i + 1]) for i in range(0, len(history) - 1, 2)
            ]  # convert to tuples of list
            yield [chat, history]
        print("Output:")
        print(output)
        print("="*20)

    def user(user_message, history, chat):
        del chat
        new_user_message = ""
        new_history = history + [user_message, None]
        new_chat = [
            (history[i], history[i + 1]) for i in range(0, len(history) - 1, 2)
        ]
        return new_user_message, new_history, new_chat

    # run the demo
    with gr.Blocks(
        css="""
        .message.svelte-w6rprc.svelte-w6rprc.svelte-w6rprc {font-size: 20px; margin-top: 20px}
        #component-21 > div.wrap.svelte-w6rprc {height: 600px;}
        """
    ) as iface:
        state = gr.State([])

        gr.Markdown(title)
        gr.Markdown(description)

        with gr.Row():
            with gr.Column(scale=1):
                temperature = gr.Slider(
                    minimum=0.0,
                    maximum=2.0,
                    value=0.5,
                    step=0.1,
                    interactive=True,
                    label="Temperature",
                )

                top_p = gr.Slider(
                    minimum=0.0,
                    maximum=1.0,
                    value=0.9,
                    step=0.05,
                    interactive=True,
                    label="Top p",
                )

                max_new_tokens = gr.Slider(
                    minimum=16,
                    maximum=512,
                    value=384,
                    step=1,
                    interactive=True,
                    label="Max new tokens",
                )

            with gr.Column(scale=1.8):
                with gr.Row():
                    with gr.Column(
                        scale=1.5,
                    ):
                        chatbot = gr.Chatbot(
                            label="Chat History",
                        )

                    with gr.Column(scale=1):
                        chat_input = gr.Textbox(
                            lines=2,
                            label="User Input")

                        with gr.Row():
                            clear_button = gr.Button(value="Clear", interactive=True)
                            clear_button.click(
                                lambda: ([], []),
                                None,
                                [chatbot, state],
                                queue=False,
                            )

                            submit_button = gr.Button(
                                value="Submit", interactive=True, variant="primary",
                            )
                            submit_button.click(
                                user,
                                [
                                    chat_input,
                                    state,
                                    chatbot,
                                ],
                                [
                                    chat_input,
                                    state,
                                    chatbot
                                ],
                                queue=True,
                                api_name="predict",
                            ).then(
                                inference_chat,
                                [
                                    state,
                                    chatbot,
                                    temperature,
                                    top_p,
                                    max_new_tokens,
                                ],
                                [chatbot, state],
                            )

        examples = [
            ["Tell me about llama."],
            ["Tell me about alpaca."],
            ["Who is the president of US in 2019?"],
            ["Who is the president of US in 2021?"],
            ["Who is the president of US in 2025?"],
        ]

        examples = gr.Examples(
            examples=examples,
            inputs=[chat_input],
        )

    iface.queue(concurrency_count=1, api_open=False, max_size=10)
    app, _, _ = iface.launch(share=True)


def generate_prompt(instruction, input=None, meta_prompt=""):
    if input:
        return f"""{meta_prompt}
{instruction}

{input}

### Dromedary
"""
    else:
        return f"""{meta_prompt}
{instruction}

### Dromedary
"""


if __name__ == "__main__":
    fire.Fire(main)
