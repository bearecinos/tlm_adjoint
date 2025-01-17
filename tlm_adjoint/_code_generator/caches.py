#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""This module is used by both the FEniCS and Firedrake backends, and
implements finite element assembly and linear solver data caching.
"""

from .backend import TrialFunction, backend_DirichletBC, backend_Function
from ..interface import (
    function_id, function_is_cached, function_space, is_function)
from .backend_code_generator_interface import (
    assemble, assemble_arguments, assemble_matrix, complex_mode, linear_solver,
    matrix_copy, parameters_key)

from ..caches import Cache

from .functions import (
    ReplacementFunction, derivative, eliminate_zeros, extract_coefficients,
    replaced_form)

from collections import defaultdict
import ufl
import warnings

__all__ = \
    [
        "AssemblyCache",
        "assembly_cache",
        "set_assembly_cache",

        "LinearSolverCache",
        "linear_solver_cache",
        "set_linear_solver_cache",
    ]


def is_cached(expr):
    for c in extract_coefficients(expr):
        if not is_function(c) or not function_is_cached(c):
            return False
    return True


def form_simplify_sign(form):
    integrals = []

    for integral in form.integrals():
        integrand = integral.integrand()

        integral_sign = None
        while isinstance(integrand, ufl.classes.Product):
            a, b = integrand.ufl_operands
            if isinstance(a, ufl.classes.IntValue) and a == -1:
                if integral_sign is None:
                    integral_sign = -1
                else:
                    integral_sign = -integral_sign
                integrand = b
            elif isinstance(b, ufl.classes.IntValue) and b == -1:
                if integral_sign is None:
                    integral_sign = -1
                else:
                    integral_sign = -integral_sign
                integrand = a
            else:
                break
        if integral_sign is not None:
            if integral_sign < 0:
                integral = integral.reconstruct(integrand=-integrand)
            else:
                integral = integral.reconstruct(integrand=integrand)

        integrals.append(integral)

    return ufl.classes.Form(integrals)


def form_simplify_conj(form):
    if complex_mode:
        def expr_conj(expr):
            if isinstance(expr, ufl.classes.Conj):
                x, = expr.ufl_operands
                return expr_simplify_conj(x)
            elif isinstance(expr, ufl.classes.Sum):
                return sum(map(expr_conj, expr.ufl_operands),
                           ufl.classes.Zero(shape=expr.ufl_shape))
            elif isinstance(expr, ufl.classes.Product):
                x, y = expr.ufl_operands
                return expr_conj(x) * expr_conj(y)
            else:
                return ufl.conj(expr)

        def expr_simplify_conj(expr):
            if isinstance(expr, ufl.classes.Conj):
                x, = expr.ufl_operands
                return expr_conj(x)
            elif isinstance(expr, ufl.classes.Sum):
                return sum(map(expr_simplify_conj, expr.ufl_operands),
                           ufl.classes.Zero(shape=expr.ufl_shape))
            elif isinstance(expr, ufl.classes.Product):
                x, y = expr.ufl_operands
                return expr_simplify_conj(x) * expr_simplify_conj(y)
            else:
                return expr

        def integral_simplify_conj(integral):
            integrand = integral.integrand()
            integrand = expr_simplify_conj(integrand)
            return integral.reconstruct(integrand=integrand)

        integrals = list(map(integral_simplify_conj, form.integrals()))
        return ufl.classes.Form(integrals)
    else:
        return ufl.algorithms.remove_complex_nodes.remove_complex_nodes(form)


def split_arity(form, x, argument):
    form_arguments = form.arguments()
    arity = len(form_arguments)
    if arity >= 2:
        raise ValueError("Invalid form arity")
    if arity == 1 and form_arguments[0].number() != 0:
        raise ValueError("Invalid form argument")
    if argument.number() < arity:
        raise ValueError("Invalid argument")

    if x not in extract_coefficients(form):
        # No dependence on x
        return ufl.classes.Form([]), form

    form_derivative = derivative(form, x, argument=argument,
                                 enable_automatic_argument=False)
    form_derivative = ufl.algorithms.expand_derivatives(form_derivative)
    if x in extract_coefficients(form_derivative):
        # Non-linear
        return ufl.classes.Form([]), form

    try:
        eq_form = ufl.algorithms.expand_derivatives(
            ufl.replace(form, {x: argument}))
        A = ufl.algorithms.formtransformations.compute_form_with_arity(
            eq_form, arity + 1)
        b = ufl.algorithms.formtransformations.compute_form_with_arity(
            eq_form, arity)
    except ufl.UFLException:
        # UFL error encountered
        return ufl.classes.Form([]), form

    try:
        ufl.algorithms.check_arities.check_form_arity(
            A, A.arguments(), complex_mode=complex_mode)
        ufl.algorithms.check_arities.check_form_arity(
            b, b.arguments(), complex_mode=complex_mode)
    except ufl.algorithms.check_arities.ArityMismatch:
        # Arity mismatch
        return ufl.classes.Form([]), form

    if not is_cached(A):
        # Non-cached higher arity form
        return ufl.classes.Form([]), form

    # Success
    return A, b


def split_terms(terms, base_integral,
                cached_terms=None, mat_terms=None, non_cached_terms=None):
    if cached_terms is None:
        cached_terms = []
    if mat_terms is None:
        mat_terms = defaultdict(lambda: [])
    if non_cached_terms is None:
        non_cached_terms = []

    for term in terms:
        if is_cached(term):
            cached_terms.append(term)
        elif isinstance(term, ufl.classes.Conj):
            term_conj, = term.ufl_operands
            if isinstance(term_conj, ufl.classes.Sum):
                split_terms(
                    tuple(map(ufl.conj, term_conj.ufl_operands)),
                    base_integral,
                    cached_terms, mat_terms, non_cached_terms)
            elif isinstance(term_conj, ufl.classes.Product):
                x, y = term_conj.ufl_operands
                split_terms(
                    (ufl.conj(x) * ufl.conj(y),),
                    base_integral,
                    cached_terms, mat_terms, non_cached_terms)
            else:
                non_cached_terms.append(term)
        elif isinstance(term, ufl.classes.Sum):
            split_terms(term.ufl_operands, base_integral,
                        cached_terms, mat_terms, non_cached_terms)
        elif isinstance(term, ufl.classes.Product):
            x, y = term.ufl_operands
            if is_cached(x):
                cached_sub, mat_sub, non_cached_sub = split_terms(
                    (y,), base_integral)
                for term in cached_sub:
                    cached_terms.append(x * term)
                for dep_id in mat_sub:
                    mat_terms[dep_id].extend(
                        x * mat_term for mat_term in mat_sub[dep_id])
                for term in non_cached_sub:
                    non_cached_terms.append(x * term)
            elif is_cached(y):
                cached_sub, mat_sub, non_cached_sub = split_terms(
                    (x,), base_integral)
                for term in cached_sub:
                    cached_terms.append(term * y)
                for dep_id in mat_sub:
                    mat_terms[dep_id].extend(
                        mat_term * y for mat_term in mat_sub[dep_id])
                for term in non_cached_sub:
                    non_cached_terms.append(term * y)
            else:
                non_cached_terms.append(term)
        else:
            mat_dep = None
            for dep in extract_coefficients(term):
                if not is_cached(dep):
                    if isinstance(dep, (backend_Function, ReplacementFunction)) and mat_dep is None:  # noqa: E501
                        mat_dep = dep
                    else:
                        mat_dep = None
                        break
            if mat_dep is None:
                non_cached_terms.append(term)
            else:
                term_form = ufl.classes.Form(
                    [base_integral.reconstruct(integrand=term)])
                mat_sub, non_cached_sub = split_arity(
                    term_form, mat_dep,
                    argument=TrialFunction(function_space(mat_dep)))
                mat_sub = [integral.integrand()
                           for integral in mat_sub.integrals()]
                non_cached_sub = [integral.integrand()
                                  for integral in non_cached_sub.integrals()]
                if len(mat_sub) > 0:
                    mat_terms[function_id(mat_dep)].extend(mat_sub)
                non_cached_terms.extend(non_cached_sub)

    return cached_terms, mat_terms, non_cached_terms


def split_form(form):
    if len(form.arguments()) != 1:
        raise ValueError("Arity 1 form required")
    if not complex_mode:
        form = ufl.algorithms.remove_complex_nodes.remove_complex_nodes(form)

    def add_integral(integrals, base_integral, terms):
        if len(terms) > 0:
            integrand = sum(terms, ufl.classes.Zero())
            integral = base_integral.reconstruct(integrand=integrand)
            integrals.append(integral)

    cached_integrals = []
    mat_integrals = defaultdict(lambda: [])
    non_cached_integrals = []
    for integral in form.integrals():
        cached_terms, mat_terms, non_cached_terms = \
            split_terms((integral.integrand(),), integral)
        add_integral(cached_integrals, integral, cached_terms)
        for dep_id in mat_terms:
            add_integral(mat_integrals[dep_id], integral, mat_terms[dep_id])
        add_integral(non_cached_integrals, integral, non_cached_terms)

    cached_form = ufl.classes.Form(cached_integrals)
    mat_forms = {}
    for dep_id in mat_integrals:
        mat_forms[dep_id] = ufl.classes.Form(mat_integrals[dep_id])
    non_cached_forms = ufl.classes.Form(non_cached_integrals)

    return cached_form, mat_forms, non_cached_forms


def form_dependencies(form):
    deps = {}
    for dep in extract_coefficients(form):
        if is_function(dep):
            dep_id = function_id(dep)
            if dep_id not in deps:
                deps[dep_id] = dep
    return deps


def form_key(form):
    form = replaced_form(form)
    form = ufl.algorithms.expand_derivatives(form)
    form = ufl.algorithms.expand_compounds(form)
    form = ufl.algorithms.expand_indices(form)
    form = form_simplify_conj(form)
    form = form_simplify_sign(form)
    return form


def assemble_key(form, bcs, assemble_kwargs):
    return (form_key(form), tuple(bcs), parameters_key(assemble_kwargs))


class AssemblyCache(Cache):
    """A :class:`tlm_adjoint.caches.Cache` for finite element assembly data.
    """

    def assemble(self, form, *,
                 bcs=None, form_compiler_parameters=None,
                 solver_parameters=None, linear_solver_parameters=None,
                 replace_map=None):
        """Perform finite element assembly and cache the result, or return a
        previously cached result.

        :arg form: The UFL :class:`Form` to assemble.
        :arg bcs: Dirichlet boundary conditions.
        :arg form_compiler_parameters: Form compiler parameters.
        :arg solver_parameters: Deprecated.
        :arg linear_solver_parameters: Linear solver parameters. Required for
            assembly parameters which appear in the linear solver parameters
            -- in particular the Firedrake `'mat_type'` parameter.
        :arg replace_map: A :class:`Mapping` defining a map from symbolic
            variables to values.
        :returns: A :class:`tuple` `(value_ref, value)`, where `value` is the
            result of the finite element assembly, and `value_ref` is a
            :class:`tlm_adjoint.caches.CacheRef` storing a reference to
            `value`.

                - For an arity zero or arity one form `value_ref` stores the
                  assembled value.
                - For an arity two form `value_ref` is a tuple `(A, b_bc)`. `A`
                  is the assembled matrix, and `b_bc` is a boundary condition
                  right-hand-side term which should be added after assembling a
                  right-hand-side with homogeneous boundary conditions applied.
                  `b_bc` may be `None` to indicate that this term is zero.
        """

        if bcs is None:
            bcs = ()
        elif isinstance(bcs, backend_DirichletBC):
            bcs = (bcs,)
        if form_compiler_parameters is None:
            form_compiler_parameters = {}

        if solver_parameters is not None:
            warnings.warn("solver_parameters argument is deprecated -- use "
                          "linear_solver_parameters instead",
                          DeprecationWarning, stacklevel=2)
            if linear_solver_parameters is not None:
                raise TypeError("Cannot pass both solver_parameters and "
                                "linear_solver_parameters arguments")
            linear_solver_parameters = solver_parameters
        elif linear_solver_parameters is None:
            linear_solver_parameters = {}

        form = eliminate_zeros(form, force_non_empty_form=True)
        arity = len(form.arguments())
        assemble_kwargs = assemble_arguments(arity, form_compiler_parameters,
                                             linear_solver_parameters)
        key = assemble_key(form, bcs, assemble_kwargs)

        def value():
            if replace_map is None:
                assemble_form = form
            else:
                assemble_form = ufl.replace(form, replace_map)
            if arity == 0:
                if len(bcs) > 0:
                    raise TypeError("Unexpected boundary conditions for arity "
                                    "0 form")
                b = assemble(assemble_form, **assemble_kwargs)
            elif arity == 1:
                b = assemble(assemble_form, **assemble_kwargs)
                for bc in bcs:
                    bc.apply(b)
            elif arity == 2:
                b = assemble_matrix(assemble_form, bcs=bcs, **assemble_kwargs)
            else:
                raise ValueError(f"Unexpected form arity {arity:d}")
            return b

        return self.add(key, value,
                        deps=tuple(form_dependencies(form).values()))


def linear_solver_key(form, bcs, linear_solver_parameters,
                      form_compiler_parameters):
    return (form_key(form), tuple(bcs),
            parameters_key(linear_solver_parameters),
            parameters_key(form_compiler_parameters))


class LinearSolverCache(Cache):
    """A :class:`tlm_adjoint.caches.Cache` for linear solver data.
    """

    def linear_solver(self, form, *,
                      A=None, bcs=None, form_compiler_parameters=None,
                      linear_solver_parameters=None, replace_map=None,
                      assembly_cache=None):
        """Construct a linear solver and cache the result, or return a
        previously cached result.

        :arg form: An arity two UFL :class:`Form`, defining the matrix.
        :arg A: Deprecated.
        :arg bcs: Dirichlet boundary conditions.
        :arg form_compiler_parameters: Form compiler parameters.
        :arg linear_solver_parameters: Linear solver parameters.
        :arg replace_map: A :class:`Mapping` defining a map from symbolic
            variables to values.
        :arg assembly_cache: :class:`AssemblyCache` to use for finite element
            assembly. Defaults to `assembly_cache()`.
        :returns: A :class:`tuple` `(value_ref, value)`. `value` is a tuple
            `(solver, A, b_bc)`, where `solver` is the linear solver, `A` is
            the assembled matrix, and `b_bc` is a boundary condition
            right-hand-side term which should be added after assembling a
            right-hand-side with homogeneous boundary conditions applied.
            `b_bc` may be `None` to indicate that this term is zero.
            `value_ref` is a :class:`tlm_adjoint.caches.CacheRef` storing a
            reference to `value`.
        """

        if bcs is None:
            bcs = ()
        elif isinstance(bcs, backend_DirichletBC):
            bcs = (bcs,)
        if form_compiler_parameters is None:
            form_compiler_parameters = {}
        if linear_solver_parameters is None:
            linear_solver_parameters = {}

        form = eliminate_zeros(form, force_non_empty_form=True)
        key = linear_solver_key(form, bcs, linear_solver_parameters,
                                form_compiler_parameters)

        if A is None:
            if assembly_cache is None:
                assembly_cache = globals()["assembly_cache"]()

            def value():
                _, (A, b_bc) = assembly_cache.assemble(
                    form, bcs=bcs,
                    form_compiler_parameters=form_compiler_parameters,
                    linear_solver_parameters=linear_solver_parameters,
                    replace_map=replace_map)
                solver = linear_solver(matrix_copy(A),
                                       linear_solver_parameters)
                return solver, A, b_bc
        else:
            warnings.warn("A argument is deprecated",
                          DeprecationWarning, stacklevel=2)

            # A = matrix_copy(A)  # Caller's responsibility

            def value():
                return linear_solver(A, linear_solver_parameters)

        return self.add(key, value,
                        deps=tuple(form_dependencies(form).values()))


_assembly_cache = AssemblyCache()


def assembly_cache():
    """
    :returns: The default :class:`AssemblyCache`.
    """

    return _assembly_cache


def set_assembly_cache(assembly_cache):
    """Set the default :class:`AssemblyCache`.

    :arg assembly_cache: The new default :class:`AssemblyCache`.
    """

    global _assembly_cache
    _assembly_cache = assembly_cache


_linear_solver_cache = LinearSolverCache()


def linear_solver_cache():
    """
    :returns: The default :class:`LinearSolverCache`.
    """

    return _linear_solver_cache


def set_linear_solver_cache(linear_solver_cache):
    """Set the default :class:`LinearSolverCache`.

    :arg linear_solver_cache: The new default :class:`LinearSolverCache`.
    """

    global _linear_solver_cache
    _linear_solver_cache = linear_solver_cache
