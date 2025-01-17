#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""This module is used by both the FEniCS and Firedrake backends, and
implements finite element calculations. In particular the
:class:`EquationSolver` class implements the solution of finite element
variational problems.
"""

from .backend import (
    TestFunction, TrialFunction, adjoint, backend_DirichletBC,
    backend_Function, parameters)
from ..interface import (
    check_space_type, function_assign, function_id, function_is_scalar,
    function_new, function_new_conjugate_dual, function_replacement,
    function_scalar_value, function_space, function_update_caches,
    function_zero, is_function)
from .backend_code_generator_interface import (
    assemble, assemble_linear_solver, copy_parameters_dict,
    form_form_compiler_parameters, function_vector, homogenize,
    interpolate_expression, matrix_multiply, process_adjoint_solver_parameters,
    process_solver_parameters, rhs_addto, rhs_copy, solve,
    update_parameters_dict, verify_assembly)

from ..caches import CacheRef
from ..equation import Equation, ZeroAssignment
from ..equations import Assignment
from ..overloaded_float import SymbolicFloat
from ..tangent_linear import get_tangent_linear

from .caches import assembly_cache, is_cached, linear_solver_cache, split_form
from .functions import (
    bcs_is_cached, bcs_is_homogeneous, bcs_is_static, derivative, diff,
    eliminate_zeros, extract_coefficients)

import numpy as np
import ufl
import warnings

__all__ = \
    [
        "Assembly",
        "DirichletBCApplication",
        "EquationSolver",
        "ExprInterpolation",
        "Projection",
        "expr_new_x",
        "linear_equation_new_x",

        "AssembleSolver",
        "DirichletBCSolver",
        "ExprEvaluation",
        "ExprEvaluationSolver",
        "ProjectionSolver"
    ]


def derivative_dependencies(expr, dep):
    dexpr = derivative(expr, dep, enable_automatic_argument=False)
    dexpr = ufl.algorithms.expand_derivatives(dexpr)
    return extract_coefficients(dexpr)


def extract_dependencies(expr, *,
                         space_type="primal"):
    deps = {}
    nl_deps = {}
    for dep in extract_coefficients(expr):
        if is_function(dep):
            deps.setdefault(function_id(dep), dep)
            for nl_dep in derivative_dependencies(expr, dep):
                if is_function(nl_dep):
                    nl_deps.setdefault(function_id(dep), dep)
                    nl_deps.setdefault(function_id(nl_dep), nl_dep)

    deps = {dep_id: deps[dep_id]
            for dep_id in sorted(deps.keys())}
    nl_deps = {nl_dep_id: nl_deps[nl_dep_id]
               for nl_dep_id in sorted(nl_deps.keys())}

    assert len(set(nl_deps.keys()).difference(set(deps.keys()))) == 0
    for dep in deps.values():
        check_space_type(dep, space_type)

    return deps, nl_deps


def apply_rhs_bcs(b, hbcs, *, b_bc=None):
    for bc in hbcs:
        bc.apply(b)
    if b_bc is not None:
        rhs_addto(b, b_bc)


class ExprEquation(Equation):
    def _replace_map(self, deps):
        eq_deps = self.dependencies()
        assert len(eq_deps) == len(deps)
        return {eq_dep: dep
                for eq_dep, dep in zip(eq_deps, deps)
                if not isinstance(eq_dep, SymbolicFloat)}

    def _replace(self, expr, deps):
        return ufl.replace(expr, self._replace_map(deps))

    def _nonlinear_replace_map(self, nl_deps):
        eq_nl_deps = self.nonlinear_dependencies()
        assert len(eq_nl_deps) == len(nl_deps)
        return {eq_nl_dep: nl_dep
                for eq_nl_dep, nl_dep in zip(eq_nl_deps, nl_deps)
                if not isinstance(eq_nl_dep, SymbolicFloat)}

    def _nonlinear_replace(self, expr, nl_deps):
        return ufl.replace(expr, self._nonlinear_replace_map(nl_deps))


class Assembly(ExprEquation):
    r"""Represents assignment to the result of finite element assembly:

    .. code-block:: python

        x = assemble(rhs)

    The forward residual :math:`\mathcal{F}` is defined so that :math:`\partial
    \mathcal{F} / \partial x` is the identity.

    :arg x: A function defining the forward solution.
    :arg rhs: A UFL :class:`Form` to assemble. Should have arity 0 or 1, and
        should not depend on the forward solution.
    :arg form_compiler_parameters: Form compiler parameters.
    :arg match_quadrature: Whether to set quadrature parameters consistently in
        the forward, adjoint, and tangent-linears. Defaults to
        `parameters['tlm_adjoint']['Assembly']['match_quadrature']`.
    """

    def __init__(self, x, rhs, *,
                 form_compiler_parameters=None, match_quadrature=None):
        if form_compiler_parameters is None:
            form_compiler_parameters = {}
        if match_quadrature is None:
            match_quadrature = parameters["tlm_adjoint"]["Assembly"]["match_quadrature"]  # noqa: E501

        rhs = ufl.classes.Form(rhs.integrals())

        arity = len(rhs.arguments())
        if arity == 0:
            check_space_type(x, "primal")
            if not function_is_scalar(x):
                raise ValueError("Arity 0 forms can only be assigned to "
                                 "scalars")
        elif arity == 1:
            check_space_type(x, "conjugate_dual")
        else:
            raise ValueError("Must be an arity 0 or arity 1 form")

        deps, nl_deps = extract_dependencies(rhs)
        if function_id(x) in deps:
            raise ValueError("Invalid non-linear dependency")
        deps, nl_deps = list(deps.values()), tuple(nl_deps.values())
        deps.insert(0, x)

        form_compiler_parameters_ = \
            copy_parameters_dict(parameters["form_compiler"])
        update_parameters_dict(form_compiler_parameters_,
                               form_compiler_parameters)
        form_compiler_parameters = form_compiler_parameters_
        if match_quadrature:
            update_parameters_dict(
                form_compiler_parameters,
                form_form_compiler_parameters(rhs, form_compiler_parameters))

        super().__init__(x, deps, nl_deps=nl_deps, ic=False, adj_ic=False)
        self._rhs = rhs
        self._form_compiler_parameters = form_compiler_parameters
        self._arity = arity

    def drop_references(self):
        replace_map = {dep: function_replacement(dep)
                       for dep in self.dependencies()
                       if not isinstance(dep, SymbolicFloat)}

        super().drop_references()

        self._rhs = ufl.replace(self._rhs, replace_map)

    def forward_solve(self, x, deps=None):
        if deps is None:
            rhs = self._rhs
        else:
            rhs = self._replace(self._rhs, deps)

        if self._arity == 0:
            function_assign(
                x,
                assemble(rhs, form_compiler_parameters=self._form_compiler_parameters))  # noqa: E501
        elif self._arity == 1:
            assemble(
                rhs, form_compiler_parameters=self._form_compiler_parameters,
                tensor=function_vector(x))
        else:
            raise ValueError("Must be an arity 0 or arity 1 form")

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_x):
        # Derived from EquationSolver.derivative_action (see dolfin-adjoint
        # reference below). Code first added 2017-12-07.
        # Re-written 2018-01-28
        # Updated to adjoint only form 2018-01-29

        eq_deps = self.dependencies()
        if dep_index < 0 or dep_index >= len(eq_deps):
            raise IndexError("dep_index out of bounds")
        elif dep_index == 0:
            return adj_x

        dep = eq_deps[dep_index]
        dF = derivative(self._rhs, dep)
        dF = ufl.algorithms.expand_derivatives(dF)
        dF = eliminate_zeros(dF)
        if dF.empty():
            return None

        dF = self._nonlinear_replace(dF, nl_deps)
        if self._arity == 0:
            dF = ufl.classes.Form(
                [integral.reconstruct(integrand=ufl.conj(integral.integrand()))
                 for integral in dF.integrals()])  # dF = adjoint(dF)
            dF = assemble(
                dF, form_compiler_parameters=self._form_compiler_parameters)
            return (-function_scalar_value(adj_x), dF)
        elif self._arity == 1:
            dF = ufl.action(adjoint(dF), coefficient=adj_x)
            dF = assemble(
                dF, form_compiler_parameters=self._form_compiler_parameters)
            return (-1.0, dF)
        else:
            raise ValueError("Must be an arity 0 or arity 1 form")

    def adjoint_jacobian_solve(self, adj_x, nl_deps, b):
        return b

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
            return Assembly(
                tlm_map[x], tlm_rhs,
                form_compiler_parameters=self._form_compiler_parameters)


class AssembleSolver(Assembly):
    ""

    def __init__(self, rhs, x, form_compiler_parameters=None,
                 match_quadrature=None):
        warnings.warn("AssembleSolver is deprecated -- "
                      "use Assembly instead, and transfer AssembleSolver "
                      "global parameters",
                      DeprecationWarning, stacklevel=2)
        if match_quadrature is None:
            match_quadrature = parameters["tlm_adjoint"]["AssembleSolver"]["match_quadrature"]  # noqa: E501
        super().__init__(
            x, rhs,
            form_compiler_parameters=form_compiler_parameters,
            match_quadrature=match_quadrature)


def unbound_form(form, deps):
    replacement_deps = tuple(map(function_replacement, deps))
    assert len(deps) == len(replacement_deps)
    replaced_form = ufl.replace(form, dict(zip(deps, replacement_deps)))
    replaced_form._cache["_tlm_adjoint__replacement_deps"] = replacement_deps
    return replaced_form


def bind_form(form, deps):
    replacement_deps = form._cache["_tlm_adjoint__replacement_deps"]
    assert len(replacement_deps) == len(deps)
    form._cache["_tlm_adjoint__bindings"] = dict(zip(replacement_deps, deps))


def unbind_form(form):
    form._cache.pop("_tlm_adjoint__bindings", None)


def homogenized_bc(bc):
    if bcs_is_homogeneous(bc):
        return bc
    else:
        hbc = homogenize(bc)
        hbc._tlm_adjoint__static = bcs_is_static(bc)
        hbc._tlm_adjoint__cache = bcs_is_cached(bc)
        hbc._tlm_adjoint__homogeneous = True
        return hbc


class EquationSolver(ExprEquation):
    """Represents the solution of a finite element variational problem.

    Caching is based on the approach described in

        - J. R. Maddison and P. E. Farrell, 'Rapid development and adjoining of
          transient finite element models', Computer Methods in Applied
          Mechanics and Engineering, 276, 95--121, 2014, doi:
          10.1016/j.cma.2014.03.010

    The arguments `eq`, `x`, `bcs`, `J`, `form_compiler_parameters`, and
    `solver_parameters` are based on the interface for the FEniCS :func:`solve`
    function (see e.g. FEniCS 2017.1.0).

    :arg eq: A UFL :class:`Equation` defining the finite element variational
        problem.
    :arg x: A function defining the forward solution.
    :arg bcs: Dirichlet boundary conditions.
    :arg J: A UFL :class:`Form` defining a Jacobian matrix approximation to use
        in a non-linear forward solve.
    :arg form_compiler_parameters: Form compiler parameters.
    :arg solver_parameters: Linear or non-linear solver parameters.
    :arg adjoint_solver_parameters: Linear solver parameters to use in an
        adjoint solve.
    :arg tlm_solver_parameters: Linear solver parameters to use when solving
        tangent-linear problems.
    :arg initial_guess: Deprecated.
    :arg cache_jacobian: Whether to cache the forward Jacobian matrix and
        linear solver data. Defaults to
        `parameters['tlm_adjoint']['EquationSolver]['cache_jacobian']`. If
        `None` then caching is autodetected.
    :arg cache_adjoint_jacobian: Whether to cache the adjoint Jacobian matrix
        and linear solver data. Defaults to `cache_jacobian`.
    :arg cache_tlm_jacobian: Whether to cache the Jacobian matrix and linear
        solver data when solving tangent-linear problems. Defaults to
        `cache_jacobian`.
    :arg cache_rhs_assembly: Whether to enable right-hand-side caching. If
        enabled then right-hand-side terms are divided into terms which are
        cached, terms which are converted into matrix multiplication by a
        cached matrix, and terms which are not cached. Defaults to
        `parameters['tlm_adjoint']['EquationSolver']['cache_rhs_assembly']`.
    :arg match_quadrature: Whether to set quadrature parameters consistently in
        the forward, adjoint, and tangent-linears. Defaults to
        `parameters['tlm_adjoint']['EquationSolver']['match_quadrature']`.
    :arg defer_adjoint_assembly: Whether to use 'deferred' adjoint assembly. If
        adjoint assembly is deferred then initially only symbolic expressions
        for adjoint right-hand-side terms are constructed. Finite element
        assembly can occur later (with default form compiler parameters), when
        further adjoint right-hand-side terms are available. Defaults to
        `parameters['tlm_adjoint']['EquationSolver']['defer_adjoint_assembly']`.
    """

    def __init__(self, eq, x, bcs=None, *,
                 J=None, form_compiler_parameters=None, solver_parameters=None,
                 adjoint_solver_parameters=None, tlm_solver_parameters=None,
                 initial_guess=None, cache_jacobian=None,
                 cache_adjoint_jacobian=None, cache_tlm_jacobian=None,
                 cache_rhs_assembly=None, match_quadrature=None,
                 defer_adjoint_assembly=None):
        if bcs is None:
            bcs = []
        if form_compiler_parameters is None:
            form_compiler_parameters = {}
        if solver_parameters is None:
            solver_parameters = {}

        if isinstance(bcs, backend_DirichletBC):
            bcs = (bcs,)
        else:
            bcs = tuple(bcs)
        if cache_jacobian is None:
            if not parameters["tlm_adjoint"]["EquationSolver"]["enable_jacobian_caching"]:  # noqa: E501
                cache_jacobian = False
        if cache_rhs_assembly is None:
            cache_rhs_assembly = parameters["tlm_adjoint"]["EquationSolver"]["cache_rhs_assembly"]  # noqa: E501
        if match_quadrature is None:
            match_quadrature = parameters["tlm_adjoint"]["EquationSolver"]["match_quadrature"]  # noqa: E501
        if defer_adjoint_assembly is None:
            defer_adjoint_assembly = parameters["tlm_adjoint"]["EquationSolver"]["defer_adjoint_assembly"]  # noqa: E501
        if match_quadrature and defer_adjoint_assembly:
            raise ValueError("Cannot both match quadrature and defer adjoint "
                             "assembly")

        check_space_type(x, "primal")

        lhs, rhs = eq.lhs, eq.rhs
        del eq
        lhs = ufl.classes.Form(lhs.integrals())
        linear = isinstance(rhs, ufl.classes.Form)
        if linear:
            rhs = ufl.classes.Form(rhs.integrals())
        if J is not None:
            J = ufl.classes.Form(J.integrals())

        if linear:
            if len(lhs.arguments()) != 2:
                raise ValueError("Unexpected number of left-hand-side "
                                 "arguments")
            if rhs.arguments() != (lhs.arguments()[0],):
                raise ValueError("Invalid right-hand-side arguments")
            if x in extract_coefficients(lhs) \
                    or x in extract_coefficients(rhs):
                raise ValueError("Invalid non-linear dependency")

            F = ufl.action(lhs, coefficient=x) - rhs
            nl_solve_J = None
            J = lhs
        else:
            if len(lhs.arguments()) != 1:
                raise ValueError("Unexpected number of left-hand-side "
                                 "arguments")
            if rhs != 0:
                raise ValueError("Invalid right-hand-side")

            F = lhs
            nl_solve_J = J
            J = derivative(F, x)
            J = ufl.algorithms.expand_derivatives(J)

        deps, nl_deps = extract_dependencies(F)
        if nl_solve_J is not None:
            for dep in extract_coefficients(nl_solve_J):
                if is_function(dep):
                    dep_id = function_id(dep)
                    if dep_id not in deps:
                        deps[dep_id] = dep

        if initial_guess is not None:
            warnings.warn("initial_guess argument is deprecated",
                          DeprecationWarning, stacklevel=2)
            if initial_guess == x:
                initial_guess = None
            else:
                initial_guess_id = function_id(initial_guess)
                if initial_guess_id not in deps:
                    deps[initial_guess_id] = initial_guess

        deps = list(deps.values())
        if x in deps:
            deps.remove(x)
        deps.insert(0, x)
        nl_deps = tuple(nl_deps.values())

        hbcs = tuple(homogenized_bc(bc) for bc in bcs)

        if cache_jacobian is None:
            cache_jacobian = is_cached(J) and bcs_is_cached(bcs)
        if cache_adjoint_jacobian is None:
            cache_adjoint_jacobian = cache_jacobian
        if cache_tlm_jacobian is None:
            cache_tlm_jacobian = cache_jacobian

        (solver_parameters, linear_solver_parameters,
         ic, J_ic) = process_solver_parameters(solver_parameters, linear)

        if adjoint_solver_parameters is None:
            adjoint_solver_parameters = process_adjoint_solver_parameters(linear_solver_parameters)  # noqa: E501
            adj_ic = J_ic
        else:
            (_, adjoint_solver_parameters,
             adj_ic, _) = process_solver_parameters(adjoint_solver_parameters, linear=True)  # noqa: E501

        if tlm_solver_parameters is not None:
            (_, tlm_solver_parameters,
             _, _) = process_solver_parameters(tlm_solver_parameters, linear=True)  # noqa: E501

        form_compiler_parameters_ = copy_parameters_dict(parameters["form_compiler"])  # noqa: E501
        update_parameters_dict(form_compiler_parameters_,
                               form_compiler_parameters)
        form_compiler_parameters = form_compiler_parameters_
        if match_quadrature:
            update_parameters_dict(
                form_compiler_parameters,
                form_form_compiler_parameters(F, form_compiler_parameters))

        super().__init__(x, deps, nl_deps=nl_deps,
                         ic=initial_guess is None and ic,
                         adj_ic=adj_ic, adj_type="primal")
        self._F = F
        self._lhs, self._rhs = lhs, rhs
        self._bcs = bcs
        self._hbcs = hbcs
        self._J = J
        self._nl_solve_J = nl_solve_J
        self._form_compiler_parameters = form_compiler_parameters
        self._solver_parameters = solver_parameters
        self._linear_solver_parameters = linear_solver_parameters
        self._adjoint_solver_parameters = adjoint_solver_parameters
        self._tlm_solver_parameters = tlm_solver_parameters
        if initial_guess is None:
            self._initial_guess_index = None
        else:
            self._initial_guess_index = deps.index(initial_guess)
        self._linear = linear

        self._cache_jacobian = cache_jacobian
        self._cache_adjoint_jacobian = cache_adjoint_jacobian
        self._cache_tlm_jacobian = cache_tlm_jacobian
        self._cache_rhs_assembly = cache_rhs_assembly
        self._defer_adjoint_assembly = defer_adjoint_assembly

        self._forward_eq = None
        self._forward_J_solver = CacheRef()
        self._forward_b_pa = None

        self._adjoint_dF_cache = {}
        self._adjoint_action_cache = {}

        self._adjoint_J_solver = CacheRef()
        self._adjoint_J = None

    def drop_references(self):
        replace_map = {dep: function_replacement(dep)
                       for dep in self.dependencies()}

        super().drop_references()

        self._F = ufl.replace(self._F, replace_map)
        self._lhs = ufl.replace(self._lhs, replace_map)
        if self._rhs != 0:
            self._rhs = ufl.replace(self._rhs, replace_map)
        self._J = ufl.replace(self._J, replace_map)
        if self._nl_solve_J is not None:
            self._nl_solve_J = ufl.replace(self._nl_solve_J, replace_map)

        if self._forward_b_pa is not None:
            cached_form, mat_forms, non_cached_form = self._forward_b_pa

            if cached_form is not None:
                cached_form[0] = ufl.replace(cached_form[0], replace_map)
            for dep_index, (mat_form, mat_cache) in mat_forms.items():
                mat_forms[dep_index][0] = ufl.replace(mat_form, replace_map)

            # self._forward_b_pa = (cached_form, mat_forms, non_cached_form)

        for dep_index, dF in self._adjoint_dF_cache.items():
            if dF is not None:
                self._adjoint_dF_cache[dep_index] = ufl.replace(dF, replace_map)  # noqa: E501

    def _cached_rhs(self, deps, *, b_bc=None):
        eq_deps = self.dependencies()

        if self._forward_b_pa is None:
            rhs = eliminate_zeros(self._rhs, force_non_empty_form=True)
            cached_form, mat_forms_, non_cached_form = split_form(rhs)
            mat_forms = {}
            for dep_index, dep in enumerate(eq_deps):
                dep_id = function_id(dep)
                if dep_id in mat_forms_:
                    mat_forms[dep_index] = [mat_forms_[dep_id], CacheRef()]
            del mat_forms_

            if non_cached_form.empty():
                non_cached_form = None
            else:
                non_cached_form = unbound_form(non_cached_form, eq_deps)

            if cached_form.empty():
                cached_form = None
            else:
                cached_form = [cached_form, CacheRef()]

            self._forward_b_pa = (cached_form, mat_forms, non_cached_form)
        else:
            cached_form, mat_forms, non_cached_form = self._forward_b_pa

        b = None

        if non_cached_form is not None:
            bind_form(non_cached_form, eq_deps if deps is None else deps)
            b = assemble(
                non_cached_form,
                form_compiler_parameters=self._form_compiler_parameters)
            unbind_form(non_cached_form)

        for dep_index, (mat_form, mat_cache) in mat_forms.items():
            mat_bc = mat_cache()
            if mat_bc is None:
                mat_forms[dep_index][1], mat_bc = assembly_cache().assemble(
                    mat_form,
                    form_compiler_parameters=self._form_compiler_parameters,
                    linear_solver_parameters=self._linear_solver_parameters,
                    replace_map=None if deps is None else self._replace_map(deps))  # noqa: E501
            mat, _ = mat_bc
            dep = (eq_deps if deps is None else deps)[dep_index]
            if b is None:
                b = matrix_multiply(mat, function_vector(dep))
            else:
                matrix_multiply(mat, function_vector(dep), tensor=b,
                                addto=True)

        if cached_form is not None:
            cached_b = cached_form[1]()
            if cached_b is None:
                cached_form[1], cached_b = assembly_cache().assemble(
                    cached_form[0],
                    form_compiler_parameters=self._form_compiler_parameters,
                    replace_map=None if deps is None else self._replace_map(deps))  # noqa: E501
            if b is None:
                b = rhs_copy(cached_b)
            else:
                rhs_addto(b, cached_b)

        if b is None:
            b = function_vector(function_new_conjugate_dual(self.x()))

        apply_rhs_bcs(b, self._hbcs, b_bc=b_bc)
        return b

    def forward_solve(self, x, deps=None):
        eq_deps = self.dependencies()

        if self._initial_guess_index is not None:
            if deps is None:
                initial_guess = eq_deps[self._initial_guess_index]
            else:
                initial_guess = deps[self._initial_guess_index]
            function_assign(x, initial_guess)
            function_update_caches(self.x(), value=x)

        if self._linear:
            if self._cache_jacobian:
                # Cases 1 and 2: Linear, Jacobian cached, with or without RHS
                # assembly caching

                J_solver_mat_bc = self._forward_J_solver()
                if J_solver_mat_bc is None:
                    # Assemble and cache the Jacobian, construct and cache the
                    # linear solver
                    self._forward_J_solver, J_solver_mat_bc = \
                        linear_solver_cache().linear_solver(
                            self._J, bcs=self._bcs,
                            form_compiler_parameters=self._form_compiler_parameters,  # noqa: E501
                            linear_solver_parameters=self._linear_solver_parameters,  # noqa: E501
                            replace_map=None if deps is None else self._replace_map(deps))  # noqa: E501
                J_solver, J_mat, b_bc = J_solver_mat_bc

                if self._cache_rhs_assembly:
                    # Assemble the RHS with RHS assembly caching
                    b = self._cached_rhs(deps, b_bc=b_bc)
                else:
                    # Assemble the RHS without RHS assembly caching
                    if deps is None:
                        rhs = self._rhs
                    else:
                        if self._forward_eq is None:
                            self._forward_eq = \
                                (None,
                                 None,
                                 unbound_form(self._rhs, eq_deps))
                        _, _, rhs = self._forward_eq
                        bind_form(rhs, deps)
                    b = assemble(
                        rhs,
                        form_compiler_parameters=self._form_compiler_parameters)  # noqa: E501
                    if deps is not None:
                        unbind_form(rhs)

                    # Add bc RHS terms
                    apply_rhs_bcs(b, self._hbcs, b_bc=b_bc)
            else:
                if self._cache_rhs_assembly:
                    # Case 3: Linear, Jacobian not cached, with RHS assembly
                    # caching

                    # Construct the linear solver, assemble the Jacobian
                    if deps is None:
                        J = self._J
                    else:
                        if self._forward_eq is None:
                            self._forward_eq = \
                                (None,
                                 unbound_form(self._J, eq_deps),
                                 None)
                        _, J, _ = self._forward_eq
                        bind_form(J, deps)
                    J_solver, J_mat, b_bc = assemble_linear_solver(
                        J, bcs=self._bcs,
                        form_compiler_parameters=self._form_compiler_parameters,  # noqa: E501
                        linear_solver_parameters=self._linear_solver_parameters)  # noqa: E501
                    if deps is not None:
                        unbind_form(J)

                    # Assemble the RHS with RHS assembly caching
                    b = self._cached_rhs(deps, b_bc=b_bc)
                else:
                    # Case 4: Linear, Jacobian not cached, without RHS assembly
                    # caching

                    # Construct the linear solver, assemble the Jacobian and
                    # RHS
                    if deps is None:
                        J, rhs = self._J, self._rhs
                    else:
                        if self._forward_eq is None:
                            self._forward_eq = \
                                (None,
                                 unbound_form(self._J, eq_deps),
                                 unbound_form(self._rhs, eq_deps))
                        _, J, rhs = self._forward_eq
                        bind_form(J, deps)
                        bind_form(rhs, deps)
                    J_solver, J_mat, b = assemble_linear_solver(
                        J, b_form=rhs, bcs=self._bcs,
                        form_compiler_parameters=self._form_compiler_parameters,  # noqa: E501
                        linear_solver_parameters=self._linear_solver_parameters)  # noqa: E501
                    if deps is not None:
                        unbind_form(J)
                        unbind_form(rhs)

            J_tolerance = parameters["tlm_adjoint"]["assembly_verification"]["jacobian_tolerance"]  # noqa: E501
            b_tolerance = parameters["tlm_adjoint"]["assembly_verification"]["rhs_tolerance"]  # noqa: E501
            if not np.isposinf(J_tolerance) or not np.isposinf(b_tolerance):
                verify_assembly(
                    self._J if deps is None
                    else self._replace(self._J, deps),
                    self._rhs if deps is None
                    else self._replace(self._rhs, deps),
                    J_mat, b, self._bcs, self._form_compiler_parameters,
                    self._linear_solver_parameters, J_tolerance, b_tolerance)

            J_solver.solve(function_vector(x), b)
        else:
            # Case 5: Non-linear
            assert self._rhs == 0
            lhs = self._lhs
            if self._nl_solve_J is None:
                J = self._J
            else:
                J = self._nl_solve_J
            if deps is not None:
                lhs = self._replace(lhs, deps)
                J = self._replace(J, deps)
            solve(lhs == 0, x, self._bcs, J=J,
                  form_compiler_parameters=self._form_compiler_parameters,
                  solver_parameters=self._solver_parameters)

    def subtract_adjoint_derivative_actions(self, adj_x, nl_deps, dep_Bs):
        for dep_index, dep_B in dep_Bs.items():
            if dep_index not in self._adjoint_dF_cache:
                dep = self.dependencies()[dep_index]
                dF = derivative(self._F, dep)
                dF = ufl.algorithms.expand_derivatives(dF)
                dF = eliminate_zeros(dF)
                if dF.empty():
                    dF = None
                else:
                    dF = adjoint(dF)
                self._adjoint_dF_cache[dep_index] = dF
            dF = self._adjoint_dF_cache[dep_index]

            if dF is not None:
                if dep_index not in self._adjoint_action_cache:
                    if self._cache_rhs_assembly \
                            and isinstance(adj_x, backend_Function) \
                            and is_cached(dF):
                        # Cached matrix action
                        self._adjoint_action_cache[dep_index] = CacheRef()
                    elif self._defer_adjoint_assembly:
                        # Cached form, deferred assembly
                        self._adjoint_action_cache[dep_index] = None
                    else:
                        # Cached form, immediate assembly
                        self._adjoint_action_cache[dep_index] = unbound_form(
                            ufl.action(dF, coefficient=adj_x),
                            list(self.nonlinear_dependencies()) + [adj_x])
                cache = self._adjoint_action_cache[dep_index]

                if cache is None:
                    # Cached form, deferred assembly
                    dep_B.sub(ufl.action(
                        self._nonlinear_replace(dF, nl_deps),
                        coefficient=adj_x))
                elif isinstance(cache, CacheRef):
                    # Cached matrix action
                    mat_bc = cache()
                    if mat_bc is None:
                        self._adjoint_action_cache[dep_index], (mat, _) = \
                            assembly_cache().assemble(
                                dF,
                                form_compiler_parameters=self._form_compiler_parameters,  # noqa: E501
                                replace_map=self._nonlinear_replace_map(nl_deps))  # noqa: E501
                    else:
                        mat, _ = mat_bc
                    dep_B.sub(matrix_multiply(mat, function_vector(adj_x)))
                else:
                    # Cached form, immediate assembly
                    assert isinstance(cache, ufl.classes.Form)
                    bind_form(cache, list(nl_deps) + [adj_x])
                    dep_B.sub(assemble(
                        cache,
                        form_compiler_parameters=self._form_compiler_parameters))  # noqa: E501
                    unbind_form(cache)

    # def adjoint_derivative_action(self, nl_deps, dep_index, adj_x):
    #     # Similar to 'RHS.derivative_action' and
    #     # 'RHS.second_derivative_action' in dolfin-adjoint file
    #     # dolfin_adjoint/adjrhs.py (see e.g. dolfin-adjoint version 2017.1.0)
    #     # Code first added to JRM personal repository 2016-05-22
    #     # Code first added to dolfin_adjoint_custom repository 2016-06-02
    #     # Re-written 2018-01-28

    def adjoint_jacobian_solve(self, adj_x, nl_deps, b):
        if adj_x is None:
            adj_x = self.new_adj_x()

        if self._cache_adjoint_jacobian:
            J_solver_mat_bc = self._adjoint_J_solver()
            if J_solver_mat_bc is None:
                J = adjoint(self._J)
                self._adjoint_J_solver, J_solver_mat_bc = \
                    linear_solver_cache().linear_solver(
                        J, bcs=self._hbcs,
                        form_compiler_parameters=self._form_compiler_parameters,  # noqa: E501
                        linear_solver_parameters=self._adjoint_solver_parameters,  # noqa: E501
                        replace_map=self._nonlinear_replace_map(nl_deps))
            J_solver, _, _ = J_solver_mat_bc

            apply_rhs_bcs(function_vector(b), self._hbcs)
            J_solver.solve(function_vector(adj_x), function_vector(b))

            return adj_x
        else:
            if self._adjoint_J is None:
                self._adjoint_J = unbound_form(
                    adjoint(self._J), self.nonlinear_dependencies())
            bind_form(self._adjoint_J, nl_deps)
            J_solver, _, _ = assemble_linear_solver(
                self._adjoint_J, bcs=self._hbcs,
                form_compiler_parameters=self._form_compiler_parameters,
                linear_solver_parameters=self._adjoint_solver_parameters)
            unbind_form(self._adjoint_J)

            apply_rhs_bcs(function_vector(b), self._hbcs)
            J_solver.solve(function_vector(adj_x), function_vector(b))

            return adj_x

    def tangent_linear(self, M, dM, tlm_map):
        x = self.x()

        tlm_rhs = ufl.classes.Form([])
        for dep in self.dependencies():
            if dep != x:
                tau_dep = get_tangent_linear(dep, M, dM, tlm_map)
                if tau_dep is not None:
                    tlm_rhs -= derivative(self._F, dep, argument=tau_dep)

        tlm_rhs = ufl.algorithms.expand_derivatives(tlm_rhs)
        if tlm_rhs.empty():
            return ZeroAssignment(tlm_map[x])
        else:
            if self._tlm_solver_parameters is None:
                tlm_solver_parameters = self._linear_solver_parameters
            else:
                tlm_solver_parameters = self._tlm_solver_parameters
            if self._initial_guess_index is None:
                tlm_initial_guess = None
            else:
                initial_guess = self.dependencies()[self._initial_guess_index]
                tlm_initial_guess = tlm_map[initial_guess]
            return EquationSolver(
                self._J == tlm_rhs, tlm_map[x], self._hbcs,
                form_compiler_parameters=self._form_compiler_parameters,
                solver_parameters=tlm_solver_parameters,
                adjoint_solver_parameters=self._adjoint_solver_parameters,
                tlm_solver_parameters=tlm_solver_parameters,
                initial_guess=tlm_initial_guess,
                cache_jacobian=self._cache_tlm_jacobian,
                cache_adjoint_jacobian=self._cache_adjoint_jacobian,
                cache_tlm_jacobian=self._cache_tlm_jacobian,
                cache_rhs_assembly=self._cache_rhs_assembly,
                defer_adjoint_assembly=self._defer_adjoint_assembly)


def expr_new_x(expr, x, *,
               annotate=None, tlm=None):
    """If an expression depends on `x`, then record the assignment `x_old =
    x`, and replace `x` with `x_old` in the expression.

    :arg expr: A UFL :class:`Expr`.
    :arg x: Defines `x`.
    :arg annotate: Whether the :class:`tlm_adjoint.tlm_adjoint.EquationManager`
        should record the solution of equations.
    :arg tlm: Whether tangent-linear equations should be solved.
    :returns: A UFL :class:`Expr` with `x` replaced with `x_old`, or `expr` if
        the expression does not depend on `x`.
    """

    if x in extract_coefficients(expr):
        x_old = function_new(x)
        Assignment(x_old, x).solve(annotate=annotate, tlm=tlm)
        return ufl.replace(expr, {x: x_old})
    else:
        return expr


def linear_equation_new_x(eq, x, *,
                          annotate=None, tlm=None):
    """If a symbolic expression for a linear finite element variational
    problem depends on the symbolic variable representing the problem solution,
    then record the assignment `x_old = x`, and replace `x` with `x_old` in the
    symbolic expression.

    Required for the case where a 'new' value is computed by solving a linear
    finite element variational problem depending on the 'old' value.

    :arg eq: A UFL :class:`Equation` defining the finite element variational
        problem.
    :arg x: A function defining the solution to the finite element variational
        problem.
    :arg annotate: Whether the :class:`tlm_adjoint.tlm_adjoint.EquationManager`
        should record the solution of equations.
    :arg tlm: Whether tangent-linear equations should be solved.
    :returns: A UFL :class:`Equation` with `x` replaced with `x_old`, or `eq`
        if the symbolic expression does not depend on `x`.
    """

    lhs, rhs = eq.lhs, eq.rhs
    lhs_x_dep = x in extract_coefficients(lhs)
    rhs_x_dep = x in extract_coefficients(rhs)
    if lhs_x_dep or rhs_x_dep:
        x_old = function_new(x)
        Assignment(x_old, x).solve(annotate=annotate, tlm=tlm)
        if lhs_x_dep:
            lhs = ufl.replace(lhs, {x: x_old})
        if rhs_x_dep:
            rhs = ufl.replace(rhs, {x: x_old})
        return lhs == rhs
    else:
        return eq


class Projection(EquationSolver):
    """Represents the solution of a finite element variational problem
    performing a projection onto the space for `x`.

    :arg x: A function defining the forward solution.
    :arg rhs: A UFL :class:`Expr` defining the expression to project onto the
        space for `x`, or a UFL :class:`Form` defining the right-hand-side
        of the finite element variational problem. Should not depend on `x`.

    Remaining arguments are passed to the :class:`EquationSolver` constructor.
    """

    def __init__(self, x, rhs, *args, **kwargs):
        space = function_space(x)
        test, trial = TestFunction(space), TrialFunction(space)
        if not isinstance(rhs, ufl.classes.Form):
            rhs = ufl.inner(rhs, test) * ufl.dx
        super().__init__(ufl.inner(trial, test) * ufl.dx == rhs, x,
                         *args, **kwargs)


class ProjectionSolver(Projection):
    ""

    def __init__(self, rhs, x, *args, **kwargs):
        warnings.warn("ProjectionSolver is deprecated -- "
                      "use Projection instead",
                      DeprecationWarning, stacklevel=2)
        super().__init__(x, rhs, *args, **kwargs)


class DirichletBCApplication(Equation):
    r"""Represents the application of a Dirichlet boundary condition to a zero
    valued function. Specifically, with the Firedrake backend this represents:

    .. code-block:: python

        x.zero()
        DirichletBC(x.function_space(), y, *args, **kwargs).apply(x)

    The forward residual :math:`\mathcal{F}` is defined so that :math:`\partial
    \mathcal{F} / \partial x` is the identity.

    :arg x: A function, updated by the above operations.
    :arg y: A function, defines the Dirichet boundary condition.

    Remaining arguments are passed to `DirichletBC`.
    """

    def __init__(self, x, y, *args, **kwargs):
        check_space_type(x, "primal")
        check_space_type(y, "primal")

        super().__init__(x, [x, y], nl_deps=[], ic=False, adj_ic=False)
        self._bc_args = args
        self._bc_kwargs = kwargs

    def forward_solve(self, x, deps=None):
        _, y = self.dependencies() if deps is None else deps
        function_zero(x)
        backend_DirichletBC(
            function_space(x), y,
            *self._bc_args, **self._bc_kwargs).apply(function_vector(x))

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_x):
        if dep_index == 0:
            return adj_x
        elif dep_index == 1:
            _, y = self.dependencies()
            F = function_new_conjugate_dual(y)
            backend_DirichletBC(
                function_space(y), adj_x,
                *self._bc_args, **self._bc_kwargs).apply(function_vector(F))
            return (-1.0, F)
        else:
            raise IndexError("dep_index out of bounds")

    def adjoint_jacobian_solve(self, adj_x, nl_deps, b):
        return b

    def tangent_linear(self, M, dM, tlm_map):
        x, y = self.dependencies()

        tau_y = get_tangent_linear(y, M, dM, tlm_map)
        if tau_y is None:
            return ZeroAssignment(tlm_map[x])
        else:
            return DirichletBCApplication(
                tlm_map[x], tau_y,
                *self._bc_args, **self._bc_kwargs)


class DirichletBCSolver(DirichletBCApplication):
    ""

    def __init__(self, y, x, *args, **kwargs):
        warnings.warn("DirichletBCSolver is deprecated -- "
                      "use DirichletBCApplication instead",
                      DeprecationWarning, stacklevel=2)
        super().__init__(x, y, *args, **kwargs)


class ExprInterpolation(ExprEquation):
    r"""Represents interpolation of `rhs` onto the space for `x`.

    The forward residual :math:`\mathcal{F}` is defined so that :math:`\partial
    \mathcal{F} / \partial x` is the identity.

    :arg x: A function defining the forward solution.
    :arg rhs: A UFL :class:`Expr` defining the expression to interpolate onto
        the space for `x`. Should not depend on `x`.
    """

    def __init__(self, x, rhs):
        deps, nl_deps = extract_dependencies(rhs)
        if function_id(x) in deps:
            raise ValueError("Invalid non-linear dependency")
        deps, nl_deps = list(deps.values()), tuple(nl_deps.values())
        deps.insert(0, x)

        super().__init__(x, deps, nl_deps=nl_deps, ic=False, adj_ic=False)
        self._rhs = rhs

    def drop_references(self):
        replace_map = {dep: function_replacement(dep)
                       for dep in self.dependencies()}

        super().drop_references()

        self._rhs = ufl.replace(self._rhs, replace_map)

    def forward_solve(self, x, deps=None):
        if deps is None:
            interpolate_expression(x, self._rhs)
        else:
            interpolate_expression(x, self._replace(self._rhs, deps))

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
        interpolate_expression(F, dF, adj_x=adj_x)
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
            return ExprInterpolation(tlm_map[x], tlm_rhs)


class ExprEvaluation(ExprInterpolation):
    ""

    def __init__(self, x, rhs):
        warnings.warn("ExprEvaluation is deprecated -- "
                      "use ExprInterpolation instead",
                      DeprecationWarning, stacklevel=2)
        super().__init__(x, rhs)


class ExprEvaluationSolver(ExprInterpolation):
    ""

    def __init__(self, rhs, x):
        warnings.warn("ExprEvaluationSolver is deprecated -- "
                      "use ExprInterpolation instead",
                      DeprecationWarning, stacklevel=2)
        super().__init__(x, rhs)
