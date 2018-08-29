#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright(c) 2018 The University of Edinburgh
#
# This file is part of tlm_adjoint.
# 
# tlm_adjoint is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, version 3 of the License.
# 
# tlm_adjoint is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
# 
# You should have received a copy of the GNU Lesser General Public License
# along with tlm_adjoint.  If not, see <https://www.gnu.org/licenses/>.

base = "Firedrake"

from firedrake import *

import firedrake

extract_args = firedrake.solving._extract_args

base_Function = firedrake.Function
base_LinearSolver = firedrake.LinearSolver
base_assemble = assemble
base_project = project
base_solve = solve

__all__ = \
  [
    "base",
    
    "base_Function",
    "base_LinearSolver",
    "base_assemble",
    "base_project",
    "base_solve",
    
    "Constant",
    "DirichletBC",
    "Function",
    "FunctionSpace",
    "LinearSolver",
    "Parameters",
    "TestFunction",
    "TrialFunction",
    "UnitIntervalMesh",
    "action",
    "adjoint",
    "as_backend_type",
    "assemble",
    "dx",
    "extract_args",
    "firedrake",
    "homogenize",
    "inner",
    "parameters",
    "project",
    "solve",
    "replace"
  ]
