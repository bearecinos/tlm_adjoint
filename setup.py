#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from setuptools import setup


setup(
    name="tlm_adjoint",
    description="A library for high-level algorithmic differentiation",
    url="https://github.com/tlm-adjoint/tlm_adjoint",
    license="GNU LGPL version 3",
    packages=["tlm_adjoint",
              "tlm_adjoint._code_generator",
              "tlm_adjoint.checkpoint_schedules",
              "tlm_adjoint.fenics",
              "tlm_adjoint.firedrake",
              "tlm_adjoint.numpy"],
    python_requires=">=3.8")
