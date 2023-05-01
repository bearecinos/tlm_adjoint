#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# For tlm_adjoint copyright information see ACKNOWLEDGEMENTS in the tlm_adjoint
# root directory

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

from .backend import Form, FunctionSpace, Parameters, TensorFunctionSpace, \
    TestFunction, TrialFunction, UserExpression, as_backend_type, \
    backend_Constant, backend_DirichletBC, backend_Function, \
    backend_KrylovSolver, backend_LUSolver, backend_LinearVariationalSolver, \
    backend_NonlinearVariationalSolver, backend_ScalarType, backend_assemble, \
    backend_assemble_system, backend_solve, cpp_LinearVariationalProblem, \
    cpp_NonlinearVariationalProblem, extract_args, has_lu_solver_method, \
    parameters
from ..interface import check_space_type, check_space_types, function_assign, \
    function_get_values, function_inner, function_new_conjugate_dual, \
    function_set_values, function_space, function_space_type, space_new

from .functions import eliminate_zeros

from collections.abc import Iterable, Sequence
import copy
import ffc
import numpy as np
import ufl

__all__ = \
    [
        "complex_mode",

        "assemble_linear_solver",
        "assemble_matrix",
        "linear_solver",
        "matrix_multiply",

        "homogenize",

        "interpolate_expression",

        "assemble",
        "solve"
    ]


if "tlm_adjoint" not in parameters:
    parameters.add(Parameters("tlm_adjoint"))
_parameters = parameters["tlm_adjoint"]
if "Assembly" not in _parameters:
    _parameters.add(Parameters("Assembly"))
if "match_quadrature" not in _parameters["Assembly"]:
    _parameters["Assembly"].add("match_quadrature", False)
if "EquationSolver" not in _parameters:
    _parameters.add(Parameters("EquationSolver"))
if "enable_jacobian_caching" not in _parameters["EquationSolver"]:
    _parameters["EquationSolver"].add("enable_jacobian_caching", True)
if "cache_rhs_assembly" not in _parameters["EquationSolver"]:
    _parameters["EquationSolver"].add("cache_rhs_assembly", True)
if "match_quadrature" not in _parameters["EquationSolver"]:
    _parameters["EquationSolver"].add("match_quadrature", False)
if "defer_adjoint_assembly" not in _parameters["EquationSolver"]:
    _parameters["EquationSolver"].add("defer_adjoint_assembly", False)
if "assembly_verification" not in _parameters:
    _parameters.add(Parameters("assembly_verification"))
if "jacobian_tolerance" not in _parameters["assembly_verification"]:
    _parameters["assembly_verification"].add("jacobian_tolerance", np.inf)
if "rhs_tolerance" not in _parameters["assembly_verification"]:
    _parameters["assembly_verification"].add("rhs_tolerance", np.inf)
# For deprecated AssembleSolver
if "AssembleSolver" not in _parameters:
    _parameters.add(Parameters("AssembleSolver"))
if "match_quadrature" not in _parameters["AssembleSolver"]:
    _parameters["AssembleSolver"].add("match_quadrature", False)
del _parameters


complex_mode = False


def copy_parameters_dict(parameters):
    if isinstance(parameters, Parameters):
        parameters = parameters.copy()
    new_parameters = {}
    for key in parameters:
        value = parameters[key]
        if isinstance(value, (Parameters, dict)):
            value = copy_parameters_dict(value)
        elif isinstance(value, Iterable):
            value = copy.copy(value)  # shallow copy only
        new_parameters[key] = value
    return new_parameters


def update_parameters_dict(parameters, new_parameters):
    for key in new_parameters:
        value = new_parameters[key]
        if key in parameters \
           and isinstance(parameters[key], (Parameters, dict)) \
           and isinstance(value, (Parameters, dict)):
            update_parameters_dict(parameters[key], value)
        elif isinstance(value, (Parameters, dict)):
            parameters[key] = copy_parameters_dict(value)
        else:
            parameters[key] = value


def process_solver_parameters(solver_parameters, linear):
    solver_parameters = copy_parameters_dict(solver_parameters)
    if linear:
        linear_solver_parameters = solver_parameters
    else:
        if "nonlinear_solver" not in solver_parameters:
            solver_parameters["nonlinear_solver"] = "newton"
        nl_solver = solver_parameters["nonlinear_solver"]
        if nl_solver == "newton":
            if "newton_solver" not in solver_parameters:
                solver_parameters["newton_solver"] = {}
            linear_solver_parameters = solver_parameters["newton_solver"]
        elif nl_solver == "snes":
            if "snes_solver" not in solver_parameters:
                solver_parameters["snes_solver"] = {}
            linear_solver_parameters = solver_parameters["snes_solver"]
        else:
            raise ValueError(f"Unsupported non-linear solver: {nl_solver}")

    if "linear_solver" not in linear_solver_parameters:
        linear_solver_parameters["linear_solver"] = "default"
    linear_solver = linear_solver_parameters["linear_solver"]
    is_lu_linear_solver = linear_solver in ["default", "direct", "lu"] \
        or has_lu_solver_method(linear_solver)
    if is_lu_linear_solver:
        if "lu_solver" not in linear_solver_parameters:
            linear_solver_parameters["lu_solver"] = {}
        linear_solver_ic = False
    else:
        if "krylov_solver" not in linear_solver_parameters:
            linear_solver_parameters["krylov_solver"] = {}
        ks_parameters = linear_solver_parameters["krylov_solver"]
        if "nonzero_initial_guess" not in ks_parameters:
            ks_parameters["nonzero_initial_guess"] = False
        nonzero_initial_guess = ks_parameters["nonzero_initial_guess"]
        linear_solver_ic = nonzero_initial_guess

    return (solver_parameters, linear_solver_parameters,
            not linear or linear_solver_ic, linear_solver_ic)


def process_adjoint_solver_parameters(linear_solver_parameters):
    # Copy not required
    return linear_solver_parameters


def assemble_arguments(arity, form_compiler_parameters, solver_parameters):
    return {"form_compiler_parameters": form_compiler_parameters}


def assemble_matrix(form, bcs=None, *args,
                    form_compiler_parameters=None, **kwargs):
    if bcs is None:
        bcs = ()
    elif isinstance(bcs, backend_DirichletBC):
        bcs = (bcs,)
    if form_compiler_parameters is None:
        form_compiler_parameters = {}

    if len(bcs) > 0:
        test = TestFunction(form.arguments()[0].function_space())
        if len(test.ufl_shape) == 0:
            zero = backend_Constant(0.0)
        else:
            zero = backend_Constant(np.zeros(test.ufl_shape,
                                             dtype=backend_ScalarType))
        dummy_rhs = ufl.inner(zero, test) * ufl.dx
        A, b_bc = assemble_system(
            form, dummy_rhs, bcs=bcs,
            form_compiler_parameters=form_compiler_parameters, *args, **kwargs)
        if b_bc.norm("linf") == 0.0:
            b_bc = None
    else:
        A = assemble(
            form, form_compiler_parameters=form_compiler_parameters,
            *args, **kwargs)
        b_bc = None

    return A, b_bc


def assemble_linear_solver(A_form, b_form=None, bcs=None, *,
                           form_compiler_parameters=None,
                           linear_solver_parameters=None):
    if bcs is None:
        bcs = ()
    elif isinstance(bcs, backend_DirichletBC):
        bcs = (bcs,)
    if form_compiler_parameters is None:
        form_compiler_parameters = {}
    if linear_solver_parameters is None:
        linear_solver_parameters = {}

    if b_form is None:
        A, b = assemble_matrix(
            A_form, bcs=bcs, form_compiler_parameters=form_compiler_parameters)
    else:
        A, b = assemble_system(
            A_form, b_form, bcs=bcs,
            form_compiler_parameters=form_compiler_parameters)

    solver = linear_solver(A, linear_solver_parameters)

    return solver, A, b


def linear_solver(A, linear_solver_parameters):
    linear_solver = linear_solver_parameters.get("linear_solver", "default")
    if linear_solver in ["direct", "lu"]:
        linear_solver = "default"
    elif linear_solver == "iterative":
        linear_solver = "gmres"
    is_lu_linear_solver = linear_solver == "default" \
        or has_lu_solver_method(linear_solver)
    if is_lu_linear_solver:
        solver = backend_LUSolver(A, linear_solver)
        lu_parameters = linear_solver_parameters.get("lu_solver", {})
        update_parameters_dict(solver.parameters, lu_parameters)
    else:
        pc = linear_solver_parameters.get("preconditioner", "default")
        ks_parameters = linear_solver_parameters.get("krylov_solver", {})
        solver = backend_KrylovSolver(A, linear_solver, pc)
        update_parameters_dict(solver.parameters, ks_parameters)
    return solver


def form_form_compiler_parameters(form, form_compiler_parameters):
    (form_data,), _, _, _ \
        = ffc.analysis.analyze_forms((form,), form_compiler_parameters)
    integral_metadata = tuple(integral_data.metadata
                              for integral_data in form_data.integral_data)
    qr = form_compiler_parameters.get("quadrature_rule", "auto")
    if qr in [None, "auto"]:
        qr = ffc.analysis._extract_common_quadrature_rule(integral_metadata)
    qd = form_compiler_parameters.get("quadrature_degree", "auto")
    if qd in [None, "auto", -1]:
        qd = ffc.analysis._extract_common_quadrature_degree(integral_metadata)
    return {"quadrature_rule": qr, "quadrature_degree": qd}


def homogenize(bc):
    hbc = backend_DirichletBC(bc)
    hbc.homogenize()
    return hbc


def matrix_copy(A):
    return A.copy()


def matrix_multiply(A, x, *,
                    tensor=None, addto=False, action_type="conjugate_dual"):
    if tensor is None:
        if hasattr(A, "_tlm_adjoint__form") and hasattr(x, "_tlm_adjoint__function"):  # noqa: E501
            tensor = function_vector(space_new(
                A._tlm_adjoint__form.arguments()[0].function_space(),
                space_type=function_space_type(x._tlm_adjoint__function,
                                               rel_space_type=action_type)))
        else:
            return A * x
    elif hasattr(tensor, "_tlm_adjoint__function") and hasattr(x, "_tlm_adjoint__function"):  # noqa: E501
        check_space_types(tensor._tlm_adjoint__function,
                          x._tlm_adjoint__function,
                          rel_space_type=action_type)

    x_v = as_backend_type(x).vec()
    tensor_v = as_backend_type(tensor).vec()
    if addto:
        as_backend_type(A).mat().multAdd(x_v, tensor_v, tensor_v)
    else:
        as_backend_type(A).mat().mult(x_v, tensor_v)
    tensor.apply("insert")

    return tensor


def is_valid_r0_space(space):
    if not hasattr(space, "_tlm_adjoint__is_valid_r0_space"):
        e = space.ufl_element()
        if e.family() != "Real" or e.degree() != 0:
            valid = False
        elif len(e.value_shape()) == 0:
            r = backend_Function(space)
            r.assign(backend_Constant(1.0), annotate=False, tlm=False)
            valid = (r.vector().max() == 1.0)
        else:
            r = backend_Function(space)
            r_arr = np.arange(1, np.prod(r.ufl_shape) + 1,
                              dtype=backend_ScalarType)
            r_arr.shape = r.ufl_shape
            r.assign(backend_Constant(r_arr),
                     annotate=False, tlm=False)
            for i, r_c in enumerate(r.split(deepcopy=True)):
                if r_c.vector().max() != i + 1:
                    valid = False
                    break
            else:
                valid = True
        space._tlm_adjoint__is_valid_r0_space = valid
    return space._tlm_adjoint__is_valid_r0_space


def r0_space(x):
    if not hasattr(x, "_tlm_adjoint__r0_space"):
        domain, = x.ufl_domains()
        domain = domain.ufl_cargo()
        if len(x.ufl_shape) == 0:
            space = FunctionSpace(domain, "R", 0)
        else:
            space = TensorFunctionSpace(domain, "R", degree=0,
                                        shape=x.ufl_shape)
        assert is_valid_r0_space(space)
        x._tlm_adjoint__r0_space = space
    return x._tlm_adjoint__r0_space


def function_vector(x):
    return x.vector()


def rhs_copy(x):
    if hasattr(x, "_tlm_adjoint__function"):
        check_space_type(x._tlm_adjoint__function, "conjugate_dual")
    return x.copy()


def rhs_addto(x, y):
    if hasattr(x, "_tlm_adjoint__function"):
        check_space_type(x._tlm_adjoint__function, "conjugate_dual")
    if hasattr(y, "_tlm_adjoint__function"):
        check_space_type(y._tlm_adjoint__function, "conjugate_dual")
    x.axpy(1.0, y)


def parameters_key(parameters):
    key = []
    for name in sorted(parameters.keys()):
        sub_parameters = parameters[name]
        if isinstance(sub_parameters, (Parameters, dict)):
            key.append((name, parameters_key(sub_parameters)))
        elif isinstance(sub_parameters, Sequence) \
                and not isinstance(sub_parameters, str):
            key.append((name, tuple(sub_parameters)))
        else:
            key.append((name, sub_parameters))
    return tuple(key)


def verify_assembly(J, rhs, J_mat, b, bcs, form_compiler_parameters,
                    linear_solver_parameters, J_tolerance, b_tolerance):
    if np.isposinf(J_tolerance) and np.isposinf(b_tolerance):
        return

    J_mat_debug, b_debug = backend_assemble_system(
        J, rhs, bcs=bcs, form_compiler_parameters=form_compiler_parameters)

    if J_mat is not None and not np.isposinf(J_tolerance):
        assert (J_mat - J_mat_debug).norm("linf") \
            <= J_tolerance * J_mat_debug.norm("linf")

    if b is not None and not np.isposinf(b_tolerance):
        assert (b - b_debug).norm("linf") <= b_tolerance * b_debug.norm("linf")


def interpolate_expression(x, expr, *, adj_x=None):
    if adj_x is None:
        check_space_type(x, "primal")
    else:
        check_space_type(x, "conjugate_dual")
        check_space_type(adj_x, "conjugate_dual")
    for dep in ufl.algorithms.extract_coefficients(expr):
        check_space_type(dep, "primal")

    expr = eliminate_zeros(expr)

    class Expr(UserExpression):
        def eval(self, value, x):
            value[:] = expr(tuple(x))

        def value_shape(self):
            return x.ufl_shape

    if adj_x is None:
        if isinstance(x, backend_Constant):
            if isinstance(expr, backend_Constant):
                value = expr
            else:
                if len(x.ufl_shape) > 0:
                    raise ValueError("Scalar Constant required")
                value = x.values()
                Expr().eval(value, ())
                value, = value
            function_assign(x, value)
        elif isinstance(x, backend_Function):
            try:
                x.assign(expr, annotate=False, tlm=False)
            except RuntimeError:
                x.interpolate(Expr())
        else:
            raise TypeError(f"Unexpected type: {type(x)}")
    else:
        expr_val = function_new_conjugate_dual(adj_x)
        interpolate_expression(expr_val, expr)

        if isinstance(x, backend_Constant):
            if len(x.ufl_shape) > 0:
                raise ValueError("Scalar Constant required")
            function_assign(x, function_inner(adj_x, expr_val))
        elif isinstance(x, backend_Function):
            x_space = function_space(x)
            adj_x_space = function_space(adj_x)
            if x_space.ufl_domains() != adj_x_space.ufl_domains() \
                    or x_space.ufl_element() != adj_x_space.ufl_element():
                raise ValueError("Unable to perform transpose interpolation")
            function_set_values(
                x, function_get_values(expr_val).conjugate() * function_get_values(adj_x))  # noqa: E501
        else:
            raise TypeError(f"Unexpected type: {type(x)}")


# The following override assemble, assemble_system, and solve so that DOLFIN
# Form objects are cached on UFL form objects


def dolfin_form(form, form_compiler_parameters):
    if form_compiler_parameters is None:
        form_compiler_parameters = parameters["form_compiler"]

    if "_tlm_adjoint__form" in form._cache and \
       parameters_key(form_compiler_parameters) != \
       form._cache["_tlm_adjoint__form_compiler_parameters_key"]:
        del form._cache["_tlm_adjoint__form"]
        del form._cache["_tlm_adjoint__deps_map"]
        del form._cache["_tlm_adjoint__form_compiler_parameters_key"]

    if "_tlm_adjoint__form" in form._cache:
        dolfin_form = form._cache["_tlm_adjoint__form"]
        bindings = form._cache.get("_tlm_adjoint__bindings", None)
        if bindings is None:
            deps = form.coefficients()
        else:
            deps = tuple(bindings.get(c, c) for c in form.coefficients())
        for i, j in enumerate(form._cache["_tlm_adjoint__deps_map"]):
            cpp_object = deps[j]._cpp_object
            dolfin_form.set_coefficient(i, cpp_object)
    else:
        bindings = form._cache.get("_tlm_adjoint__bindings", None)
        if bindings is not None:
            dep_cpp_object = {}
            for dep in form.coefficients():
                if dep in bindings:
                    dep_binding = bindings[dep]
                    dep_cpp_object[dep] = getattr(dep, "_cpp_object", None)
                    dep._cpp_object = dep_binding._cpp_object

        simplified_form = eliminate_zeros(form, force_non_empty_form=True)
        dolfin_form = Form(
            simplified_form,
            form_compiler_parameters=copy_parameters_dict(form_compiler_parameters))  # noqa: E501
        if not hasattr(dolfin_form, "_compiled_form"):
            dolfin_form._compiled_form = None

        if bindings is not None:
            for dep, cpp_object in dep_cpp_object.items():
                if cpp_object is None:
                    del dep._cpp_object
                else:
                    dep._cpp_object = cpp_object

        form._cache["_tlm_adjoint__form"] = dolfin_form
        form._cache["_tlm_adjoint__deps_map"] = \
            tuple(map(dolfin_form.original_coefficient_position,
                      range(dolfin_form.num_coefficients())))
        form._cache["_tlm_adjoint__form_compiler_parameters_key"] = \
            parameters_key(form_compiler_parameters)
    return dolfin_form


def clear_dolfin_form(form):
    for i in range(form.num_coefficients()):
        form.set_coefficient(i, None)

# Aim for compatibility with FEniCS 2019.1.0 API


def assemble(form, tensor=None, form_compiler_parameters=None,
             *args, **kwargs):
    if form_compiler_parameters is None:
        form_compiler_parameters = {}

    if tensor is not None and hasattr(tensor, "_tlm_adjoint__function"):
        check_space_type(tensor._tlm_adjoint__function, "conjugate_dual")

    is_dolfin_form = isinstance(form, Form)
    if not is_dolfin_form:
        form = dolfin_form(form, form_compiler_parameters)
    b = backend_assemble(form, tensor=tensor, *args, **kwargs)
    if not is_dolfin_form:
        clear_dolfin_form(form)

    return b


def assemble_system(A_form, b_form, bcs=None, x0=None,
                    form_compiler_parameters=None, add_values=False,
                    finalize_tensor=True, keep_diagonal=False, A_tensor=None,
                    b_tensor=None, *args, **kwargs):
    if bcs is None:
        bcs = ()
    elif isinstance(bcs, backend_DirichletBC):
        bcs = (bcs,)
    if form_compiler_parameters is None:
        form_compiler_parameters = {}

    if b_tensor is not None and hasattr(b_tensor, "_tlm_adjoint__function"):
        check_space_type(b_tensor._tlm_adjoint__function, "conjugate_dual")

    A_is_dolfin_form = isinstance(A_form, Form)
    b_is_dolfin_form = isinstance(b_form, Form)
    if not A_is_dolfin_form:
        A_form = dolfin_form(A_form, form_compiler_parameters)
    if not b_is_dolfin_form:
        b_form = dolfin_form(b_form, form_compiler_parameters)
    return_value = backend_assemble_system(
        A_form, b_form, bcs=bcs, x0=x0, add_values=add_values,
        finalize_tensor=finalize_tensor, keep_diagonal=keep_diagonal,
        A_tensor=A_tensor, b_tensor=b_tensor, *args, **kwargs)
    if not A_is_dolfin_form:
        clear_dolfin_form(A_form)
    if not b_is_dolfin_form:
        clear_dolfin_form(b_form)
    return return_value


def solve(*args, **kwargs):
    if not isinstance(args[0], ufl.classes.Equation):
        return backend_solve(*args, **kwargs)

    extracted_args = extract_args(*args, **kwargs)
    if len(extracted_args) == 8:
        (eq, x, bcs, J,
         tol, M,
         form_compiler_parameters,
         solver_parameters) \
            = extracted_args
        preconditioner = None
    else:
        (eq, x, bcs, J,
         tol, M,
         preconditioner,
         form_compiler_parameters,
         solver_parameters) = extracted_args
    del extracted_args

    check_space_type(x, "primal")
    if bcs is None:
        bcs = ()
    elif isinstance(bcs, backend_DirichletBC):
        bcs = (bcs,)
    if form_compiler_parameters is None:
        form_compiler_parameters = {}
    if solver_parameters is None:
        solver_parameters = {}

    if tol is not None or M is not None or preconditioner is not None:
        return backend_solve(*args, **kwargs)

    lhs, rhs = eq.lhs, eq.rhs
    linear = isinstance(rhs, ufl.classes.Form)
    if linear:
        lhs = dolfin_form(lhs, form_compiler_parameters)
        rhs = dolfin_form(rhs, form_compiler_parameters)
        cpp_object = x._cpp_object
        problem = cpp_LinearVariationalProblem(lhs, rhs, cpp_object, bcs)
        solver = backend_LinearVariationalSolver(problem)
        solver.parameters.update(solver_parameters)
        return_value = solver.solve()
        clear_dolfin_form(lhs)
        clear_dolfin_form(rhs)
        return return_value
    else:
        F = lhs
        assert rhs == 0
        if J is None:
            if "_tlm_adjoint__J" in F._cache:
                J = F._cache["_tlm_adjoint__J"]
            else:
                J = ufl.derivative(F, x,
                                   argument=TrialFunction(x.function_space()))
                J = ufl.algorithms.expand_derivatives(J)
                F._cache["_tlm_adjoint__J"] = J

        F = dolfin_form(F, form_compiler_parameters)
        J = dolfin_form(J, form_compiler_parameters)
        cpp_object = x._cpp_object
        problem = cpp_NonlinearVariationalProblem(F, cpp_object, bcs, J)
        solver = backend_NonlinearVariationalSolver(problem)
        solver.parameters.update(solver_parameters)
        return_value = solver.solve()
        clear_dolfin_form(F)
        clear_dolfin_form(J)
        return return_value
