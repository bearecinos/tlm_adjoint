#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from .interface import conjugate_dual_space_type, function_id, \
    function_new_conjugate_dual, function_replacement, function_space, \
    function_space_type, function_zero, is_function, space_new

from .alias import WeakAlias
from .equation import Equation, Referrer, ZeroAssignment

from collections.abc import Sequence
import inspect
import warnings

__all__ = \
    [
        "LinearEquation",
        "Matrix",
        "RHS"
    ]


class LinearEquation(Equation):
    r"""Represents the solution of a linear equation

    .. math::

        A x = \sum_i b_i,

    with left-hand-side matrix :math:`A` and right-hand-side terms :math:`b_i`.
    The matrix and right-hand-side terms may depend on other forward variables
    :math:`y_i`.

    The forward residual is defined

    .. math::

        \mathcal{F} \left( x, y_1, y_2, \ldots \right) = A x - b.

    :arg X: A function or a :class:`Sequence` of functions defining the forward
        solution `x`.
    :arg B: A :class:`tlm_adjoint.linear_equation.RHS` or a :class:`Sequence`
        of :class:`tlm_adjoint.linear_equation.RHS` objects defining the
        right-hand-side terms.
    :arg A: A :class:`Matrix` defining the left-hand-side matrix. Defaults to
        an identity matrix if not supplied.
    :arg adj_type: The space type relative to `X` of adjoint variables.
        `'primal'` or `'conjugate_dual'`, or a :class:`Sequence` of these.
    """

    def __init__(self, X, B, *, A=None, adj_type=None):
        if isinstance(X, RHS) \
                or (isinstance(X, Sequence) and len(X) > 0
                    and isinstance(X[0], RHS)):
            warnings.warn("LinearEquation(B, X, *, A=None, adj_type=None) "
                          "signature is deprecated -- use "
                          "LinearEquation(X, B, *, A=None, adj_type=None) "
                          "instead",
                          DeprecationWarning, stacklevel=2)
            X, B = B, X

        if is_function(X):
            X = (X,)
        if isinstance(B, RHS):
            B = (B,)
        if adj_type is None:
            if A is None:
                adj_type = "conjugate_dual"
            else:
                adj_type = "primal"

        deps = []
        dep_ids = {}
        nl_deps = []
        nl_dep_ids = {}

        x_ids = set()
        for x in X:
            x_id = function_id(x)
            if x_id in x_ids:
                raise ValueError("Duplicate solve")
            x_ids.add(x_id)
            deps.append(x)
            dep_ids[x_id] = len(deps) - 1

        b_dep_indices = tuple([] for b in B)
        b_nl_dep_indices = tuple([] for b in B)

        for i, b in enumerate(B):
            for dep in b.dependencies():
                dep_id = function_id(dep)
                if dep_id in x_ids:
                    raise ValueError("Invalid dependency in linear Equation")
                if dep_id not in dep_ids:
                    deps.append(dep)
                    dep_ids[dep_id] = len(deps) - 1
                b_dep_indices[i].append(dep_ids[dep_id])
            for dep in b.nonlinear_dependencies():
                dep_id = function_id(dep)
                if dep_id not in nl_dep_ids:
                    nl_deps.append(dep)
                    nl_dep_ids[dep_id] = len(nl_deps) - 1
                b_nl_dep_indices[i].append(nl_dep_ids[dep_id])

        b_dep_ids = tuple({function_id(b_dep): i
                           for i, b_dep in enumerate(b.dependencies())}
                          for b in B)

        if A is not None:
            A_dep_indices = []
            A_nl_dep_indices = []
            for dep in A.nonlinear_dependencies():
                dep_id = function_id(dep)
                if dep_id not in dep_ids:
                    deps.append(dep)
                    dep_ids[dep_id] = len(deps) - 1
                A_dep_indices.append(dep_ids[dep_id])
                if dep_id not in nl_dep_ids:
                    nl_deps.append(dep)
                    nl_dep_ids[dep_id] = len(nl_deps) - 1
                A_nl_dep_indices.append(nl_dep_ids[dep_id])

            A_nl_dep_ids = {function_id(A_nl_dep): i
                            for i, A_nl_dep
                            in enumerate(A.nonlinear_dependencies())}

            if len(A.nonlinear_dependencies()) > 0:
                A_x_indices = []
                for x in X:
                    x_id = function_id(x)
                    if x_id not in nl_dep_ids:
                        nl_deps.append(x)
                        nl_dep_ids[x_id] = len(nl_deps) - 1
                    A_x_indices.append(nl_dep_ids[x_id])

        del x_ids, dep_ids, nl_dep_ids

        super().__init__(
            X, deps, nl_deps=nl_deps,
            ic=A is not None and A.has_initial_condition(),
            adj_ic=A is not None and A.adjoint_has_initial_condition(),
            adj_type=adj_type)
        self._B = tuple(B)
        self._b_dep_indices = b_dep_indices
        self._b_nl_dep_indices = b_nl_dep_indices
        self._b_dep_ids = b_dep_ids
        self._A = A
        if A is not None:
            self._A_dep_indices = A_dep_indices
            self._A_nl_dep_indices = A_nl_dep_indices
            self._A_nl_dep_ids = A_nl_dep_ids
            if len(A.nonlinear_dependencies()) > 0:
                self._A_x_indices = A_x_indices

        self.add_referrer(*B)
        if A is not None:
            self.add_referrer(A)

    def drop_references(self):
        super().drop_references()
        self._B = tuple(WeakAlias(b) for b in self._B)
        if self._A is not None:
            self._A = WeakAlias(self._A)

    def forward_solve(self, X, deps=None):
        if is_function(X):
            X = (X,)
        if deps is None:
            deps = self.dependencies()

        if self._A is None:
            for x in X:
                function_zero(x)
            B = X
        else:
            def b_space_type(m):
                space_type = function_space_type(
                    self.X(m), rel_space_type=self.adj_X_type(m))
                return conjugate_dual_space_type(space_type)

            B = tuple(space_new(function_space(x), space_type=b_space_type(m))
                      for m, x in enumerate(X))

        for i, b in enumerate(self._B):
            b.add_forward(B[0] if len(B) == 1 else B,
                          [deps[j] for j in self._b_dep_indices[i]])

        if self._A is not None:
            self._A.forward_solve(X[0] if len(X) == 1 else X,
                                  [deps[j] for j in self._A_dep_indices],
                                  B[0] if len(B) == 1 else B)

    _reset_adjoint_warning = False

    def reset_adjoint(self):
        if self._A is not None:
            self._A.reset_adjoint()

    _initialize_adjoint_warning = False

    def initialize_adjoint(self, J, nl_deps):
        if self._A is not None:
            self._A.initialize_adjoint(J, nl_deps)

    _finalize_adjoint_warning = False

    def finalize_adjoint(self, J):
        if self._A is not None:
            self._A.finalize_adjoint(J)

    def adjoint_jacobian_solve(self, adj_X, nl_deps, B):
        if self._A is None:
            return B
        else:
            return self._A.adjoint_solve(
                adj_X, [nl_deps[j] for j in self._A_nl_dep_indices], B)

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_X):
        if is_function(adj_X):
            adj_X = (adj_X,)

        eq_deps = self.dependencies()
        if dep_index < 0 or dep_index >= len(eq_deps):
            raise IndexError("dep_index out of bounds")
        elif dep_index < len(self.X()):
            if self._A is None:
                return adj_X[dep_index]
            else:
                dep = eq_deps[dep_index]
                F = function_new_conjugate_dual(dep)
                self._A.adjoint_action([nl_deps[j]
                                        for j in self._A_nl_dep_indices],
                                       adj_X[0] if len(adj_X) == 1 else adj_X,
                                       F, b_index=dep_index, method="assign")
                return F
        else:
            dep = eq_deps[dep_index]
            dep_id = function_id(dep)
            F = function_new_conjugate_dual(dep)
            assert len(self._B) == len(self._b_dep_ids)
            for i, (b, b_dep_ids) in enumerate(zip(self._B, self._b_dep_ids)):
                if dep_id in b_dep_ids:
                    b_dep_index = b_dep_ids[dep_id]
                else:
                    continue
                b_nl_deps = [nl_deps[j] for j in self._b_nl_dep_indices[i]]
                b.subtract_adjoint_derivative_action(
                    b_nl_deps, b_dep_index,
                    adj_X[0] if len(adj_X) == 1 else adj_X,
                    F)
            if self._A is not None and dep_id in self._A_nl_dep_ids:
                A_nl_dep_index = self._A_nl_dep_ids[dep_id]
                A_nl_deps = [nl_deps[j] for j in self._A_nl_dep_indices]
                X = [nl_deps[j] for j in self._A_x_indices]
                self._A.adjoint_derivative_action(
                    A_nl_deps, A_nl_dep_index,
                    X[0] if len(X) == 1 else X,
                    adj_X[0] if len(adj_X) == 1 else adj_X,
                    F, method="add")
            return F

    def tangent_linear(self, M, dM, tlm_map):
        X = self.X()

        if self._A is None:
            tlm_B = []
        else:
            tlm_B = self._A.tangent_linear_rhs(M, dM, tlm_map,
                                               X[0] if len(X) == 1 else X)
            if tlm_B is None:
                tlm_B = []
            elif isinstance(tlm_B, RHS):
                tlm_B = [tlm_B]
        for b in self._B:
            tlm_b = b.tangent_linear_rhs(M, dM, tlm_map)
            if tlm_b is None:
                pass
            elif isinstance(tlm_b, RHS):
                tlm_B.append(tlm_b)
            else:
                tlm_B.extend(tlm_b)

        if len(tlm_B) == 0:
            return ZeroAssignment([tlm_map[x] for x in self.X()])
        else:
            return LinearEquation([tlm_map[x] for x in self.X()], tlm_B,
                                  A=self._A, adj_type=self.adj_X_type())


class Matrix(Referrer):
    r"""Represents a matrix :math:`A`.

    This is an abstract base class. Information required by forward, adjoint,
    and tangent-linear calculations is provided by overloading abstract
    methods. This class does *not* inherit from :class:`abc.ABC`, so that
    methods may be implemented as needed.

    :arg nl_deps: A :class:`Sequence` of functions, defining dependencies of
        the matrix :math:`A`.
    :arg ic: Whether solution of a linear equation :math:`A x = b` for
        :math:`x` uses an initial guess. Defaults to `True`.
    :arg adj_ic: Whether solution of an adjoint linear equation :math:`A^*
        \lambda = b` for :math:`\lambda` uses an initial guess.
    """

    def __init__(self, nl_deps=None, *, has_ic_dep=None, ic=None, adj_ic=True):
        if nl_deps is None:
            nl_deps = []
        if len({function_id(dep) for dep in nl_deps}) != len(nl_deps):
            raise ValueError("Duplicate non-linear dependency")

        if has_ic_dep is not None:
            warnings.warn("has_ic_dep argument is deprecated -- use ic "
                          "instead",
                          DeprecationWarning, stacklevel=2)
            if ic is not None:
                raise TypeError("Cannot pass both has_ic_dep and ic arguments")
            ic = has_ic_dep
        elif ic is None:
            ic = True

        super().__init__()
        self._nl_deps = tuple(nl_deps)
        self._ic = ic
        self._adj_ic = adj_ic

    _reset_adjoint_warning = True
    _initialize_adjoint_warning = True
    _finalize_adjoint_warning = True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        if hasattr(cls, "reset_adjoint"):
            if cls._reset_adjoint_warning:
                warnings.warn("Matrix.reset_adjoint method is deprecated",
                              DeprecationWarning, stacklevel=2)
        else:
            cls._reset_adjoint_warning = False
            cls.reset_adjoint = lambda self: None

        if hasattr(cls, "initialize_adjoint"):
            if cls._initialize_adjoint_warning:
                warnings.warn("Matrix.initialize_adjoint method is "
                              "deprecated",
                              DeprecationWarning, stacklevel=2)
        else:
            cls._initialize_adjoint_warning = False
            cls.initialize_adjoint = lambda self, J, nl_deps: None

        if hasattr(cls, "finalize_adjoint"):
            if cls._finalize_adjoint_warning:
                warnings.warn("Matrix.finalize_adjoint method is deprecated",
                              DeprecationWarning, stacklevel=2)
        else:
            cls._finalize_adjoint_warning = False
            cls.finalize_adjoint = lambda self, J: None

        adj_solve_sig = inspect.signature(cls.adjoint_solve)
        if tuple(adj_solve_sig.parameters.keys()) in [("self", "nl_deps", "b"),
                                                      ("self", "nl_deps", "B")]:  # noqa: E501
            warnings.warn("Matrix.adjoint_solve(self, nl_deps, b/B) method "
                          "signature is deprecated",
                          DeprecationWarning, stacklevel=2)

            def adjoint_solve(self, adj_X, nl_deps, B):
                return adjoint_solve_orig(self, nl_deps, B)
            adjoint_solve_orig = cls.adjoint_solve
            cls.adjoint_solve = adjoint_solve

    def drop_references(self):
        self._nl_deps = tuple(function_replacement(dep)
                              for dep in self._nl_deps)

    def nonlinear_dependencies(self):
        """Return dependencies of the :class:`Matrix`.

        :returns: A :class:`Sequence` of functions defining dependencies.
        """

        return self._nl_deps

    def has_initial_condition_dependency(self):
        warnings.warn("Matrix.has_initial_condition_dependency method is "
                      "deprecated -- use Matrix.has_initial_condition instead",
                      DeprecationWarning, stacklevel=2)
        return self._ic

    def has_initial_condition(self):
        """Return whether solution of a linear equation :math:`A x = b` for
        :math:`x` uses an initial guess.

        :returns: `True` if an initial guess is used, and `False` otherwise.
        """

        return self._ic

    def adjoint_has_initial_condition(self):
        r"""Return whether solution of an adjoint linear equation :math:`A^*
        \lambda = b` for :math:`\lambda` uses an initial guess.

        :returns: `True` if an initial guess is used, and `False` otherwise.
        """

        return self._adj_ic

    def forward_action(self, nl_deps, X, B, *, method="assign"):
        """Evaluate the action of the matrix on :math:`x`, :math:`A x`. Assigns
        the result to `B`, or adds the result to or subtracts the result from
        `B`.

        :arg nl_deps: A :class:`Sequence` of functions defining values of
            non-linear dependencies. Should not be modified.
        :arg X: Defines :math:`x`. A function if it has a single component, and
            a :class:`Sequence` of functions otherwise. Should not be modified.
            Subclasses may replace this argument with `x` if there is a single
            component.
        :arg B: Stores the result. A function if it has a single component, and
            a :class:`Sequence` of functions otherwise. Subclasses may replace
            this argument with `b` if there is a single component.
        :arg method: If equal to `'assign'` then this method should set `B`
            equal to the result. If equal to `'add'` then this method should
            add the result to `B`. If equal to `'sub'` then this method should
            subtract the result from `B`.
        """

        raise NotImplementedError("Method not overridden")

    def adjoint_action(self, nl_deps, adj_X, b, b_index=0, *, method="assign"):
        r"""Evaluate the action of the adjoint of the matrix on
        :math:`\lambda`, :math:`A^* \lambda`. Assigns the `b_index` th
        component to `b`, or adds the `b_index` th component to or subtracts
        the `b_index` th component from `b`.

        :arg nl_deps: A :class:`Sequence` of functions defining values of
            non-linear dependencies. Should not be modified.
        :arg adj_X: Defines :math:`\lambda`. A function if it has a single
            component, and a :class:`Sequence` of functions otherwise. Should
            not be modified. Subclasses may replace this argument with `adj_x`
            if there is a single component.
        :arg b: A function storing the result. Should be updated by this
            method.
        :arg b_index: The component of the result which should be used to
            update `b`.
        :arg method: If equal to `'assign'` then this method should set `b`
            equal to the `b_index` th component of the result. If equal to
            `'add'` then this method should add the `b_index` th component of
            the result to `b`. If equal to `'sub'` then this method should
            subtract the `b_index` th component of the result from `b`.
        """

        raise NotImplementedError("Method not overridden")

    def forward_solve(self, X, nl_deps, B):
        """Solve the linear system :math:`A x = b` for :math:`x`.

        :arg X: The solution :math:`x`. A function if it has a single
            component, and a :class:`Sequence` of functions otherwise. May
            define an initial guess. Subclasses may replace this argument with
            `x` if there is a single component.
        :arg nl_deps: A :class:`Sequence` of functions defining values of
            non-linear dependencies. Should not be modified.
        :arg B: The right-hand-side :math:`b`. A function if it has a single
            component, and a :class:`Sequence` of functions otherwise. Should
            not be modified. Subclasses may replace this argument with `b` if
            there is a single component.
        """

        raise NotImplementedError("Method not overridden")

    def adjoint_derivative_action(self, nl_deps, nl_dep_index, X, adj_X, b, *,
                                  method="assign"):
        """Evaluate the action of the adjoint of a derivative of :math:`A x` on
        an adjoint variable. Assigns the result to `b`, or adds the result to
        or subtracts the result from `b`.

        :arg nl_deps: A :class:`Sequence` of functions defining values of
            non-linear dependencies. Should not be modified.
        :arg nl_deps_index: An :class:`int`. The derivative is defined by
            differentiation of :math:`A x` with respect to
            `self.nonlinear_dependencies()[nl_dep_index]`.
        :arg X: Defines :math:`x`. A function if it has a single component, and
            a :class:`Sequence` of functions otherwise. Should not be modified.
            Subclasses may replace this argument with `x` if there is a single
            component.
        :arg adj_X: The adjoint variable. A function if the adjoint variable
            has a single component, and :class:`Sequence` of functions
            otherwise. Should not be modified. Subclasses may replace this
            argument with `adj_x` if the adjoint variable has a single
            component.
        :arg b: A function storing the result. Should be updated by this
            method.
        :arg method: If equal to `'assign'` then this method should set `b`
            equal to the result. If equal to `'add'` then this method should
            add the result to `b`. If equal to `'sub'` then this method should
            subtract the result from `b`.
        """

        raise NotImplementedError("Method not overridden")

    def adjoint_solve(self, adj_X, nl_deps, B):
        r"""Solve the linear system :math:`A^* \lambda = b` for
        :math:`\lambda`.

        :arg adj_X: The solution :math:`\lambda`. A function if it has a single
            component, and a :class:`Sequence` of functions otherwise. May
            define an initial guess. Subclasses may replace this argument with
            `adj_x` if there is a single component.
        :arg nl_deps: A :class:`Sequence` of functions defining values of
            non-linear dependencies. Should not be modified.
        :arg B: The right-hand-side :math:`b`. A function if it has a single
            component, and a :class:`Sequence` of functions otherwise. Should
            not be modified. Subclasses may replace this argument with `b` if
            there is a single component.
        """

        raise NotImplementedError("Method not overridden")

    def tangent_linear_rhs(self, M, dM, tlm_map, X):
        r"""Construct tangent-linear right-hand-side terms obtained by
        differentiation of

        .. math::

            \mathcal{G} \left( x, y_1, y_2, \ldots \right) = -A x

        with respect to dependencies of the matrix :math:`A`. i.e. construct

        .. math::

            -\sum_i \frac{\partial \mathcal{G}}{\partial y_i} \tau_{y_i},

        where :math:`\tau_{y_i}` is the tangent-linear variable associated with
        the dependency :math:`y_i`. Note the *negative* sign. Does *not*
        include the term :math:`-A \tau_x` where :math:`\tau_x` is the
        tangent-linear variable associated with :math:`x`.

        :arg M: A :class:`Sequence` of functions defining the control.
        :arg dM: A :class:`Sequence` of functions defining the derivative
            direction. The tangent-linear model computes directional
            derivatives with respect to the control defined by `M` and with
            direction defined by `dM`.
        :arg tlm_map: A :class:`tlm_adjoint.tangent_linear.TangentLinearMap`
            storing values for tangent-linear variables.
        :arg X: Defines :math:`x`. A function if it has a single component, and
            a :class:`Sequence` of functions otherwise. Subclasses may replace
            this argument with `x` if there is a single component.
        :returns: A :class:`tlm_adjoint.linear_equation.RHS`, or a
            :class:`Sequence` of :class:`tlm_adjoint.linear_equation.RHS`
            objects, defining the right-hand-side terms. Returning `None`
            indicates that there are no terms.
        """

        raise NotImplementedError("Method not overridden")


class RHS(Referrer):
    """Represents a right-hand-side term.

    This is an abstract base class. Information required by forward, adjoint,
    and tangent-linear calculations is provided by overloading abstract
    methods. This class does *not* inherit from :class:`abc.ABC`, so that
    methods may be implemented as needed.

    :arg deps: A :class:`Sequence` of functions defining dependencies.
    :arg nl_deps: A :class:`Sequence` of functions defining non-linear
        dependencies.
    """

    def __init__(self, deps, nl_deps=None):
        dep_ids = set(map(function_id, deps))
        if len(dep_ids) != len(deps):
            raise ValueError("Duplicate dependency")

        if nl_deps is None:
            nl_deps = tuple(deps)
        nl_dep_ids = set(map(function_id, nl_deps))
        if len(nl_dep_ids) != len(nl_deps):
            raise ValueError("Duplicate non-linear dependency")
        if len(dep_ids.intersection(nl_dep_ids)) != len(nl_deps):
            raise ValueError("Non-linear dependency is not a dependency")

        super().__init__()
        self._deps = tuple(deps)
        self._nl_deps = tuple(nl_deps)

    def drop_references(self):
        self._deps = tuple(function_replacement(dep) for dep in self._deps)
        self._nl_deps = tuple(function_replacement(dep)
                              for dep in self._nl_deps)

    def dependencies(self):
        """Return dependencies of the :class:`tlm_adjoint.linear_equation.RHS`.

        :returns: A :class:`Sequence` of functions defining dependencies.
        """

        return self._deps

    def nonlinear_dependencies(self):
        """Return non-linear dependencies of the
        :class:`tlm_adjoint.linear_equation.RHS`.

        :returns: A :class:`Sequence` of functions defining non-linear
            dependencies.
        """

        return self._nl_deps

    def add_forward(self, B, deps):
        """Add the right-hand-side term to `B`.

        :arg B: A function if it has a single component, and a
            :class:`Sequence` of functions otherwise. Should be updated by the
            addition of this :class:`tlm_adjoint.linear_equation.RHS`.
            Subclasses may replace this argument with `b` if there is a single
            component.
        :arg deps: A :class:`Sequence` of functions defining values of
            dependencies. Should not be modified.
        """

        raise NotImplementedError("Method not overridden")

    def subtract_adjoint_derivative_action(self, nl_deps, dep_index, adj_X, b):
        """Subtract the action of the adjoint of a derivative of the
        right-hand-side term, on an adjoint variable, from `b`.

        :arg nl_deps: A :class:`Sequence` of functions defining values of
            non-linear dependencies. Should not be modified.
        :arg deps_index: An :class:`int`. The derivative is defined by
            differentiation of the right-hand-side term with respect to
            `self.dependencies()[dep_index]`.
        :arg adj_X: The adjoint variable. A function if the adjoint variable
            has a single component, and a :class:`Sequence` of functions
            otherwise. Should not be modified. Subclasses may replace this
            argument with `adj_x` if the adjoint variable has a single
            component.
        :arg b: A function storing the result. Should be updated by subtracting
            the action of the adjoint of the right-hand-side term on the
            adjoint variable.
        """

        raise NotImplementedError("Method not overridden")

    def tangent_linear_rhs(self, M, dM, tlm_map):
        r"""Construct tangent-linear right-hand-side terms obtained by
        differentiation of this right-hand-side term. That is, construct

        .. math::

            \sum_i \frac{\partial b}{\partial y_i} \tau_{y_i},

        where :math:`b` is this right-hand-side term, and :math:`\tau_{y_i}` is
        the tangent-linear variable associated with a dependency :math:`y_i`.

        :arg M: A :class:`Sequence` of functions defining the control.
        :arg dM: A :class:`Sequence` of functions defining the derivative
            direction. The tangent-linear model computes directional
            derivatives with respect to the control defined by `M` and with
            direction defined by `dM`.
        :arg tlm_map: A :class:`tlm_adjoint.tangent_linear.TangentLinearMap`
            storing values for tangent-linear variables.
        :returns: A :class:`tlm_adjoint.linear_equation.RHS`, or a
            :class:`Sequence` of :class:`tlm_adjoint.linear_equation.RHS`
            objects, defining the right-hand-side terms. Returning `None`
            indicates that there are no terms.
        """

        raise NotImplementedError("Method not overridden")
