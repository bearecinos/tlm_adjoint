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

from .backend import backend_Constant, backend_DirichletBC, backend_Function, \
    backend_ScalarType
from ..interface import DEFAULT_COMM, SpaceInterface, add_interface, \
    comm_parent, function_caches, function_comm, function_dtype, function_id, \
    function_is_cached, function_is_checkpointed, function_is_static, \
    function_name, function_replacement, function_space, function_space_type, \
    is_function, space_comm
from ..interface import FunctionInterface as _FunctionInterface

from ..caches import Caches
from ..overloaded_float import SymbolicFloat

import numpy as np
import ufl
import weakref
import warnings

__all__ = \
    [
        "Constant",
        "DirichletBC",
        "Function",
        "HomogeneousDirichletBC",
        "ReplacementConstant",
        "ReplacementFunction",
        "ZeroConstant",
        "ZeroFunction",
        "bcs_is_cached",
        "bcs_is_homogeneous",
        "bcs_is_static",
        "define_function_alias",
        "eliminate_zeros",
        "extract_coefficients",
        "new_count",
        "replaced_form"
    ]


def new_count():
    c = backend_Constant.__new__(backend_Constant, 0.0)
    backend_Constant._tlm_adjoint__orig___init__(c, 0.0)
    return c.count()


class ConstantSpaceInterface(SpaceInterface):
    def _comm(self):
        return self._tlm_adjoint__space_interface_attrs["comm"]

    def _dtype(self):
        return self._tlm_adjoint__space_interface_attrs["dtype"]

    def _id(self):
        return self._tlm_adjoint__space_interface_attrs["id"]

    def _new(self, *, name=None, space_type="primal", static=False, cache=None,
             checkpoint=None):
        domain = self._tlm_adjoint__space_interface_attrs["domain"]
        return Constant(name=name, domain=domain, space=self,
                        space_type=space_type, static=static, cache=cache,
                        checkpoint=checkpoint)


class ConstantInterface(_FunctionInterface):
    def _space(self):
        return self._tlm_adjoint__function_interface_attrs["space"]

    def _space_type(self):
        return self._tlm_adjoint__function_interface_attrs["space_type"]

    def _dtype(self):
        return self._tlm_adjoint__function_interface_attrs["dtype"]

    def _id(self):
        return self._tlm_adjoint__function_interface_attrs["id"]

    def _name(self):
        if hasattr(self, "name"):
            assert "name" not in self._tlm_adjoint__function_interface_attrs
            return self.name()
        else:
            return self._tlm_adjoint__function_interface_attrs["name"]

    def _state(self):
        return self._tlm_adjoint__function_interface_attrs["state"]

    def _update_state(self):
        state = self._tlm_adjoint__function_interface_attrs["state"]
        self._tlm_adjoint__function_interface_attrs.d_setitem("state", state + 1)  # noqa: E501

    def _is_static(self):
        return self._tlm_adjoint__function_interface_attrs["static"]

    def _is_cached(self):
        return self._tlm_adjoint__function_interface_attrs["cache"]

    def _is_checkpointed(self):
        return self._tlm_adjoint__function_interface_attrs["checkpoint"]

    def _caches(self):
        if "caches" not in self._tlm_adjoint__function_interface_attrs:
            self._tlm_adjoint__function_interface_attrs["caches"] \
                = Caches(self)
        return self._tlm_adjoint__function_interface_attrs["caches"]

    def _zero(self):
        if len(self.ufl_shape) == 0:
            value = 0.0
        else:
            value = np.zeros(self.ufl_shape, dtype=function_dtype(self))
            value = backend_Constant(value)
        self.assign(value, annotate=False, tlm=False)

    def _assign(self, y):
        if isinstance(y, SymbolicFloat):
            y = y.value()
        if isinstance(y, (int, np.integer,
                          float, np.floating,
                          complex, np.complexfloating)):
            dtype = function_dtype(self)
            if len(self.ufl_shape) == 0:
                value = dtype(y)
            else:
                value = np.full(self.ufl_shape, dtype(y), dtype=dtype)
                value = backend_Constant(value)
        elif isinstance(y, backend_Constant):
            value = y
        else:
            raise TypeError(f"Unexpected type: {type(y)}")
        self.assign(value, annotate=False, tlm=False)

    def _axpy(self, alpha, x, /):
        dtype = function_dtype(self)
        alpha = dtype(alpha)
        if isinstance(x, SymbolicFloat):
            x = x.value()
        if isinstance(x, (int, np.integer,
                          float, np.floating,
                          complex, np.complexfloating)):
            if len(self.ufl_shape) == 0:
                value = (dtype(self) + alpha * dtype(x))
            else:
                value = self.values() + alpha * dtype(x)
                value.shape = self.ufl_shape
                value = backend_Constant(value)
        elif isinstance(x, backend_Constant):
            if len(self.ufl_shape) == 0:
                value = (dtype(self) + alpha * dtype(x))
            else:
                value = self.values() + alpha * x.values()
                value.shape = self.ufl_shape
                value = backend_Constant(value)
        else:
            raise TypeError(f"Unexpected type: {type(x)}")
        self.assign(value, annotate=False, tlm=False)

    def _inner(self, y):
        if isinstance(y, backend_Constant):
            return y.values().conjugate().dot(self.values())
        else:
            raise TypeError(f"Unexpected type: {type(y)}")

    def _sum(self):
        return self.values().sum()

    def _linf_norm(self):
        return abs(self.values()).max()

    def _local_size(self):
        comm = function_comm(self)
        if comm.rank == 0:
            if len(self.ufl_shape) == 0:
                return 1
            else:
                return np.prod(self.ufl_shape)
        else:
            return 0

    def _global_size(self):
        if len(self.ufl_shape) == 0:
            return 1
        else:
            return np.prod(self.ufl_shape)

    def _local_indices(self):
        comm = function_comm(self)
        if comm.rank == 0:
            if len(self.ufl_shape) == 0:
                return slice(0, 1)
            else:
                return slice(0, np.prod(self.ufl_shape))
        else:
            return slice(0, 0)

    def _get_values(self):
        comm = function_comm(self)
        if comm.rank == 0:
            values = self.values().view()
        else:
            values = np.array([], dtype=function_dtype(self))
        values.setflags(write=False)
        return values

    def _set_values(self, values):
        if not np.can_cast(values, function_dtype(self)):
            raise ValueError("Invalid dtype")
        comm = function_comm(self)
        if comm.rank != 0:
            values = None
        values = comm.bcast(values, root=0)
        if len(self.ufl_shape) == 0:
            values.shape = (1,)
            self.assign(values[0], annotate=False, tlm=False)
        else:
            values.shape = self.ufl_shape
            self.assign(backend_Constant(values), annotate=False, tlm=False)

    def _replacement(self):
        if not hasattr(self, "_tlm_adjoint__replacement"):
            self._tlm_adjoint__replacement = ReplacementConstant(self)
        return self._tlm_adjoint__replacement

    def _is_replacement(self):
        return False

    def _is_scalar(self):
        return len(self.ufl_shape) == 0

    def _scalar_value(self):
        # assert function_is_scalar(self)
        return function_dtype(self)(self)

    def _is_alias(self):
        return "alias" in self._tlm_adjoint__function_interface_attrs


class Constant(backend_Constant):
    def __init__(self, value=None, *args, name=None, domain=None, space=None,
                 space_type="primal", shape=None, comm=None, static=False,
                 cache=None, checkpoint=None, **kwargs):
        if space_type not in ["primal", "conjugate", "dual", "conjugate_dual"]:
            raise ValueError("Invalid space type")

        if domain is None and space is not None:
            domains = space.ufl_domains()
            if len(domains) > 0:
                domain, = domains

        # Shape initialization / checking
        if space is not None:
            if shape is None:
                shape = space.ufl_element().value_shape()
            elif shape != space.ufl_element().value_shape():
                raise ValueError("Invalid shape")
        if value is None:
            if shape is None:
                shape = tuple()
        elif shape is not None:
            value_ = value
            if not isinstance(value_, np.ndarray):
                value_ = np.array(value_)
            if value_.shape != shape:
                raise ValueError("Invalid shape")
            del value_

        # Default value
        if value is None:
            if len(shape) == 0:
                value = 0.0
            else:
                value = np.zeros(shape, dtype=backend_ScalarType)

        # Default comm
        if comm is None:
            if space is None:
                comm = DEFAULT_COMM
            else:
                comm = comm_parent(space_comm(space))

        if cache is None:
            cache = static
        if checkpoint is None:
            checkpoint = not static

        super().__init__(value, *args, name=name, domain=domain, space=space,
                         comm=comm, **kwargs)
        self._tlm_adjoint__function_interface_attrs.d_setitem("space_type", space_type)  # noqa: E501
        self._tlm_adjoint__function_interface_attrs.d_setitem("static", static)
        self._tlm_adjoint__function_interface_attrs.d_setitem("cache", cache)
        self._tlm_adjoint__function_interface_attrs.d_setitem("checkpoint", checkpoint)  # noqa: E501


class Function(backend_Function):
    def __init__(self, *args, space_type="primal", static=False, cache=None,
                 checkpoint=None, **kwargs):
        if space_type not in ["primal", "conjugate", "dual", "conjugate_dual"]:
            raise ValueError("Invalid space type")
        if cache is None:
            cache = static
        if checkpoint is None:
            checkpoint = not static

        super().__init__(*args, **kwargs)
        self._tlm_adjoint__function_interface_attrs.d_setitem("space_type", space_type)  # noqa: E501
        self._tlm_adjoint__function_interface_attrs.d_setitem("static", static)
        self._tlm_adjoint__function_interface_attrs.d_setitem("cache", cache)
        self._tlm_adjoint__function_interface_attrs.d_setitem("checkpoint", checkpoint)  # noqa: E501


class Zero:
    def _tlm_adjoint__function_interface_assign(self, y):
        raise RuntimeError("Cannot call _assign interface of Zero")

    def _tlm_adjoint__function_interface_axpy(self, alpha, x, /):
        raise RuntimeError("Cannot call _axpy interface of Zero")

    def _tlm_adjoint__function_interface_set_values(self, values):
        raise RuntimeError("Cannot call _set_values interface of Zero")


class ZeroConstant(Constant, Zero):
    def __init__(self, *, name=None, domain=None, space_type="primal",
                 shape=None, comm=None):
        Constant.__init__(
            self, name=name, domain=domain, space_type=space_type, shape=shape,
            comm=comm, static=True)

    def assign(self, *args, **kwargs):
        raise RuntimeError("Cannot call assign method of ZeroConstant")


class ZeroFunction(Function, Zero):
    def __init__(self, space, *, name=None, space_type="primal"):
        Function.__init__(
            self, space, name=name, space_type=space_type, static=True)

    def assign(self, *args, **kwargs):
        raise RuntimeError("Cannot call assign method of ZeroFunction")

    def interpolate(self, *args, **kwargs):
        raise RuntimeError("Cannot call interpolate method of ZeroFunction")

    def project(self, *args, **kwargs):
        raise RuntimeError("Cannot call project method of ZeroFunction")


def extract_coefficients(expr):
    if isinstance(expr, ufl.classes.Form):
        return expr.coefficients()
    else:
        return ufl.algorithms.extract_coefficients(expr)


def eliminate_zeros(expr, *, force_non_empty_form=False):
    if isinstance(expr, ufl.classes.Form) \
            and "_tlm_adjoint__simplified_form" in expr._cache:
        simplified_expr = expr._cache["_tlm_adjoint__simplified_form"]
    else:
        replace_map = {}
        for c in extract_coefficients(expr):
            if isinstance(c, Zero):
                replace_map[c] = ufl.classes.Zero(shape=c.ufl_shape)

        if len(replace_map) == 0:
            simplified_expr = expr
        else:
            simplified_expr = ufl.replace(expr, replace_map)

        if isinstance(expr, ufl.classes.Form):
            expr._cache["_tlm_adjoint__simplified_form"] = simplified_expr

    if force_non_empty_form \
            and isinstance(simplified_expr, ufl.classes.Form) \
            and simplified_expr.empty():
        if "_tlm_adjoint__simplified_form_non_empty" in expr._cache:
            simplified_expr = expr._cache["_tlm_adjoint__simplified_form_non_empty"]  # noqa: E501
        else:
            # Inefficient, but it is very difficult to generate a non-empty but
            # zero valued form
            arguments = expr.arguments()
            if isinstance(expr, ufl.classes.Expr):
                domain, = ufl.domain.extract_domains(expr)
            else:
                domain, = expr.ufl_domains()
            zero = ZeroConstant(domain=domain)
            if len(arguments) == 0:
                simplified_expr = zero * ufl.ds
            elif len(arguments) == 1:
                test, = arguments
                simplified_expr = zero * ufl.conj(test) * ufl.ds
            else:
                test, trial = arguments
                simplified_expr = zero * ufl.inner(trial, test) * ufl.ds

            if isinstance(expr, ufl.classes.Form):
                expr._cache["_tlm_adjoint__simplified_form_non_empty"] = simplified_expr  # noqa: E501

    return simplified_expr


class DirichletBC(backend_DirichletBC):
    # Based on FEniCS 2019.1.0 DirichletBC API
    def __init__(self, V, g, sub_domain, *args, static=None, cache=None,
                 homogeneous=None, _homogeneous=None, **kwargs):
        super().__init__(V, g, sub_domain, *args, **kwargs)

        if static is None:
            static = True
            for dep in ufl.algorithms.extract_coefficients(
                    g if isinstance(g, ufl.classes.Expr)
                    else backend_Constant(g)):
                # The 'static' flag for functions is only a hint. 'not
                # checkpointed' is a guarantee that the function will never
                # appear as the solution to an Equation.
                if not is_function(dep) or not function_is_checkpointed(dep):
                    static = False
                    break
        if cache is None:
            cache = static
        if homogeneous is not None:
            warnings.warn("homogeneous argument is deprecated -- "
                          "use HomogeneousDirichletBC instead",
                          DeprecationWarning, stacklevel=2)
            if _homogeneous is not None:
                raise TypeError("Cannot supply both homogeneous and "
                                "_homogeneous arguments")
        elif _homogeneous is None:
            homogeneous = False
        else:
            homogeneous = _homogeneous

        self.__static = static
        self.__cache = cache
        self.__homogeneous = homogeneous

    def is_static(self):
        return self.__static

    def is_cached(self):
        return self.__cache

    def is_homogeneous(self):
        return self.__homogeneous

    def homogenize(self):
        if self.is_static():
            raise RuntimeError("Cannot call homogenize method for static "
                               "DirichletBC")
        if not self.__homogeneous:
            super().homogenize()
            self.__homogeneous = True

    def set_value(self, *args, **kwargs):
        if self.is_static():
            raise RuntimeError("Cannot call set_value method for static "
                               "DirichletBC")
        super().set_value(*args, **kwargs)


class HomogeneousDirichletBC(DirichletBC):
    # Based on FEniCS 2019.1.0 DirichletBC API
    def __init__(self, V, sub_domain, *args, **kwargs):
        shape = V.ufl_element().value_shape()
        if len(shape) == 0:
            g = 0.0
        else:
            g = np.zeros(shape, dtype=backend_ScalarType)
        super().__init__(V, g, sub_domain, *args, static=True,
                         _homogeneous=True, **kwargs)


def bcs_is_static(bcs):
    for bc in bcs:
        if not hasattr(bc, "is_static") or not bc.is_static():
            return False
    return True


def bcs_is_cached(bcs):
    for bc in bcs:
        if not hasattr(bc, "is_cached") or not bc.is_cached():
            return False
    return True


def bcs_is_homogeneous(bcs):
    for bc in bcs:
        if not hasattr(bc, "is_homogeneous") or not bc.is_homogeneous():
            return False
    return True


class ReplacementInterface(_FunctionInterface):
    def _space(self):
        return self.ufl_function_space()

    def _space_type(self):
        return self._tlm_adjoint__function_interface_attrs["space_type"]

    def _id(self):
        return self._tlm_adjoint__function_interface_attrs["id"]

    def _name(self):
        return self._tlm_adjoint__function_interface_attrs["name"]

    def _state(self):
        return -1

    def _is_static(self):
        return self._tlm_adjoint__function_interface_attrs["static"]

    def _is_cached(self):
        return self._tlm_adjoint__function_interface_attrs["cache"]

    def _is_checkpointed(self):
        return self._tlm_adjoint__function_interface_attrs["checkpoint"]

    def _caches(self):
        return self._tlm_adjoint__function_interface_attrs["caches"]

    def _replacement(self):
        return self

    def _is_replacement(self):
        return True


class Replacement(ufl.classes.Coefficient):
    def __init__(self, x):
        space = function_space(x)

        x_domains = x.ufl_domains()
        if len(x_domains) == 0:
            domain = None
        else:
            domain, = x_domains

        super().__init__(space, count=new_count())
        self.__domain = domain
        add_interface(self, ReplacementInterface,
                      {"id": function_id(x), "name": function_name(x),
                       "space": space,
                       "space_type": function_space_type(x),
                       "static": function_is_static(x),
                       "cache": function_is_cached(x),
                       "checkpoint": function_is_checkpointed(x),
                       "caches": function_caches(x)})

    def ufl_domain(self):
        return self.__domain

    def ufl_domains(self):
        if self.__domain is None:
            return ()
        else:
            return (self.__domain,)


class ReplacementConstant(backend_Constant, Replacement):
    def __init__(self, x):
        Replacement.__init__(self, x)


class ReplacementFunction(backend_Function, Replacement):
    def __init__(self, x):
        Replacement.__init__(self, x)


def replaced_form(form):
    replace_map = {}
    for c in form.coefficients():
        if is_function(c):
            replace_map[c] = function_replacement(c)
    return ufl.replace(form, replace_map)


def define_function_alias(x, parent, *, key):
    if x is not parent:
        if "alias" in x._tlm_adjoint__function_interface_attrs:
            alias_parent, alias_key = x._tlm_adjoint__function_interface_attrs["alias"]  # noqa: E501
            alias_parent = alias_parent()
            if alias_parent is None or alias_parent is not parent \
                    or alias_key != key:
                raise ValueError("Invalid alias data")
        else:
            x._tlm_adjoint__function_interface_attrs["alias"] \
                = (weakref.ref(parent), key)
            x._tlm_adjoint__function_interface_attrs.d_setitem(
                "space_type", function_space_type(parent))
            x._tlm_adjoint__function_interface_attrs.d_setitem(
                "static", function_is_static(parent))
            x._tlm_adjoint__function_interface_attrs.d_setitem(
                "cache", function_is_cached(parent))
            x._tlm_adjoint__function_interface_attrs.d_setitem(
                "checkpoint", function_is_checkpointed(parent))
