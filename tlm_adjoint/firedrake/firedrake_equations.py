#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""This module includes functionality for use with the tlm_adjoint Firedrake
backend.
"""

from .backend import (
    FunctionSpace, Interpolator, Tensor, TestFunction, TrialFunction,
    VertexOnlyMesh, backend_Constant, backend_Function, backend_assemble)
from ..interface import (
    check_space_type, function_assign, function_comm, function_dtype,
    function_get_values, function_id, function_inner, function_is_scalar,
    function_new_conjugate_dual, function_replacement, function_scalar_value,
    function_set_values, function_space, function_space_type, function_zero,
    is_function, space_new, weakref_method)
from .backend_code_generator_interface import assemble, matrix_multiply

from ..caches import Cache
from ..equation import Equation, ZeroAssignment
from ..tangent_linear import get_tangent_linear

from .caches import form_dependencies, form_key, parameters_key
from .equations import (
    EquationSolver, ExprEquation, bind_form, derivative, extract_dependencies,
    unbind_form, unbound_form)
from .functions import diff, eliminate_zeros

import itertools
import numpy as np
import pyop2
import ufl
import warnings

__all__ = \
    [
        "LocalSolverCache",
        "local_solver_cache",
        "set_local_solver_cache",

        "ExprAssignment",
        "LocalProjection",
        "PointInterpolation",

        "LocalProjectionSolver",
        "PointInterpolationSolver"
    ]


def local_solver_key(form, form_compiler_parameters):
    return (form_key(form),
            parameters_key(form_compiler_parameters))


def LocalSolver(form, *,
                form_compiler_parameters=None):
    if form_compiler_parameters is None:
        form_compiler_parameters = {}

    # Perform zero elimination here, rather than in overridden assemble, as
    # Tensor(form).inv is not a Form
    form = eliminate_zeros(form, force_non_empty_form=True)
    local_solver = backend_assemble(
        Tensor(form).inv,
        form_compiler_parameters=form_compiler_parameters)

    def solve_local(self, x, b):
        matrix_multiply(self, b, tensor=x)
    local_solver._tlm_adjoint__solve_local = weakref_method(
        solve_local, local_solver)

    return local_solver


class LocalSolverCache(Cache):
    """A :class:`tlm_adjoint.caches.Cache` for element-wise local block
    diagonal linear solver data.
    """

    def local_solver(self, form, *,
                     form_compiler_parameters=None, replace_map=None):
        """Compute data for an element-wise local block diagonal linear
        solver and cache the result, or return a previously cached result.

        :arg form: An arity two UFL :class:`Form`, defining the element-wise
            local block diagonal matrix.
        :arg form_compiler_parameters: Form compiler parameters.
        :arg replace_map: A :class:`Mapping` defining a map from symbolic
            variables to values.
        :returns: A :class:`tuple` `(value_ref, value)`. `value` is a Firedrake
            :class:`Matrix` storing the assembled inverse matrix, and
            `value_ref` is a :class:`tlm_adjoint.caches.CacheRef` storing a
            reference to `value`.
        """

        if form_compiler_parameters is None:
            form_compiler_parameters = {}

        form = eliminate_zeros(form, force_non_empty_form=True)
        key = local_solver_key(form, form_compiler_parameters)

        def value():
            if replace_map is None:
                assemble_form = form
            else:
                assemble_form = ufl.replace(form, replace_map)
            return LocalSolver(
                assemble_form,
                form_compiler_parameters=form_compiler_parameters)

        return self.add(key, value,
                        deps=tuple(form_dependencies(form).values()))


_local_solver_cache = LocalSolverCache()


def local_solver_cache():
    """
    :returns: The default :class:`LocalSolverCache`.
    """

    return _local_solver_cache


def set_local_solver_cache(local_solver_cache):
    """Set the default :class:`LocalSolverCache`.

    :arg local_solver_cache: The new default :class:`LocalSolverCache`.
    """

    global _local_solver_cache
    _local_solver_cache = local_solver_cache


class LocalProjection(EquationSolver):
    """Represents the solution of a finite element variational problem
    performing a projection onto the space for `x`, for the case where the mass
    matrix is element-wise local block diagonal.

    :arg x: A function defining the forward solution.
    :arg rhs: A UFL :class:`Expr` defining the expression to project onto the
        space for `x`, or a UFL :class:`Form` defining the right-hand-side
        of the finite element variational problem. Should not depend on `x`.

    Remaining arguments are passed to the :class:`EquationSolver` constructor.
    """

    def __init__(self, x, rhs, *,
                 form_compiler_parameters=None, cache_jacobian=None,
                 cache_rhs_assembly=None, match_quadrature=None,
                 defer_adjoint_assembly=None):
        if form_compiler_parameters is None:
            form_compiler_parameters = {}

        space = function_space(x)
        test, trial = TestFunction(space), TrialFunction(space)
        lhs = ufl.inner(trial, test) * ufl.dx
        if not isinstance(rhs, ufl.classes.Form):
            rhs = ufl.inner(rhs, test) * ufl.dx

        super().__init__(
            lhs == rhs, x,
            form_compiler_parameters=form_compiler_parameters,
            solver_parameters={},
            cache_jacobian=cache_jacobian,
            cache_rhs_assembly=cache_rhs_assembly,
            match_quadrature=match_quadrature,
            defer_adjoint_assembly=defer_adjoint_assembly)

    def forward_solve(self, x, deps=None):
        if self._cache_rhs_assembly:
            b = self._cached_rhs(deps)
        elif deps is None:
            b = assemble(
                self._rhs,
                form_compiler_parameters=self._form_compiler_parameters)
        else:
            if self._forward_eq is None:
                self._forward_eq = \
                    (None,
                     None,
                     unbound_form(self._rhs, self.dependencies()))
            _, _, rhs = self._forward_eq
            bind_form(rhs, deps)
            b = assemble(
                rhs,
                form_compiler_parameters=self._form_compiler_parameters)
            unbind_form(rhs)

        if self._cache_jacobian:
            local_solver = self._forward_J_solver()
            if local_solver is None:
                self._forward_J_solver, local_solver = \
                    local_solver_cache().local_solver(
                        self._lhs,
                        form_compiler_parameters=self._form_compiler_parameters)  # noqa: E501
        else:
            local_solver = LocalSolver(
                self._lhs,
                form_compiler_parameters=self._form_compiler_parameters)

        local_solver._tlm_adjoint__solve_local(x, b)

    def adjoint_jacobian_solve(self, adj_x, nl_deps, b):
        if self._cache_jacobian:
            local_solver = self._forward_J_solver()
            if local_solver is None:
                self._forward_J_solver, local_solver = \
                    local_solver_cache().local_solver(
                        self._lhs,
                        form_compiler_parameters=self._form_compiler_parameters)  # noqa: E501
        else:
            local_solver = LocalSolver(
                self._lhs,
                form_compiler_parameters=self._form_compiler_parameters)

        adj_x = self.new_adj_x()
        local_solver._tlm_adjoint__solve_local(adj_x, b)
        return adj_x

    def tangent_linear(self, M, dM, tlm_map):
        x = self.x()

        tlm_rhs = ufl.classes.Form([])
        for dep in self.dependencies():
            if dep != x:
                tau_dep = get_tangent_linear(dep, M, dM, tlm_map)
                if tau_dep is not None:
                    tlm_rhs += derivative(self._rhs, dep, argument=tau_dep)

        tlm_rhs = ufl.algorithms.expand_derivatives(tlm_rhs)
        if tlm_rhs.empty():
            return ZeroAssignment(tlm_map[x])
        else:
            return LocalProjection(
                tlm_map[x], tlm_rhs,
                form_compiler_parameters=self._form_compiler_parameters,
                cache_jacobian=self._cache_jacobian,
                cache_rhs_assembly=self._cache_rhs_assembly,
                defer_adjoint_assembly=self._defer_adjoint_assembly)


class LocalProjectionSolver(LocalProjection):
    ""

    def __init__(self, rhs, x, form_compiler_parameters=None,
                 cache_jacobian=None, cache_rhs_assembly=None,
                 match_quadrature=None, defer_adjoint_assembly=None):
        warnings.warn("LocalProjectionSolver is deprecated -- "
                      "use LocalProjection instead",
                      DeprecationWarning, stacklevel=2)
        super().__init__(
            x, rhs, form_compiler_parameters=form_compiler_parameters,
            cache_jacobian=cache_jacobian,
            cache_rhs_assembly=cache_rhs_assembly,
            match_quadrature=match_quadrature,
            defer_adjoint_assembly=defer_adjoint_assembly)


def vmesh_coords_map(vmesh, X_coords):
    comm = vmesh.comm
    N, _ = X_coords.shape

    vmesh_coords = vmesh.coordinates.dat.data_ro
    Nm, _ = vmesh_coords.shape

    vmesh_coords_indices = {tuple(vmesh_coords[i, :]): i for i in range(Nm)}
    vmesh_coords_map = np.full(Nm, -1, dtype=np.int64)
    for i in range(N):
        key = tuple(X_coords[i, :])
        if key in vmesh_coords_indices:
            vmesh_coords_map[vmesh_coords_indices[key]] = i
    if (vmesh_coords_map < 0).any():
        raise RuntimeError("Failed to find vertex map")

    vmesh_coords_map = comm.allgather(vmesh_coords_map)
    if len(tuple(itertools.chain(*vmesh_coords_map))) != N:
        raise RuntimeError("Failed to find vertex map")

    return vmesh_coords_map


class PointInterpolation(Equation):
    r"""Represents interpolation of a scalar-valued function at given points.

    The forward residual :math:`\mathcal{F}` is defined so that :math:`\partial
    \mathcal{F} / \partial x` is the identity.

    :arg X: A scalar-function, or a :class:`Sequence` of scalar-valued
        functions, defining the forward solution.
    :arg y: A scalar-valued Firedrake :class:`Function` to interpolate.
    :arg X_coords: A NumPy :class:`ndarray` defining the coordinates at which
        to interpolate `y`. Shape is `(n, d)` where `n` is the number of
        interpolation points and `d` is the geometric dimension. Ignored if `P`
        is supplied.
    """

    def __init__(self, X, y, X_coords=None, *,
                 _interp=None):
        if is_function(X):
            X = (X,)

        for x in X:
            check_space_type(x, "primal")
            if not function_is_scalar(x):
                raise ValueError("Solution must be a scalar, or a sequence of "
                                 "scalars")
        check_space_type(y, "primal")

        if X_coords is None:
            if _interp is None:
                raise TypeError("X_coords required")
        else:
            if len(X) != X_coords.shape[0]:
                raise ValueError("Invalid number of functions")
        if not isinstance(y, backend_Function):
            raise TypeError("y must be a Function")
        if len(y.ufl_shape) > 0:
            raise ValueError("y must be a scalar-valued Function")

        interp = _interp
        if interp is None:
            y_space = function_space(y)
            vmesh = VertexOnlyMesh(y_space.mesh(), X_coords)
            vspace = FunctionSpace(vmesh, "Discontinuous Lagrange", 0)
            interp = Interpolator(TestFunction(y_space), vspace)
            if not hasattr(interp, "_tlm_adjoint__vmesh_coords_map"):
                interp._tlm_adjoint__vmesh_coords_map = vmesh_coords_map(vmesh, X_coords)  # noqa: E501

        super().__init__(X, list(X) + [y], nl_deps=[], ic=False, adj_ic=False)
        self._interp = interp

    def forward_solve(self, X, deps=None):
        if is_function(X):
            X = (X,)
        y = (self.dependencies() if deps is None else deps)[-1]

        Xm = space_new(self._interp.V)
        self._interp.interpolate(y, output=Xm)

        X_values = function_comm(Xm).allgather(Xm.dat.data_ro)
        vmesh_coords_map = self._interp._tlm_adjoint__vmesh_coords_map
        for x_val, index in zip(itertools.chain(*X_values),
                                itertools.chain(*vmesh_coords_map)):
            function_assign(X[index], x_val)

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_X):
        if is_function(adj_X):
            adj_X = (adj_X,)

        if dep_index < len(adj_X):
            return adj_X[dep_index]
        elif dep_index == len(adj_X):
            adj_Xm = space_new(self._interp.V)

            vmesh_coords_map = self._interp._tlm_adjoint__vmesh_coords_map
            rank = function_comm(adj_Xm).rank
            # This line must be outside the loop to avoid deadlocks
            adj_Xm_data = adj_Xm.dat.data
            for i, j in enumerate(vmesh_coords_map[rank]):
                adj_Xm_data[i] = function_scalar_value(adj_X[j])

            F = function_new_conjugate_dual(self.dependencies()[-1])
            self._interp.interpolate(adj_Xm, transpose=True, output=F)
            return (-1.0, F)
        else:
            raise IndexError("dep_index out of bounds")

    def adjoint_jacobian_solve(self, adj_X, nl_deps, B):
        return B

    def tangent_linear(self, M, dM, tlm_map):
        X = self.X()
        y = self.dependencies()[-1]

        tlm_y = get_tangent_linear(y, M, dM, tlm_map)
        if tlm_y is None:
            return ZeroAssignment([tlm_map[x] for x in X])
        else:
            return PointInterpolation([tlm_map[x] for x in X], tlm_y,
                                      _interp=self._interp)


class PointInterpolationSolver(PointInterpolation):
    ""

    def __init__(self, y, X, X_coords=None, P=None, P_T=None, tolerance=None):
        if P is not None:
            warnings.warn("P argument is deprecated and has no effect",
                          DeprecationWarning, stacklevel=2)
        if P_T is not None:
            warnings.warn("P_T argument is deprecated and has no effect",
                          DeprecationWarning, stacklevel=2)
        if tolerance is not None:
            warnings.warn("tolerance argument is deprecated and has no effect",
                          DeprecationWarning, stacklevel=2)
        warnings.warn("PointInterpolationSolver is deprecated -- "
                      "use PointInterpolation instead",
                      DeprecationWarning, stacklevel=2)
        super().__init__(X, y, X_coords)


class ExprAssignment(ExprEquation):
    r"""Represents an evaluation of `rhs`, storing the result in `x`. Uses
    `Function.assign` to perform the evaluation.

    The forward residual :math:`\mathcal{F}` is defined so that :math:`\partial
    \mathcal{F} / \partial x` is the identity.

    :arg x: A Firedrake :class:`Function` defining the forward solution.
    :arg rhs: A UFL :class:`Expr` defining the expression to evaluate. Should
        not depend on `x`.
    :arg subset: A PyOP2 :class:`Subset`. If provided then defines a subset of
        degrees of freedom at which to evaluate `rhs`. Other degrees of freedom
        are set to zero.
    """

    def __init__(self, x, rhs, *,
                 subset=None):
        deps, nl_deps = extract_dependencies(
            rhs, space_type=function_space_type(x))
        if function_id(x) in deps:
            raise ValueError("Invalid non-linear dependency")
        deps, nl_deps = list(deps.values()), tuple(nl_deps.values())
        deps.insert(0, x)

        if subset is not None:
            subset = pyop2.Subset(subset.superset, subset.indices)

        super().__init__(x, deps, nl_deps=nl_deps, ic=False, adj_ic=False)
        self._rhs = rhs
        self._subset = subset

    def drop_references(self):
        replace_map = {dep: function_replacement(dep)
                       for dep in self.dependencies()}

        super().drop_references()

        self._rhs = ufl.replace(self._rhs, replace_map)

    def forward_solve(self, x, deps=None):
        rhs = self._rhs
        if deps is not None:
            rhs = self._replace(rhs, deps)
        if self._subset is not None:
            function_zero(x)
        x.assign(rhs, subset=self._subset)

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_x):
        eq_deps = self.dependencies()
        if dep_index < 0 or dep_index >= len(eq_deps):
            raise IndexError("dep_index out of bounds")
        elif dep_index == 0:
            return adj_x

        dep = eq_deps[dep_index]
        dF = diff(self._rhs, dep)
        dF = ufl.algorithms.expand_derivatives(dF)
        dF = eliminate_zeros(dF)
        dF = self._nonlinear_replace(dF, nl_deps)

        F = function_new_conjugate_dual(dep)
        if isinstance(F, backend_Constant):
            dF = function_new_conjugate_dual(adj_x).assign(dF,
                                                           subset=self._subset)
            F.assign(function_inner(adj_x, dF))
        elif isinstance(F, backend_Function):
            dF = function_dtype(F)(dF).conjugate()
            F.assign(adj_x, subset=self._subset)
            # Work around Firedrake issue #3047
            function_set_values(F, dF * function_get_values(F))
        else:
            raise TypeError(f"Unexpected type: {type(F)}")
        return (-1.0, F)

    def adjoint_jacobian_solve(self, adj_x, nl_deps, b):
        return b

    def tangent_linear(self, M, dM, tlm_map):
        x = self.x()

        tlm_rhs = ufl.classes.Zero(shape=x.ufl_shape)
        for dep in self.dependencies():
            if dep != x:
                tau_dep = get_tangent_linear(dep, M, dM, tlm_map)
                if tau_dep is not None:
                    # Cannot use += as Firedrake might add to the *values* for
                    # tlm_rhs
                    tlm_rhs = (tlm_rhs
                               + derivative(self._rhs, dep, argument=tau_dep))

        if isinstance(tlm_rhs, ufl.classes.Zero):
            return ZeroAssignment(tlm_map[x])
        tlm_rhs = ufl.algorithms.expand_derivatives(tlm_rhs)
        if isinstance(tlm_rhs, ufl.classes.Zero):
            return ZeroAssignment(tlm_map[x])
        else:
            return ExprAssignment(tlm_map[x], tlm_rhs, subset=self._subset)
