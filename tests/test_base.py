#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from tlm_adjoint import DEFAULT_COMM, start_manager
from tlm_adjoint.override import override_function
import tlm_adjoint.interface

import hashlib
import inspect
import json
import numpy as np
from operator import itemgetter
import os
import pytest
import runpy

__all__ = \
    [
        "chdir_tmp_path",
        "seed_test",
        "tmp_path"
    ]


def space_type_error(orig, orig_args, msg, *, stacklevel=1):
    if tlm_adjoint.interface._check_space_types == 0:
        raise RuntimeError(f"{msg}")


tlm_adjoint.interface.space_type_warning = override_function(
    tlm_adjoint.interface.space_type_warning)(space_type_error)


def seed_test(fn):
    @override_function(fn)
    def wrapped_fn(orig, orig_args, *args, **kwargs):
        if "tmp_path" in inspect.signature(fn).parameters:
            # Raises an error if tmp_path is a positional argument
            del kwargs["tmp_path"]

        h = hashlib.sha256()
        h.update(fn.__name__.encode("utf-8"))
        h.update(str(args).encode("utf-8"))
        h.update(str(sorted(kwargs.items(), key=itemgetter(0))).encode("utf-8"))  # noqa: E501
        seed = int(h.hexdigest(), 16) + DEFAULT_COMM.rank
        seed %= 2 ** 32
        np.random.seed(seed)

        return orig_args()

    return wrapped_fn


@pytest.fixture
def tmp_path(tmp_path):
    if DEFAULT_COMM.rank != 0:
        tmp_path = None
    return DEFAULT_COMM.bcast(tmp_path, root=0)


@pytest.fixture
def chdir_tmp_path(tmp_path):
    cwd = os.getcwd()
    os.chdir(tmp_path)

    yield

    os.chdir(cwd)


def run_example(filename, *,
                clear_forward_globals=True):
    start_manager()
    gl = runpy.run_path(filename)

    if clear_forward_globals:
        # Clear objects created by the script. Requires the script to define a
        # 'forward' function.
        gl["forward"].__globals__.clear()


def run_example_notebook(filename, tmp_path):
    if DEFAULT_COMM.size > 1:
        raise RuntimeError("Serial only")

    tmp_filename = os.path.join(tmp_path, "tmp.py")

    with open(filename, "r") as nb_h, open(tmp_filename, "w") as py_h:
        nb = json.load(nb_h)
        if nb["metadata"]["language_info"]["name"] != "python":
            raise RuntimeError("Expected a Python notebook")

        for cell in nb["cells"]:
            if cell["cell_type"] == "code":
                for line in cell["source"]:
                    py_h.write(line)
                py_h.write("\n\n")

    run_example(tmp_filename, clear_forward_globals=False)
