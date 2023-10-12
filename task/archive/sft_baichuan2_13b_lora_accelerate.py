"""
lora 微调的代码
"""
# You can also adapt this script on your own causal language modeling task. Pointers for this are left as comments.

import argparse
import gc
import json
import logging
import math
import os
import random
import threading
from itertools import chain

import bitsandbytes as bnb
import datasets
import psutil
import torch
from accelerate import Accelerator, DistributedType
from accelerate.logging import get_logger
from accelerate.utils import DummyOptim, DummyScheduler, set_seed
from datasets import load_dataset
from peft import AdaLoraConfig, LoraConfig, TaskType, get_peft_model, prepare_model_for_kbit_training
from torch.utils.data import DataLoader
from tqdm.auto import tqdm

import transformers
from transformers import (
    CONFIG_MAPPING,
    MODEL_MAPPING,
    AutoConfig,
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    SchedulerType,
    default_data_collator,
    get_scheduler,
)
from transformers.utils import (
    check_min_version,
    send_example_telemetry,
)

# WLoraConfioill error if the minimal version of Transformers is not installed. Remove at your own risks.
check_min_version("4.32.2")

logger = get_logger(__name__)

MODEL_CONFIG_CLASSES = list(MODEL_MAPPING.keys())
MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)


def parse_args():
    parser = argparse.ArgumentParser(description="Finetune a transformers model on a causal language modeling task")
    parser.add_argument(
        "--dataset_name",
        type=str,
        default=None,
        help="The name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--dataset_config_name",
        type=str,
        default=None,
        help="The configuration name of the dataset to use (via the datasets library).",
    )
    parser.add_argument(
        "--train_file",
        type=str,
        default=None,
        help="A csv or a json file containing the training data.",
    )
    parser.add_argument(
        "--validation_file",
        type=str,
        default=None,
        help="A csv or a json file containing the validation data.",
    )
    parser.add_argument(
        "--validation_split_percentage",
        default=5,
        help="The percentage of the train set used as validation set in case there's no validation split",
    )
    parser.add_argument(
        "--model_name_or_path",
        type=str,
        help="Path to pretrained model or model identifier from huggingface.co/models.",
        required=False,
    )
    parser.add_argument(
        "--config_name",
        type=str,
        default=None,
        help="Pretrained config name or path if not the same as model_name",
    )
    parser.add_argument(
        "--tokenizer_name",
        type=str,
        default=None,
        help="Pretrained tokenizer name or path if not the same as model_name",
    )

    parser.add_argument(
        "--use_slow_tokenizer",
        action="store_true",
        help="If passed, will use a slow tokenizer (not backed by the �� Tokenizers library).",
    )
    parser.add_argument(
        "--per_device_train_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the training dataloader.",
    )
    parser.add_argument(
        "--per_device_eval_batch_size",
        type=int,
        default=8,
        help="Batch size (per device) for the evaluation dataloader.",
    )
    parser.add_argument(
        "--learning_rate",
        type=float,
        default=5e-5,
        help="Initial learning rate (after the potential warmup period) to use.",
    )
    parser.add_argument("--weight_decay", type=float, default=0.0, help="Weight decay to use.")
    parser.add_argument(
        "--num_train_epochs",
        type=int,
        default=3,
        help="Total number of training epochs to perform.",
    )
    parser.add_argument(
        "--max_train_steps",
        type=int,
        default=None,
        help="Total number of training steps to perform. If provided, overrides num_train_epochs.",
    )
    parser.add_argument(
        "--gradient_accumulation_steps",
        type=int,
        default=1,
        help="Number of updates steps to accumulate before performing a backward/update pass.",
    )
    parser.add_argument(
        "--lr_scheduler_type",
        type=SchedulerType,
        default="linear",
        help="The scheduler type to use.",
        choices=[
            "linear",
            "cosine",
            "cosine_with_restarts",
            "polynomial",
            "constant",
            "constant_with_warmup",
        ],
    )
    parser.add_argument(
        "--num_warmup_steps",
        type=int,
        default=0,
        help="Number of steps for the warmup in the lr scheduler.",
    )
    parser.add_argument("--output_dir", type=str, default=None, help="Where to store the final model.")
    parser.add_argument("--seed", type=int, default=None, help="A seed for reproducible training.")
    parser.add_argument(
        "--model_type",
        type=str,
        default=None,
        help="Model type to use if training from scratch.",
        choices=MODEL_TYPES,
    )
    parser.add_argument(
        "--block_size",
        type=int,
        default=None,
        help=(
            "Optional input sequence length after tokenization. The training dataset will be truncated in block of"
            " this size for training. Default to the model max input length for single sentence inputs (take into"
            " account special tokens)."
        ),
    )
    parser.add_argument(
        "--preprocessing_num_workers",
        type=int,
        default=None,
        help="The number of processes to use for the preprocessing.",
    )
    parser.add_argument(
        "--overwrite_cache",
        action="store_true",
        help="Overwrite the cached training and evaluation sets",
    )
    parser.add_argument(
        "--no_keep_linebreaks",
        action="store_true",
        help="Do not keep line breaks when using TXT files.",
    )
    parser.add_argument(
        "--checkpointing_steps",
        type=str,
        default=None,
        help="Whether the various states should be saved at the end of every n steps, or 'epoch' for each epoch.",
    )
    parser.add_argument(
        "--resume_from_checkpoint",
        type=str,
        default=None,
        help="If the training should continue from a checkpoint folder.",
    )
    parser.add_argument(
        "--with_tracking",
        action="store_true",
        help="Whether to enable experiment trackers for logging.",
    )
    parser.add_argument(
        "--report_to",
        type=str,
        default="tensorboard",
        help=(
            'The integration to report the results and logs to. Supported platforms are `"tensorboard"`,'
            ' `"wandb"`, `"comet_ml"` and `"clearml"`. Use `"all"` (default) to report to all integrations.'
            "Only applicable when `--with_tracking` is passed."
        ),
    )
    parser.add_argument(
        "--low_cpu_mem_usage",
        action="store_true",
        help=(
            "It is an option to create the model as an empty shell, then only materialize its parameters when the pretrained weights are loaded."
            "If passed, LLM loading time and RAM consumption will be benefited."
        ),
    )
    parser.add_argument("--max_source_length", type=int, default=1024, help="")

    parser.add_argument(
        "--max_target_length",
        type=int,
        default=128,
        help=(
            "The maximum total sequence length for target text after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."
        ),
    )

    parser.add_argument(
        "--ignore_pad_token_for_loss",
        type=bool,
        default=True,
        help="Whether to ignore the tokens corresponding to padded labels in the loss computation or not.",
    )

    parser.add_argument("--quantization_bit", type=str, default=None, help="quantization training")
    args = parser.parse_args()
    # Sanity checks

    if args.dataset_name is None and args.train_file is None and args.validation_file is None:
        raise ValueError("Need either a dataset name or a training/validation file.")
    else:
        if args.train_file is not None:
            extension = args.train_file.split(".")[-1]
            assert extension in ["csv", "json", "txt"], "`train_file` should be a csv, json or txt file."
        if args.validation_file is not None:
            extension = args.validation_file.split(".")[-1]
            assert extension in [
                "csv",
                "json",
                "txt",
            ], "`validation_file` should be a csv, json or txt file."

    return args


def main():
    args = parse_args()
    # Sending telemetry. Tracking the example usage helps us better allocate resources to maintain them. The
    # information sent is the one passed as arguments along with your Python/PyTorch versions.
    send_example_telemetry("run_clm_no_trainer", args)
    # Initialize the accelerator. We will let the accelerator handle device placement for us in this example.
    # If we're using tracking, we also need to initialize it here and it will by default pick up all supported trackers
    # in the environment

    accelerator_log_kwargs = {}
    if args.with_tracking:
        if args.report_to == "tensorboard":
            from accelerate.tracking import TensorBoardTracker

            tensorboard = TensorBoardTracker(run_name="baichuan", logging_dir=args.output_dir)

            accelerator_log_kwargs["log_with"] = tensorboard
        else:
            accelerator_log_kwargs["log_with"] = args.report_to

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        **accelerator_log_kwargs,
    )

    # Make one log on every process with the configuration for debugging.
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )

    logger.info(accelerator.state, main_process_only=False)
    if accelerator.is_local_main_process:
        datasets.utils.logging.set_verbosity_warning()
        transformers.utils.logging.set_verbosity_info()
    else:
        datasets.utils.logging.set_verbosity_error()
        transformers.utils.logging.set_verbosity_error()
    # If passed along, set the training seed now.
    if args.seed is not None:
        set_seed(args.seed)

    if accelerator.is_main_process:
        if args.output_dir is not None:
            os.makedirs(args.output_dir, exist_ok=True)

    accelerator.wait_for_everyone()

    # Get the datasets: you can either provide your own CSV/JSON/TXT training and evaluation files (see below)
    # or just provide the name of one of the public datasets available on the hub at https://huggingface.co/datasets/
    # (the dataset will be downloaded automatically from the datasets Hub).
    #
    # For CSV/JSON files, this script will use the column called 'text' or the first column if no column called
    # 'text' is found. You can easily tweak this behavior (see below).
    #
    # In distributed training, the load_dataset function guarantee that only one local process can concurrently
    # download the dataset.

    if args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.

        raw_datasets = load_dataset(args.dataset_name, args.dataset_config_name)
        if "validation" not in raw_datasets.keys():
            raw_datasets["validation"] = load_dataset(
                args.dataset_name,
                args.dataset_config_name,
                split=f"train[:{args.validation_split_percentage}%]",
            )
            raw_datasets["train"] = load_dataset(
                args.dataset_name,
                args.dataset_config_name,
                split=f"train[{args.validation_split_percentage}%:]",
            )
    else:
        data_files = {}
        dataset_args = {}
        if args.train_file is not None:
            data_files["train"] = args.train_file
        if args.validation_file is not None:
            data_files["validation"] = args.validation_file

        extension = args.train_file.split(".")[-1]
        if extension == "txt":
            extension = "text"
            dataset_args["keep_linebreaks"] = not args.no_keep_linebreaks

        raw_datasets = load_dataset(extension, data_files=data_files, **dataset_args)
        # If no validation data is there, validation_split_percentage will be used to divide the dataset.

        if "validation" not in raw_datasets.keys():
            raw_datasets["validation"] = load_dataset(
                extension,
                data_files=data_files,
                split=f"train[:{args.validation_split_percentage}%]",
                **dataset_args,
            )
            raw_datasets["train"] = load_dataset(
                extension,
                data_files=data_files,
                split=f"train[{args.validation_split_percentage}%:]",
                **dataset_args,
            )

    # See more about loading any type of standard or custom dataset (from files, python dict, pandas DataFrame, etc) at
    # https://huggingface.co/docs/datasets/loading_datasets.html.
    # Load pretrained model and tokenizer
    #
    # In distributed training, the .from_pretrained methods guarantee that only one local process can concurrently
    # download model & vocab.

    if args.config_name:
        config = AutoConfig.from_pretrained(args.config_name)
    elif args.model_name_or_path:
        config = AutoConfig.from_pretrained(args.model_name_or_path, trust_remote_code=True)
    else:
        config = CONFIG_MAPPING[args.model_type]()
        logger.warning("You are instantiating a new config instance from scratch.")
    if args.tokenizer_name:
        tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_name, use_fast=not args.use_slow_tokenizer)
    elif args.model_name_or_path:
        tokenizer = AutoTokenizer.from_pretrained(
            args.model_name_or_path, use_fast=not args.use_slow_tokenizer, trust_remote_code=True
        )
    else:
        raise ValueError(
            "You are instantiating a new tokenizer from scratch. This is not supported by this script."
            "You can do it from another script, save it, and load it from here, using --tokenizer_name."
        )

    # if args.model_name_or_path:
    #     model = AutoModelForCausalLM.from_pretrained(
    #         args.model_name_or_path,
    #         from_tf=bool(".ckpt" in args.model_name_or_path),
    #         config=config,
    #         low_cpu_mem_usage=args.low_cpu_mem_usage,
    #         trust_remote_code=True,
    #         device_map="auto",
    #     )
    # else:
    #     logger.info("Training new model from scratch")
    #     model = AutoModelForCausalLM.from_config(config)
    # We resize the embeddings only when necessary to avoid index errors. If you are creating a model from scratch
    # on a small vocab and want a smaller embedding size, remove this test.

    if args.quantization_bit is not None:
        print(f"Quantized to {args.quantization_bit}")
        if args.quantization_bit == "4bit":
            quantization_config = BitsAndBytesConfig(
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
                bnb_4bit_quant_type="nf4",
            )
        elif args.quantization_bit == "8bit":
            quantization_config = BitsAndBytesConfig(load_in_8bit=True)
        else:
            raise ValueError("unsupport quantization_bit")

        model = AutoModelForCausalLM.from_pretrained(
            args.model_name_or_path,
            from_tf=bool(".ckpt" in args.model_name_or_path),
            config=config,
            quantization_config=quantization_config,
            low_cpu_mem_usage=True,
            trust_remote_code=True,
        )

    else:
        model = AutoModelForCausalLM.from_pretrained(
            args.model_name_or_path,
            config=config,
            from_tf=bool(".ckpt" in args.model_name_or_path),
            trust_remote_code=True,
        )

    # accelerator.print(model)
    embedding_size = model.get_input_embeddings().weight.shape[0]

    logger.info(f"**embedding_size ={embedding_size},len tokenizer={len(tokenizer)}")

    # if len(tokenizer) > embedding_size:
    #     model.resize_token_embeddings(len(tokenizer))

    # model setting and lora config
    # model.supports_gradient_checkpointing = True  #
    # model.gradient_checkpointing_enable()
    # model.enable_input_require_grads()

    model.config.use_cache = False  # silence the warnings. Please re-enable for inference!the datasets

    # find all linear layer for lora adapter
    # def find_all_linear_names(model):
    #     """
    #     找出所有全连接层，为所有全连接添加adapter
    #     """
    #     cls = torch.nn.Linear
    #     lora_module_names = set()
    #     for name, module in model.named_modules():
    #         if isinstance(module, cls):
    #             names = name.split('.')
    #             lora_module_names.add(names[0] if len(names) == 1 else names[-1])

    #     if 'lm_head' in lora_module_names:
    #         # needed for 16-BitsAndBytesConfig
    #         lora_module_names.remove('lm_head')

    #     return list(lora_module_names)

    if args.quantization_bit is not None:
        # 启用模型量化需要开启
        model = prepare_model_for_kbit_training(model)

    # elif accelerator.state.deepspeed_plugin is None:
    #     # 不使用deepspeed
    #     accelerator.print("no quantization_bit using fp16")
    #     model = model.half()

    # lora_modules = find_all_linear_names(model)
    # model = model.half()

    # if args.quantization_bit is not None:
    #     print(f"Quantized to {args.quantization_bit} bit")

    peft_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        target_modules=["W_pack"],
        inference_mode=False,
        r=16,
        lora_alpha=16,
        lora_dropout=0.05,
    )
    # adalora 会出现 https://github.com/huggingface/peft/issues/479

    model = get_peft_model(model, peft_config)

    model.is_parallelizable = True
    model.model_parallel = True
    model.print_trainable_parameters()

    # logger.info("using fp16")
    # model = model.half()

    def preprocessing_function_train(examples):
        max_seq_length = args.max_source_length + args.max_target_length + 1
        # 添加EOS

        model_inputs = {
            "input_ids": [],
            "labels": [],
        }

        # __import__('pdb').set_trace()
        for i in range(len(examples["input"])):
            if examples["input"][i] and examples["output"][i] and examples["instruction"]:
                inputs, outputs, instruction = examples["input"][i], examples["output"][i], examples["instruction"][i]
                outputs = str(outputs)
                prompt = instruction + inputs + " ->"

                a_ids = tokenizer.encode(
                    text=prompt, add_special_tokens=True, truncation=True, max_length=args.max_source_length
                )
                b_ids = tokenizer.encode(
                    text=outputs, add_special_tokens=False, truncation=True, max_length=args.max_target_length
                )

                context_length = len(a_ids)
                input_ids = a_ids + b_ids + [model.generation_config.eos_token_id]
                # print(f"===={model.generation_config.pad_token_id}")
                labels = (
                    [model.generation_config.pad_token_id] * context_length
                    + b_ids
                    + [model.generation_config.eos_token_id]
                )
                # 构建 batch padding
                pad_len = max_seq_length - len(input_ids)
                input_ids = input_ids + [model.generation_config.pad_token_id] * pad_len
                labels = labels + [model.generation_config.pad_token_id] * pad_len
                if args.ignore_pad_token_for_loss:
                    labels = [(l if l != model.generation_config.pad_token_id else -100) for l in labels]

                model_inputs["input_ids"].append(input_ids)
                model_inputs["labels"].append(labels)

        return model_inputs

    def preprocessing_function_eval(examples):
        sources, targets = [], []
        for i in range(len(examples["input"])):
            if examples["input"][i] and examples["output"][i] and examples["instruction"][i]:
                inputs = examples["input"][i]
                instruction = examples["instruction"][i]
                inputs = instruction + inputs + "->"
                sources.append(inputs)
                target = str(examples["output"][i])  # 需要将字典类型转化为字符串类型
                targets.append(target)

        tokenizer.pad_token_id = model.generation_config.pad_token_id
        model_inputs = tokenizer(
            sources,
            max_length=args.max_source_length,
            truncation=True,
            padding=True,
        )
        #
        labels = tokenizer(targets, max_length=args.max_target_length, truncation=True, padding=True)
        if args.ignore_pad_token_for_loss:
            labels["input_ids"] = [
                [(l if l != model.generation_config.pad_token_id else -100) for l in label]
                for label in labels["input_ids"]
            ]

        model_inputs["labels"] = labels["input_ids"]

        return model_inputs

    column_names = raw_datasets["train"].column_names

    with accelerator.main_process_first():
        train_dataset = raw_datasets["train"].map(
            preprocessing_function_train,
            batched=True,
            num_proc=args.preprocessing_num_workers,
            remove_columns=column_names,
            load_from_cache_file=not args.overwrite_cache,
            desc="Running tokenizer on train dataset",
        )

    with accelerator.main_process_first():
        eval_dataset = raw_datasets["validation"].map(
            preprocessing_function_train,
            batched=True,
            num_proc=args.preprocessing_num_workers,
            remove_columns=column_names,
            load_from_cache_file=not args.overwrite_cache,
            desc="Running tokenizer on validation dataset",
        )

    # Log a few random samples from the training set:
    # for index in random.sample(range(len(train_dataset)), 1):
    #     logger.info(f"Sample {index} of the training set: {train_dataset[index]}.")
    # DataLoaders creation:

    train_dataloader = DataLoader(
        train_dataset,
        shuffle=True,
        collate_fn=default_data_collator,
        batch_size=args.per_device_train_batch_size,
    )
    eval_dataloader = DataLoader(
        eval_dataset,
        collate_fn=default_data_collator,
        batch_size=args.per_device_eval_batch_size,
    )

    # Optimizer
    # Split weights in two groups, one with weight decay and the other not.
    no_decay = ["bias", "layer_norm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
            "weight_decay": 0.0,
        },
    ]

    # New Code #
    # Creates Dummy Optimizer if `optimizer` was specified in the config file else creates Adam optimizer

    optimizer_cls = (
        torch.optim.AdamW
        if accelerator.state.deepspeed_plugin is None
        or "optimizer" not in accelerator.state.deepspeed_plugin.deepspeed_config
        else DummyOptim
    )

    optimizer = optimizer_cls(optimizer_grouped_parameters, lr=args.learning_rate)

    # # On TPU, the tie weights in our model have been disconnected, so we need to restore the ties.
    # if accelerator.distributed_type == DistributedType.TPU:
    #     model.tie_weights()
    # Scheduler and math around the number of training steps.

    if accelerator.state.deepspeed_plugin is not None:
        args.gradient_accumulation_steps = accelerator.state.deepspeed_plugin.deepspeed_config[
            "gradient_accumulation_steps"
        ]

    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    overrode_max_train_steps = False
    if args.max_train_steps is None:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch
        overrode_max_train_steps = True
    else:
        args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

        # New Code #
        # Creates Dummy Scheduler if `scheduler` was specified in the config file else creates `args.lr_scheduler_type` Scheduler
    if (
        accelerator.state.deepspeed_plugin is None
        or "scheduler" not in accelerator.state.deepspeed_plugin.deepspeed_config
    ):
        lr_scheduler = get_scheduler(
            name=args.lr_scheduler_type,
            optimizer=optimizer,
            num_warmup_steps=args.num_warmup_steps,
            num_training_steps=args.max_train_steps,
        )
    else:
        lr_scheduler = DummyScheduler(
            optimizer, total_num_steps=args.max_train_steps, warmup_num_steps=args.num_warmup_steps
        )

    # Prepare everything with our `accelerator`.
    (
        model,
        optimizer,
        train_dataloader,
        eval_dataloader,
        lr_scheduler,
    ) = accelerator.prepare(model, optimizer, train_dataloader, eval_dataloader, lr_scheduler)

    accelerator.print(model)

    for name, paramas in model.named_parameters():
        accelerator.print(f"{name}==torch type={paramas.dtype}")

    # We need to recalculate our total training steps as the size of the training dataloader may have changed.
    num_update_steps_per_epoch = math.ceil(len(train_dataloader) / args.gradient_accumulation_steps)
    if overrode_max_train_steps:
        args.max_train_steps = args.num_train_epochs * num_update_steps_per_epoch

    # Afterwards we recalculate our number of training epochs
    args.num_train_epochs = math.ceil(args.max_train_steps / num_update_steps_per_epoch)

    # Figure out how many steps we should save the Accelerator states
    checkpointing_steps = args.checkpointing_steps
    if checkpointing_steps is not None and checkpointing_steps.isdigit():
        checkpointing_steps = int(checkpointing_steps)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if args.with_tracking:
        experiment_config = vars(args)
        # TensorBoard cannot log Enums, need the raw value
        experiment_config["lr_scheduler_type"] = experiment_config["lr_scheduler_type"].value

        run = os.path.split(__file__)[-1].split(".")[0]
        accelerator.init_trackers(run, experiment_config)

    # Train!
    total_batch_size = args.per_device_train_batch_size * accelerator.num_processes * args.gradient_accumulation_steps

    logger.info("***** Running training *****")
    logger.info(f"  Num examples = {len(train_dataset)}")
    logger.info(f"  Num Epochs = {args.num_train_epochs}")
    logger.info(f"  Instantaneous batch size per device = {args.per_device_train_batch_size}")
    logger.info(f"  Total train batch size (w. parallel, distributed & accumulation) = {total_batch_size}")
    logger.info(f"  Gradient Accumulation steps = {args.gradient_accumulation_steps}")
    logger.info(f"  Total optimization steps = {args.max_train_steps}")

    # Only show the progress bar once on each machine.
    progress_bar = tqdm(range(args.max_train_steps), disable=not accelerator.is_local_main_process)
    completed_steps = 0
    starting_epoch = 0
    # Potentially load in the weights and states from a previous save
    if args.resume_from_checkpoint:
        if args.resume_from_checkpoint is not None or args.resume_from_checkpoint != "":
            accelerator.print(f"Resumed from checkpoint: {args.resume_from_checkpoint}")
            accelerator.load_state(args.resume_from_checkpoint)
            path = os.path.basename(args.resume_from_checkpoint)
        else:
            # Get the most recent checkpoint
            dirs = [f.name for f in os.scandir(os.getcwd()) if f.is_dir()]
            dirs.sort(key=os.path.getctime)
            path = dirs[-1]  # Sorts folders by date modified, most recent checkpoint is the last
        # Extract `epoch_{i}` or `step_{i}`
        training_difference = os.path.splitext(path)[0]
        if "epoch" in training_difference:
            starting_epoch = int(training_difference.replace("epoch_", "")) + 1
            resume_step = None
        else:
            # need to multiply `gradient_accumulation_steps` to reflect real steps
            resume_step = int(training_difference.replace("step_", "")) * args.gradient_accumulation_steps
            starting_epoch = resume_step // len(train_dataloader)
            resume_step -= starting_epoch * len(train_dataloader)

    # update the progress_bar if load from checkpoint

    progress_bar.update(starting_epoch * num_update_steps_per_epoch)
    completed_steps = starting_epoch * num_update_steps_per_epoch

    for epoch in range(starting_epoch, args.num_train_epochs):
        # with TorchTracemalloc() as tracemalloc:
        model.train()
        if args.with_tracking:
            total_loss = 0
        for step, batch in enumerate(train_dataloader):
            # We need to skip steps until we reach the resumed step
            if args.resume_from_checkpoint and epoch == starting_epoch:
                if resume_step is not None and step < resume_step:
                    if step % args.gradient_accumulation_steps == 0:
                        progress_bar.update(1)
                        completed_steps += 1
                    continue

            with accelerator.accumulate(model):
                outputs = model(**batch)
                loss = outputs.loss
                # We keep track of the loss at each epoch

                if args.with_tracking:
                    total_loss += loss.detach().float()
                accelerator.backward(loss)
                optimizer.step()
                lr_scheduler.step()
                optimizer.zero_grad()

                if args.with_tracking:
                    accelerator.log({"train_loss": loss}, step=step)

            # Checks if the accelerator has performed an optimization step behind the scenes
            if accelerator.sync_gradients:
                progress_bar.update(1)
                completed_steps += 1

            if isinstance(checkpointing_steps, int):
                if completed_steps % checkpointing_steps == 0:
                    output_dir = f"step_{completed_steps }"
                    if args.output_dir is not None:
                        output_dir = os.path.join(args.output_dir, output_dir)
                    accelerator.save_state(output_dir)
            if completed_steps >= args.max_train_steps:
                break

        # Printing the GPU memory usage details such as allocated memory, peak memory, and total memory usage
        # accelerator.print("GPU Memory before entering the train : {}".format(b2mb(tracemalloc.begin)))
        # accelerator.print("GPU Memory consumed at the end of the train (end-begin): {}".format(tracemalloc.used))
        # accelerator.print("GPU Peak Memory consumed during the train (max-begin): {}".format(tracemalloc.peaked))
        # accelerator.print(
        #     "GPU Total Peak Memory consumed during the train (max): {}".format(
        #         tracemalloc.peaked + b2mb(tracemalloc.begin)
        #     )

        # 每个epoch 验证一次
        model.eval()
        losses = []
        for step, batch in enumerate(eval_dataloader):
            # __import__("ipdb").set_trace()
            with torch.no_grad():
                outputs = model(**batch)
            loss = outputs.loss
            losses.append(accelerator.gather_for_metrics(loss.repeat(args.per_device_eval_batch_size)))

        losses = torch.cat(losses)
        try:
            eval_loss = torch.mean(losses)
            perplexity = math.exp(eval_loss)
        except OverflowError:
            perplexity = float("inf")
        logger.info(f"epoch {epoch}: perplexity: {perplexity} eval_loss: {eval_loss}")

        if args.with_tracking:
            accelerator.log(
                {
                    "perplexity": perplexity,
                    "eval_loss": eval_loss,
                    "train_loss": total_loss.item() / len(train_dataloader),
                    "epoch": epoch,
                    "step": completed_steps,
                },
                step=completed_steps,
            )

        if args.checkpointing_steps == "epoch":
            output_dir = f"epoch_{epoch}"
            if args.output_dir is not None:
                output_dir = os.path.join(args.output_dir, output_dir)
            accelerator.save_state(output_dir)

    if args.with_tracking:
        accelerator.end_training()

    if args.output_dir is not None:
        accelerator.wait_for_everyone()
        unwrapped_model = accelerator.unwrap_model(model)
        unwrapped_model.save_pretrained(
            args.output_dir,
            is_main_process=accelerator.is_main_process,
            save_function=accelerator.save,
            state_dict=accelerator.get_state_dict(model),
        )

        if accelerator.is_main_process:
            tokenizer.save_pretrained(args.output_dir)
            with open(os.path.join(args.output_dir, "all_results.json"), "w") as f:
                json.dump({"perplexity": perplexity}, f)


if __name__ == "__main__":
    # should in the main
    # torch.multiprocessing.set_start_method('spawn')
    main()