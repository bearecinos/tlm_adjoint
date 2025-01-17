#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from .interface import function_assign, function_axpy, function_copy, \
    function_id, function_inner, function_update_caches, function_zero, \
    is_function, no_space_type_checking

from .adjoint import AdjointModelRHS
from .alias import WeakAlias
from .equation import Equation, ZeroAssignment

import logging
import numpy as np
import warnings

__all__ = \
    [
        "CustomNormSq",
        "FixedPointSolver"
    ]


@no_space_type_checking
def l2_norm_sq(x):
    return function_inner(x, x)


class CustomNormSq:
    r"""Defines the square of the norm of forward and adjoint solutions.

    Callables are used to define squared norms for the forward and adjoint
    solutions of equations. The total squared norm is then the sum of the
    squares.

    :arg eqs: A :class:`Sequence` of :class:`tlm_adjoint.equation.Equation`
        objects.
    :arg norm_sqs: A :class:`Sequence`. Each element is either a callable, or a
        :class:`Sequence` of callables. The callables define the squared norm
        associated with the corresponding components of the forward solution
        for the corresponding :class:`tlm_adjoint.equation.Equation` in `eqs`.
        Each callable accepts a single function and returns a :class:`float`.
        Defaults to the square of the :math:`l_2` norm of the degrees of
        freedom vector.
    :arg adj_norm_sqs: A :class:`Sequence`. Each element is either a callable,
        or a :class:`Sequence` of callables. The callables define the squared
        norm associated with the corresponding components of the adjoint
        solution for the corresponding :class:`tlm_adjoint.equation.Equation`
        in `eqs`. Each callable accepts a single function and returns a
        :class:`float`. Defaults to the square of the :math:`l_2` norm of the
        degrees of freedom vector.
    """

    def __init__(self, eqs, *, norm_sqs=None, adj_norm_sqs=None):
        if norm_sqs is None:
            norm_sqs = [l2_norm_sq for eq in eqs]
        if adj_norm_sqs is None:
            adj_norm_sqs = [l2_norm_sq for eq in eqs]

        norm_sqs = list(norm_sqs)
        if len(eqs) != len(norm_sqs):
            raise ValueError("Invalid squared norm callable(s)")
        for i, (eq, X_norm_sq) in enumerate(zip(eqs, norm_sqs)):
            if callable(X_norm_sq):
                X_norm_sq = (X_norm_sq,)
            if len(eq.X()) != len(X_norm_sq):
                raise ValueError("Invalid squared norm callable(s)")
            norm_sqs[i] = tuple(X_norm_sq)

        adj_norm_sqs = list(adj_norm_sqs)
        if len(eqs) != len(adj_norm_sqs):
            raise ValueError("Invalid squared norm callable(s)")
        for i, (eq, X_norm_sq) in enumerate(zip(eqs, adj_norm_sqs)):
            if callable(X_norm_sq):
                X_norm_sq = (X_norm_sq,)
            if len(eq.X()) != len(X_norm_sq):
                raise ValueError("Invalid squared norm callable(s)")
            adj_norm_sqs[i] = tuple(X_norm_sq)

        self._norm_sqs = tuple(norm_sqs)
        self._adj_norm_sqs = tuple(adj_norm_sqs)

    def _forward_norm_sq(self, eq_X):
        norm_sq = 0.0
        assert len(eq_X) == len(self._norm_sqs)
        for X, X_norm_sq in zip(eq_X, self._norm_sqs):
            assert len(X) == len(X_norm_sq)
            for x, x_norm_sq in zip(X, X_norm_sq):
                norm_sq_term = complex(x_norm_sq(x))
                assert norm_sq_term.imag == 0.0
                norm_sq_term = norm_sq_term.real
                assert norm_sq_term >= 0.0
                norm_sq += norm_sq_term

        return norm_sq

    def _adjoint_norm_sq(self, eq_adj_X):
        norm_sq = 0.0
        assert len(eq_adj_X) == len(self._adj_norm_sqs)
        for X, X_norm_sq in zip(eq_adj_X, self._adj_norm_sqs):
            assert len(X) == len(X_norm_sq)
            for x, x_norm_sq in zip(X, X_norm_sq):
                norm_sq_term = complex(x_norm_sq(x))
                assert norm_sq_term.imag == 0.0
                norm_sq_term = norm_sq_term.real
                assert norm_sq_term >= 0.0
                norm_sq += norm_sq_term

        return norm_sq


class FixedPointSolver(Equation, CustomNormSq):
    """A fixed-point solver. Solves the given equations in sequence until
    either an absolute or relative tolerance is reached.

    Derives tangent-linear and adjoint information using the approach described
    in:

        - Jean Charles Gilbert, 'Automatic differentiation and iterative
          processes', Optimization Methods and Software, 1(1), pp. 13--21,
          1992, doi: 10.1080/10556789208805503
        - Bruce Christianson, 'Reverse accumulation and attractive fixed
          points', Optimization Methods and Software, 3(4), pp. 311--326, 1994,
          doi: 10.1080/10556789408805572

    :arg eqs: A :class:`Sequence` of :class:`tlm_adjoint.equation.Equation`
        objects. One forward iteration consists of computing, in order, a
        forward solution for all :class:`tlm_adjoint.equation.Equation`
        objects.
    :arg solver_parameters: A :class:`Mapping` defining solver parameters.
        Parameters (a number of which are based on KrylovSolver parameters in
        FEniCS 2017.2.0) are:

            - absolute_tolerance: A :class:`float` defining the absolute
              tolerance for a change in the solution in one iteration.
              Required.
            - relative_tolerance: A :class:`float` defining the relative
              tolerance for a change in the solution in one iteration.
              Required.
            - maximum_iterations: An :class:`int` defining the maximum
              permitted iterations. Defaults to 1000.
            - nonzero_initial_guess: A :class:`bool` indicating whether to use
              a non-zero initial guess in a forward solve. Defaults to `True`.
            - adjoint_nonzero_initial_guess: A :class:`bool` indicating whether
              to use a non-zero initial guess in an adjoint solve. Defaults to
              `True`.
            - adjoint_eqs_index_0: One adjoint iteration consists of computing,
              in reverse order, an adjoint solution for all
              :class:`tlm_adjoint.equation.Equation` objects. This parameter
              defines which of these should be solved for *first* -- the
              adjoint fixed-point iteration starts at `eqs[(len(eqs) - 1 -
              adjoint_eqs_index_0) % len(eqs)]`. An :class:`int`, and defaults
              to 0.

    :arg norm_sqs: Defines the squared norm used to test for convergence in a
        forward solve. See :class:`CustomNormSq`.
    :arg adj_norm_sqs: Defines the squared norm used to test for convergence in
        an adjoint solve. See :class:`CustomNormSq`.
    """

    def __init__(self, eqs, solver_parameters, *,
                 norm_sqs=None, adj_norm_sqs=None):
        X_ids = set()
        for eq in eqs:
            for x in eq.X():
                x_id = function_id(x)
                if x_id in X_ids:
                    raise ValueError("Duplicate solve")
                X_ids.add(x_id)

        solver_parameters = dict(solver_parameters)
        if "nonzero_adjoint_initial_guess" in solver_parameters:
            warnings.warn("nonzero_adjoint_initial_guess parameter is "
                          "deprecated -- use adjoint_nonzero_initial_guess "
                          "instead",
                          DeprecationWarning, stacklevel=2)
            if "adjoint_nonzero_initial_guess" in solver_parameters:
                raise ValueError("Cannot supply both "
                                 "nonzero_adjoint_initial_guess and "
                                 "adjoint_nonzero_initial_guess "
                                 "parameters")
            solver_parameters["adjoint_nonzero_initial_guess"] = \
                solver_parameters.pop("nonzero_adjoint_initial_guess")
        # Based on KrylovSolver parameters in FEniCS 2017.2.0
        for key, default_value in [("maximum_iterations", 1000),
                                   ("nonzero_initial_guess", True),
                                   ("adjoint_nonzero_initial_guess", True),
                                   ("adjoint_eqs_index_0", 0)]:
            solver_parameters.setdefault(key, default_value)

        nonzero_initial_guess = solver_parameters["nonzero_initial_guess"]
        adjoint_nonzero_initial_guess = \
            solver_parameters["adjoint_nonzero_initial_guess"]

        X = []
        deps = []
        dep_ids = {}
        nl_deps = []
        nl_dep_ids = {}
        adj_X_type = []

        eq_X_indices = tuple([] for eq in eqs)
        eq_dep_indices = tuple([] for eq in eqs)
        eq_nl_dep_indices = tuple([] for eq in eqs)

        for i, eq in enumerate(eqs):
            eq_X = eq.X()
            eq_adj_X_type = eq.adj_X_type()
            assert len(eq_X) == len(eq_adj_X_type)
            for x, adj_x_type in zip(eq_X, eq_adj_X_type):
                X.append(x)
                eq_X_indices[i].append(len(X) - 1)
                adj_X_type.append(adj_x_type)
            del eq_X, eq_adj_X_type

            for dep in eq.dependencies():
                dep_id = function_id(dep)
                if dep_id not in dep_ids:
                    deps.append(dep)
                    dep_ids[dep_id] = len(deps) - 1
                eq_dep_indices[i].append(dep_ids[dep_id])

            for dep in eq.nonlinear_dependencies():
                dep_id = function_id(dep)
                if dep_id not in nl_dep_ids:
                    nl_deps.append(dep)
                    nl_dep_ids[dep_id] = len(nl_deps) - 1
                eq_nl_dep_indices[i].append(nl_dep_ids[dep_id])

        del dep_ids, nl_dep_ids

        if nonzero_initial_guess:
            ic_deps = {}
            previous_x_ids = set()
            remaining_x_ids = set(X_ids)

            for i, eq in enumerate(eqs):
                for x in eq.X():
                    remaining_x_ids.remove(function_id(x))

                for dep in eq.dependencies():
                    dep_id = function_id(dep)
                    if dep_id in remaining_x_ids and dep_id not in ic_deps:
                        ic_deps[dep_id] = dep

                for dep in eq.initial_condition_dependencies():
                    dep_id = function_id(dep)
                    assert dep_id not in previous_x_ids
                    if dep_id not in ic_deps:
                        ic_deps[dep_id] = dep

                for x in eq.X():
                    previous_x_ids.add(function_id(x))

            ic_deps = list(ic_deps.values())
            del previous_x_ids, remaining_x_ids
        else:
            ic_deps = []

        if adjoint_nonzero_initial_guess:
            adj_ic_deps = {}
            previous_x_ids = set()
            remaining_x_ids = set(X_ids)

            adjoint_i0 = solver_parameters["adjoint_eqs_index_0"]
            for i in range(len(eqs) - 1, -1, -1):
                i = (i - adjoint_i0) % len(eqs)
                eq = eqs[i]

                for x in eq.X():
                    remaining_x_ids.remove(function_id(x))

                for dep in eq.dependencies():
                    dep_id = function_id(dep)
                    if dep_id in remaining_x_ids \
                            and dep_id not in adj_ic_deps:
                        adj_ic_deps[dep_id] = dep

                for dep in eq.adjoint_initial_condition_dependencies():
                    dep_id = function_id(dep)
                    assert dep_id not in previous_x_ids
                    if dep_id not in adj_ic_deps:
                        adj_ic_deps[dep_id] = dep

                for x in eq.X():
                    previous_x_ids.add(function_id(x))

            adj_ic_deps = list(adj_ic_deps.values())
            del previous_x_ids, remaining_x_ids
        else:
            adj_ic_deps = []

        eq_dep_index_map = tuple(
            {function_id(dep): i for i, dep in enumerate(eq.dependencies())}
            for eq in eqs)

        dep_eq_index_map = {}
        for i, eq in enumerate(eqs):
            for dep in eq.dependencies():
                dep_id = function_id(dep)
                if dep_id in dep_eq_index_map:
                    dep_eq_index_map[dep_id].append(i)
                else:
                    dep_eq_index_map[dep_id] = [i]

        dep_map = {}
        for k, eq in enumerate(eqs):
            for m, x in enumerate(eq.X()):
                dep_map[function_id(x)] = (k, m)
        dep_B_indices = tuple({} for eq in eqs)
        for i, eq in enumerate(eqs):
            for j, dep in enumerate(eq.dependencies()):
                dep_id = function_id(dep)
                if dep_id in dep_map:
                    k, m = dep_map[dep_id]
                    if k != i:
                        dep_B_indices[i][j] = (k, m)
        del dep_map

        Equation.__init__(self, X, deps, nl_deps=nl_deps,
                          ic_deps=ic_deps, adj_ic_deps=adj_ic_deps,
                          adj_type=adj_X_type)
        CustomNormSq.__init__(self, eqs,
                              norm_sqs=norm_sqs, adj_norm_sqs=adj_norm_sqs)
        self._eqs = tuple(eqs)
        self._eq_X_indices = eq_X_indices
        self._eq_dep_indices = eq_dep_indices
        self._eq_nl_dep_indices = eq_nl_dep_indices
        self._eq_dep_index_map = eq_dep_index_map
        self._dep_eq_index_map = dep_eq_index_map
        self._dep_B_indices = dep_B_indices
        self._solver_parameters = solver_parameters

        self.add_referrer(*eqs)

    def drop_references(self):
        super().drop_references()
        self._eqs = tuple(WeakAlias(eq) for eq in self._eqs)

    def forward_solve(self, X, deps=None):
        if is_function(X):
            X = (X,)

        # Based on KrylovSolver parameters in FEniCS 2017.2.0
        absolute_tolerance = self._solver_parameters["absolute_tolerance"]
        relative_tolerance = self._solver_parameters["relative_tolerance"]
        maximum_iterations = self._solver_parameters["maximum_iterations"]
        nonzero_initial_guess = \
            self._solver_parameters["nonzero_initial_guess"]
        logger = logging.getLogger("tlm_adjoint.FixedPointSolver")

        eq_X = tuple(tuple(X[j] for j in self._eq_X_indices[i])
                     for i in range(len(self._eqs)))
        if deps is None:
            eq_deps = tuple(None for i in range(len(self._eqs)))
        else:
            eq_deps = tuple(tuple(deps[j] for j in self._eq_dep_indices[i])
                            for i in range(len(self._eqs)))

        if not nonzero_initial_guess:
            for x in X:
                function_zero(x)
            function_update_caches(*self.X(), value=X)

        it = 0
        X_0 = tuple(tuple(function_copy(x) for x in eq_X[i])
                    for i in range(len(self._eqs)))
        while True:
            it += 1

            for i, eq in enumerate(self._eqs):
                eq.forward(eq_X[i], deps=eq_deps[i])

            R = X_0
            del X_0
            for i in range(len(self._eqs)):
                assert len(R[i]) == len(eq_X[i])
                for r, x in zip(R[i], eq_X[i]):
                    function_axpy(r, -1.0, x)
            R_norm_sq = self._forward_norm_sq(R)
            if relative_tolerance == 0.0:
                tolerance_sq = absolute_tolerance ** 2
            else:
                X_norm_sq = self._forward_norm_sq(eq_X)
                tolerance_sq = max(absolute_tolerance ** 2,
                                   X_norm_sq * (relative_tolerance ** 2))
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Fixed point iteration, "
                             f"forward iteration {it:d}, "
                             f"change norm {np.sqrt(R_norm_sq):.16e} "
                             f"(tolerance {np.sqrt(tolerance_sq):.16e})")
            if np.isnan(R_norm_sq):
                raise RuntimeError(
                    f"Fixed point iteration, forward iteration {it:d}, "
                    f"NaN encountered")
            if R_norm_sq < tolerance_sq or R_norm_sq == 0.0:
                break
            if it >= maximum_iterations:
                raise RuntimeError(
                    f"Fixed point iteration, forward iteration {it:d}, "
                    f"failed to converge")

            X_0 = R
            del R
            for i in range(len(self._eqs)):
                assert len(X_0[i]) == len(eq_X[i])
                for x_0, x in zip(X_0[i], eq_X[i]):
                    function_assign(x_0, x)

    _reset_adjoint_warning = False

    def reset_adjoint(self):
        for eq in self._eqs:
            eq.reset_adjoint()

    _initialize_adjoint_warning = False

    def initialize_adjoint(self, J, nl_deps):
        for i, eq in enumerate(self._eqs):
            eq_nl_deps = tuple(nl_deps[j] for j in self._eq_nl_dep_indices[i])
            eq.initialize_adjoint(J, eq_nl_deps)

    _finalize_adjoint_warning = False

    def finalize_adjoint(self, J):
        for eq in self._eqs:
            eq.finalize_adjoint(J)

    def adjoint_jacobian_solve(self, adj_X, nl_deps, B):
        if is_function(B):
            B = (B,)
        if adj_X is None:
            adj_X = list(self.new_adj_X())
        elif is_function(adj_X):
            adj_X = [adj_X]
        else:
            adj_X = list(adj_X)

        # Based on KrylovSolver parameters in FEniCS 2017.2.0
        absolute_tolerance = self._solver_parameters["absolute_tolerance"]
        relative_tolerance = self._solver_parameters["relative_tolerance"]
        maximum_iterations = self._solver_parameters["maximum_iterations"]

        nonzero_initial_guess = self._solver_parameters["adjoint_nonzero_initial_guess"]  # noqa: E501
        adjoint_i0 = self._solver_parameters["adjoint_eqs_index_0"]
        logger = logging.getLogger("tlm_adjoint.FixedPointSolver")

        eq_adj_X = [tuple(adj_X[j] for j in self._eq_X_indices[i])
                    for i in range(len(self._eqs))]
        eq_nl_deps = tuple(tuple(nl_deps[j] for j in nl_dep_indices)
                           for nl_dep_indices in self._eq_nl_dep_indices)
        adj_B = AdjointModelRHS([self._eqs])

        dep_Bs = tuple({} for eq in self._eqs)
        for i, eq in enumerate(self._eqs):
            eq_B = adj_B[0][i].B()
            for j, k in enumerate(self._eq_X_indices[i]):
                function_assign(eq_B[j], B[k])
            for j, (k, m) in self._dep_B_indices[i].items():
                dep_Bs[i][j] = adj_B[0][k][m]

        if nonzero_initial_guess:
            for i, eq in enumerate(self._eqs):
                eq.subtract_adjoint_derivative_actions(
                    eq_adj_X[i][0] if len(eq_adj_X[i]) == 1 else eq_adj_X[i],
                    eq_nl_deps[i], dep_Bs[i])
        else:
            for adj_x in adj_X:
                function_zero(adj_x)

        it = 0
        X_0 = tuple(tuple(function_copy(x) for x in eq_adj_X[i])
                    for i in range(len(self._eqs)))
        while True:
            it += 1

            for i in range(len(self._eqs) - 1, - 1, -1):
                i = (i - adjoint_i0) % len(self._eqs)
                # Copy required here, as adjoint_jacobian_solve may return the
                # RHS function itself
                eq_B = adj_B[0][i].B(copy=True)

                eq_adj_X[i] = self._eqs[i].adjoint_jacobian_solve(
                    eq_adj_X[i][0] if len(eq_adj_X[i]) == 1 else eq_adj_X[i],
                    eq_nl_deps[i],
                    eq_B[0] if len(eq_B) == 1 else eq_B)

                if eq_adj_X[i] is None:
                    eq_adj_X[i] = self._eqs[i].new_adj_X()
                else:
                    if is_function(eq_adj_X[i]):
                        eq_adj_X[i] = (eq_adj_X[i],)
                    self._eqs[i].subtract_adjoint_derivative_actions(
                        eq_adj_X[i][0] if len(eq_adj_X[i]) == 1 else eq_adj_X[i],  # noqa: E501
                        eq_nl_deps[i], dep_Bs[i])

                assert len(self._eq_X_indices[i]) == len(eq_adj_X[i])
                for j, x in zip(self._eq_X_indices[i], eq_adj_X[i]):
                    adj_X[j] = x

                eq_B = adj_B[0][i].B()
                for j, k in enumerate(self._eq_X_indices[i]):
                    function_assign(eq_B[j], B[k])

            R = X_0
            del X_0
            for i in range(len(self._eqs)):
                assert len(R[i]) == len(eq_adj_X[i])
                for r, x in zip(R[i], eq_adj_X[i]):
                    function_axpy(r, -1.0, x)
            R_norm_sq = self._adjoint_norm_sq(R)
            if relative_tolerance == 0.0:
                tolerance_sq = absolute_tolerance ** 2
            else:
                X_norm_sq = self._adjoint_norm_sq(eq_adj_X)
                tolerance_sq = max(absolute_tolerance ** 2,
                                   X_norm_sq * (relative_tolerance ** 2))
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"Fixed point iteration, "
                             f"adjoint iteration {it:d}, "
                             f"change norm {np.sqrt(R_norm_sq):.16e} "
                             f"(tolerance {np.sqrt(tolerance_sq):.16e})")
            if np.isnan(R_norm_sq):
                raise RuntimeError(
                    f"Fixed point iteration, adjoint iteration {it:d}, "
                    f"NaN encountered")
            if R_norm_sq < tolerance_sq or R_norm_sq == 0.0:
                break
            if it >= maximum_iterations:
                raise RuntimeError(
                    f"Fixed point iteration, adjoint iteration {it:d}, "
                    f"failed to converge")

            X_0 = R
            del R
            for i in range(len(self._eqs)):
                assert len(X_0[i]) == len(eq_adj_X[i])
                for x_0, x in zip(X_0[i], eq_adj_X[i]):
                    function_assign(x_0, x)

        return adj_X

    def subtract_adjoint_derivative_actions(self, adj_X, nl_deps, dep_Bs):
        if is_function(adj_X):
            adj_X = (adj_X,)

        eq_dep_Bs = tuple({} for eq in self._eqs)
        for dep_index, B in dep_Bs.items():
            dep = self.dependencies()[dep_index]
            dep_id = function_id(dep)
            for i in self._dep_eq_index_map[dep_id]:
                eq_dep_Bs[i][self._eq_dep_index_map[i][dep_id]] = B

        for i, eq in enumerate(self._eqs):
            eq_adj_X = tuple(adj_X[j] for j in self._eq_X_indices[i])
            eq_nl_deps = tuple(nl_deps[j] for j in self._eq_nl_dep_indices[i])
            eq.subtract_adjoint_derivative_actions(
                eq_adj_X[0] if len(eq_adj_X) == 1 else eq_adj_X,
                eq_nl_deps, eq_dep_Bs[i])

    def tangent_linear(self, M, dM, tlm_map):
        tlm_eqs = []
        for eq in self._eqs:
            tlm_eq = eq.tangent_linear(M, dM, tlm_map)
            if tlm_eq is None:
                warnings.warn("Equation.tangent_linear should return an "
                              "Equation",
                              DeprecationWarning)
                tlm_eq = ZeroAssignment([tlm_map[x] for x in eq.X()])
            tlm_eqs.append(tlm_eq)
        return FixedPointSolver(
            tlm_eqs, solver_parameters=self._solver_parameters,
            norm_sqs=self._norm_sqs, adj_norm_sqs=self._adj_norm_sqs)
