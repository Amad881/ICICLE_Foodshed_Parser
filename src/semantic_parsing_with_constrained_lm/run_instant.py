# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
import bdb
import datetime
import importlib
import json
import os
import pathlib
import re
import sys
import traceback
from dataclasses import dataclass
from enum import Enum
from typing import (
    AsyncContextManager,
    Callable,
    Dict,
    Generic,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import torch
import typer

from semantic_parsing_with_constrained_lm.util import logger
from semantic_parsing_with_constrained_lm.async_tools import limits
from semantic_parsing_with_constrained_lm.datum import FullDatumSub
from semantic_parsing_with_constrained_lm.eval import Metric, exact_match_with_logging
from semantic_parsing_with_constrained_lm.lm import TRAINED_MODEL_DIR, ClientType
from semantic_parsing_with_constrained_lm.model import Model

from pdb import set_trace as bp

@dataclass
class Experiment(Generic[FullDatumSub]):
    model: Model
    client: AsyncContextManager
    test_data: List[FullDatumSub]
    metrics: Mapping[str, Metric[Sequence[str], FullDatumSub]]


class EvalSplit(str, Enum):
    """Controls which data is used for evaluation."""

    # 100-200 examples from the dev set.
    DevSubset = "dev-subset"
    # All dev set examples.
    DevFull = "dev-full"
    # 100-200 examples from the test set.
    TestSubset = "test-subset"
    # All the test set examples.
    TestFull = "test-full"
    # 100-200 examples from the training set.
    # Used as dev when we do not have access to test, and need to get results on full dev.
    TrainSubset = "train-subset"


def main(
    config_name: str = typer.Option(...),
    log_dir: pathlib.Path = typer.Option(...),
    debug: bool = typer.Option(False),
    exp_names: Optional[List[str]] = typer.Option(
        None
    ),  # pylint: disable=unused-argument
    exp_name_pattern: Optional[List[str]] = typer.Option(None),
    ids: Optional[List[str]] = typer.Option(None),
    rerun: bool = typer.Option(False),
    num_eval_examples: Optional[int] = typer.Option(None),
    model: ClientType = typer.Option(ClientType.GPT2),
    rank: int = typer.Option(0),
    world_size: int = typer.Option(1),
    results_dir: str = typer.Option("results"),
    utterance: str = typer.Option(""),
):
    async def inner():
        nonlocal exp_names

        config_mod = importlib.import_module(config_name)
        kwargs = {
            "model": model,
            "results_dir": results_dir,
            "rank": rank,
            "utterance": utterance,
        }

        # TODO: Change log_dir argument into exp_log_dir
        exps: Union[
            Iterable[Tuple[str, Experiment]],  # Deprecated
            Dict[str, Callable[[], Experiment]],
        ] = config_mod.build_config(
            log_dir, **kwargs
        )  # type: ignore
        if isinstance(exps, dict):
            exps_dict = exps
        else:
            exps_dict = {exp_name: (lambda exp=exp: exp) for exp_name, exp in exps}

        if exp_names and exp_name_pattern:
            # print("Cannot specify --exp-names and --exp-name-pattern together")
            return

        if exp_name_pattern:
            exp_names = [
                name
                for name in exps_dict.keys()
                if any(re.match(pat, name) for pat in exp_name_pattern)
            ]
            if not exp_names:
                # print("--exp-name-pattern matched no experiment names")
                return
            # print("Matched experiments:")
            # for name in exp_names:
            #     print(name)
        elif not exp_names:
            exp_names = list(exps_dict.keys())

        error = False
        for exp_name in exp_names:
            if exp_name not in exps_dict:
                # print(f"Experiment {exp_name} not found in config.")
                error = True
        if error:
            # print("Names in config:")
            # for name in exps_dict.keys():
            #     print(name)
            return

        for exp_name in exp_names:
            await run(exp_name, exps_dict[exp_name]())

    async def run(exp_name: str, exp: Experiment) -> None:
        if world_size == 1:
            # exp_log_dir = log_dir / exp_name
            exp_log_dir = pathlib.Path(log_dir + "/" + exp_name)
        else:
            exp_log_dir = log_dir / f"{exp_name}_rank-{rank:02d}-of-{world_size:02d}"
        exp_log_dir.mkdir(exist_ok=True, parents=True)
        # results_path = exp_log_dir / "results.json"
        results_path = exp_log_dir / "results.json"
        # if results_path.exists() and not rerun:
        #     print(f"Skipping {exp_name}, already finished")
        #     return
        # print("********************")
        # print(f"Running {exp_name} rank {rank} world size {world_size}")
        # print("********************")
        now = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        all_metric_results: Dict[str, float] = {}

        test_data = (
            [datum for datum in exp.test_data if datum.dialogue_id in ids]
            if ids
            else exp.test_data
        )
        if not test_data:
            # print(f"No test data! ids: {ids}")
            return

        # print(f"Total test examples: {len(test_data)}")
        test_data = test_data[
            (rank * len(test_data))
            // world_size : ((rank + 1) * len(test_data))
            // world_size
        ]
        if num_eval_examples is not None:
            test_data = test_data[:num_eval_examples]

        # print(f"Test examples this shard: {len(test_data)}")
        current_test_index = 0

        # Find past model outputs
        candidate_past_model_outputs: List[Tuple[pathlib.Path, List[Dict]]] = []
        for past_model_outputs_path in exp_log_dir.glob("model_outputs.*.jsonl"):
            candidate_past_model_outputs.append(
                (
                    past_model_outputs_path,
                    [json.loads(line) for line in open(past_model_outputs_path, "r")],
                )
            )
        if candidate_past_model_outputs:
            # past_model_outputs_path, past_model_outputs_to_copy = max(
            #     candidate_past_model_outputs, key=lambda t: len(t[1])
            # )
            # print(
            #     f"*** Copying {len(past_model_outputs_to_copy)} past results from {past_model_outputs_path} ***"
            # )
            past_model_outputs_to_copy = []
        else:
            past_model_outputs_to_copy = []
        # with logger.intercept_output(
        #     exp_log_dir / f"stdout.{now}", exp_log_dir / f"stderr.{now}",
        # ), 
        with open(str(exp_log_dir) + "/model_outputs." + str(now) + ".jsonl", "w") as model_outputs_f:
            try:
                for metric in exp.metrics.values():
                    metric.reset()
                for test_datum, past_model_output in zip(
                    test_data, past_model_outputs_to_copy
                ):
                    assert test_datum.dialogue_id == past_model_output["test_datum_id"]
                    assert (
                        test_datum.turn_part_index
                        == past_model_output["test_datum_turn_part_index"]
                    )
                    for metric in exp.metrics.values():
                        metric.update(past_model_output["outputs"], test_datum)
                    model_outputs_f.write(json.dumps(past_model_output) + "\n")
                model_outputs_f.flush()
                async with exp.client:
                    async for kbest, test_datum in limits.map_async_limited(
                        exp.model.predict,
                        test_data[len(past_model_outputs_to_copy) :],
                        max_concurrency=1,
                        wrap_exception=not debug,
                    ):
                        beam_search_results = [beam.text for beam in kbest]
                        # print(beam_search_results)
                        returnVal.extend(kbest)
                        model_outputs_f.write(
                            json.dumps(
                                {
                                    "test_datum_id": test_datum.dialogue_id,
                                    "test_datum_turn_part_index": test_datum.turn_part_index,
                                    "outputs": beam_search_results,
                                }
                            )
                            + "\n"
                        )
                        model_outputs_f.flush()
            except (  # pylint: disable=try-except-raise
                KeyboardInterrupt,
                bdb.BdbQuit,
            ):
                # If we get Ctrl-C then we want to stop the entire program,
                # instead of just skipping this one experiment.
                raise
            except Exception as e:  # pylint: disable=broad-except
                if isinstance(e, limits.MapInnerException):
                    if isinstance(e.__cause__, (KeyboardInterrupt, bdb.BdbQuit)):
                        # If we get Ctrl-C then we want to stop the entire program,
                        # instead of just skipping this one experiment.
                        raise e.__cause__

                    # pylint: disable=no-member
                    # print(
                    #     f"Last test_datum: {e.orig_item} in experiment {exp_name}",
                    #     file=sys.stderr,
                    # )

                if debug:
                    # If we're running inside a debugger, re-raise the
                    # exception so that we can debug it.
                    raise
                # Log the exception, and move onto the next item in `exps`.
                # traceback.print_exc()

    returnVal = []
    with torch.no_grad():
        asyncio.run(inner())
    outVal = []
    for result in returnVal:
        processedText = result.text.replace("<s>", "")
        processedText = processedText.strip()
        score = result.cost
        outVal.append({"text": processedText, "score": score})

    # Print the results as a csv
    # for result in outVal:
    #     print(f"{result['text']} , {result['score']}")

    # Outval to json
    with open('output.json', 'w') as fout:
        json.dump(outVal, fout)

    # with open('output.tsv', 'a') as fout:
    #     fout.write("\n")
    #     for result in outVal:
    #         fout.write(f"{utterance}, {result['text']}, {result['score']}\n")
    return outVal

def utteranceRun(query):
    config_name = 'semantic_parsing_with_constrained_lm.configs.liveRun'
    log_dir =  pathlib.Path('logs/')
    debug = False
    exp_names = None
    exp_name_pattern='overnight_Bart_test-full_.*_constrained_canonicalUtterance_train-200'
    ids = None
    rerun = False
    num_eval_examples = None
    model = 'Bart'
    rank = 0
    world_size = 1
    results_dir = 'results'
    utterance = query

    async def inner():
        nonlocal exp_names

        config_mod = importlib.import_module(config_name)
        kwargs = {
            "model": model,
            "results_dir": results_dir,
            "rank": rank,
            "utterance": utterance,
        }

        # TODO: Change log_dir argument into exp_log_dir
        exps: Union[
            Iterable[Tuple[str, Experiment]],  # Deprecated
            Dict[str, Callable[[], Experiment]],
        ] = config_mod.build_config(
            log_dir, **kwargs
        )  # type: ignore
        if isinstance(exps, dict):
            exps_dict = exps
        else:
            exps_dict = {exp_name: (lambda exp=exp: exp) for exp_name, exp in exps}

        if exp_names and exp_name_pattern:
            # print("Cannot specify --exp-names and --exp-name-pattern together")
            return

        if exp_name_pattern:
            exp_names = [
                name
                for name in exps_dict.keys()
                if any(re.match(pat, name) for pat in exp_name_pattern)
            ]
            if not exp_names:
                # print("--exp-name-pattern matched no experiment names")
                return
            # print("Matched experiments:")
            # for name in exp_names:
            #     print(name)
        elif not exp_names:
            exp_names = list(exps_dict.keys())

        error = False
        for exp_name in exp_names:
            if exp_name not in exps_dict:
                # print(f"Experiment {exp_name} not found in config.")
                error = True
        if error:
            # print("Names in config:")
            # for name in exps_dict.keys():
            #     print(name)
            return

        for exp_name in exp_names:
            await run(exp_name, exps_dict[exp_name]())

    async def run(exp_name: str, exp: Experiment) -> None:
        if world_size == 1:
            exp_log_dir = log_dir / exp_name
            # exp_log_dir = pathlib.Path(log_dir + "/" + exp_name)
        else:
            exp_log_dir = log_dir / f"{exp_name}_rank-{rank:02d}-of-{world_size:02d}"
        exp_log_dir.mkdir(exist_ok=True, parents=True)
        # results_path = exp_log_dir / "results.json"
        results_path = exp_log_dir / "results.json"
        # if results_path.exists() and not rerun:
        #     print(f"Skipping {exp_name}, already finished")
        #     return
        # print("********************")
        # print(f"Running {exp_name} rank {rank} world size {world_size}")
        # print("********************")
        now = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        all_metric_results: Dict[str, float] = {}

        test_data = (
            [datum for datum in exp.test_data if datum.dialogue_id in ids]
            if ids
            else exp.test_data
        )
        if not test_data:
            # print(f"No test data! ids: {ids}")
            return

        # print(f"Total test examples: {len(test_data)}")
        test_data = test_data[
            (rank * len(test_data))
            // world_size : ((rank + 1) * len(test_data))
            // world_size
        ]
        if num_eval_examples is not None:
            test_data = test_data[:num_eval_examples]

        # print(f"Test examples this shard: {len(test_data)}")
        current_test_index = 0

        # Find past model outputs
        candidate_past_model_outputs: List[Tuple[pathlib.Path, List[Dict]]] = []
        for past_model_outputs_path in exp_log_dir.glob("model_outputs.*.jsonl"):
            candidate_past_model_outputs.append(
                (
                    past_model_outputs_path,
                    [json.loads(line) for line in open(past_model_outputs_path, "r")],
                )
            )
        if candidate_past_model_outputs:
            # past_model_outputs_path, past_model_outputs_to_copy = max(
            #     candidate_past_model_outputs, key=lambda t: len(t[1])
            # )
            # print(
            #     f"*** Copying {len(past_model_outputs_to_copy)} past results from {past_model_outputs_path} ***"
            # )
            past_model_outputs_to_copy = []
        else:
            past_model_outputs_to_copy = []
        # with logger.intercept_output(
        #     exp_log_dir / f"stdout.{now}", exp_log_dir / f"stderr.{now}",
        # ), 
        with open(str(exp_log_dir) + "/model_outputs." + str(now) + ".jsonl", "w") as model_outputs_f:
            try:
                for metric in exp.metrics.values():
                    metric.reset()
                for test_datum, past_model_output in zip(
                    test_data, past_model_outputs_to_copy
                ):
                    assert test_datum.dialogue_id == past_model_output["test_datum_id"]
                    assert (
                        test_datum.turn_part_index
                        == past_model_output["test_datum_turn_part_index"]
                    )
                    for metric in exp.metrics.values():
                        metric.update(past_model_output["outputs"], test_datum)
                    model_outputs_f.write(json.dumps(past_model_output) + "\n")
                model_outputs_f.flush()
                async with exp.client:
                    async for kbest, test_datum in limits.map_async_limited(
                        exp.model.predict,
                        test_data[len(past_model_outputs_to_copy) :],
                        max_concurrency=1,
                        wrap_exception=not debug,
                    ):
                        beam_search_results = [beam.text for beam in kbest]
                        # print(beam_search_results)
                        returnVal.extend(kbest)
                        model_outputs_f.write(
                            json.dumps(
                                {
                                    "test_datum_id": test_datum.dialogue_id,
                                    "test_datum_turn_part_index": test_datum.turn_part_index,
                                    "outputs": beam_search_results,
                                }
                            )
                            + "\n"
                        )
                        model_outputs_f.flush()
            except (  # pylint: disable=try-except-raise
                KeyboardInterrupt,
                bdb.BdbQuit,
            ):
                # If we get Ctrl-C then we want to stop the entire program,
                # instead of just skipping this one experiment.
                raise
            except Exception as e:  # pylint: disable=broad-except
                if isinstance(e, limits.MapInnerException):
                    if isinstance(e.__cause__, (KeyboardInterrupt, bdb.BdbQuit)):
                        # If we get Ctrl-C then we want to stop the entire program,
                        # instead of just skipping this one experiment.
                        raise e.__cause__

                    # pylint: disable=no-member
                    # print(
                    #     f"Last test_datum: {e.orig_item} in experiment {exp_name}",
                    #     file=sys.stderr,
                    # )

                if debug:
                    # If we're running inside a debugger, re-raise the
                    # exception so that we can debug it.
                    raise
                # Log the exception, and move onto the next item in `exps`.
                traceback.print_exc()

    returnVal = []
    with torch.no_grad():
        asyncio.run(inner())
    outVal = []
    for result in returnVal:
        processedText = result.text.replace("<s>", "")
        processedText = processedText.strip()
        score = result.cost
        outVal.append({"text": processedText, "score": score})

    # Print the results as a csv
    # for result in outVal:
    #     print(f"{result['text']} , {result['score']}")

    # Outval to json
    with open('output.json', 'w') as fout:
        json.dump(outVal, fout)

    # with open('output.tsv', 'a') as fout:
    #     fout.write("\n")
    #     for result in outVal:
    #         fout.write(f"{utterance}, {result['text']}, {result['score']}\n")
    return outVal


if __name__ == "__main__":
    # outVal = typer.run(main)
    checkVal = main()