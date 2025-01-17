#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""This module includes functionality for use with the tlm_adjoint FEniCS
backend.
"""

from .backend import Cell, LocalSolver, Mesh, MeshEditor, Point, \
    TestFunction, TrialFunction, backend_Function, backend_ScalarType, \
    parameters
from ..interface import check_space_type, function_assign, function_comm, \
    function_get_values, function_is_scalar, function_local_size, \
    function_new, function_new_conjugate_dual, function_scalar_value, \
    function_set_values, function_space, is_function, space_comm
from .backend_code_generator_interface import assemble

from ..caches import Cache
from ..equation import Equation, ZeroAssignment
from ..equations import MatrixActionRHS
from ..linear_equation import LinearEquation, Matrix
from ..tangent_linear import get_tangent_linear

from .caches import form_dependencies, form_key
from .equations import EquationSolver, bind_form, derivative, unbind_form, \
    unbound_form
from .functions import eliminate_zeros

import functools
import mpi4py.MPI as MPI
import numpy as np
import ufl
import warnings

__all__ = \
    [
        "LocalSolverCache",
        "local_solver_cache",
        "set_local_solver_cache",

        "Interpolation",
        "LocalProjection",
        "PointInterpolation",

        "InterpolationSolver",
        "LocalProjectionSolver",
        "PointInterpolationSolver"
    ]


def function_coords(x):
    space = function_space(x)
    return space.tabulate_dof_coordinates()


def has_ghost_cells(mesh):
    for cell in range(mesh.num_cells()):
        if Cell(mesh, cell).is_ghost():
            return True
    return False


def reorder_gps_disabled(fn):
    @functools.wraps(fn)
    def wrapped_fn(*args, **kwargs):
        reorder_vertices_gps = parameters["reorder_vertices_gps"]
        reorder_cells_gps = parameters["reorder_cells_gps"]
        parameters["reorder_vertices_gps"] = False
        parameters["reorder_cells_gps"] = False
        try:
            return fn(*args, **kwargs)
        finally:
            parameters["reorder_vertices_gps"] = reorder_vertices_gps
            parameters["reorder_cells_gps"] = reorder_cells_gps
    return wrapped_fn


@reorder_gps_disabled
def local_mesh(mesh):
    coordinates = mesh.coordinates()
    cells = mesh.cells()
    assert coordinates.shape[0] == mesh.num_vertices()
    assert cells.shape[0] == mesh.num_cells()

    local_vertex_map = {}
    full_vertex_map = []
    full_cell_map = []
    for full_cell in range(cells.shape[0]):
        if not Cell(mesh, full_cell).is_ghost():
            for full_vertex in cells[full_cell, :]:
                if full_vertex not in local_vertex_map:
                    local_vertex_map[full_vertex] = len(full_vertex_map)
                    full_vertex_map.append(full_vertex)
            full_cell_map.append(full_cell)

    l_mesh = Mesh(MPI.COMM_SELF)
    ed = MeshEditor()
    ed.open(mesh=l_mesh, type=mesh.cell_name(), tdim=mesh.topology().dim(),
            gdim=mesh.geometry().dim(), degree=mesh.geometry().degree())

    N_local_vertices = len(full_vertex_map)
    ed.init_vertices_global(N_local_vertices, N_local_vertices)

    N_local_cells = len(full_cell_map)
    ed.init_cells_global(N_local_cells, N_local_cells)

    for local_vertex, full_vertex in enumerate(full_vertex_map):
        ed.add_vertex(local_vertex, Point(*coordinates[full_vertex, :]))

    for local_cell, full_cell in enumerate(full_cell_map):
        local_vertices = [local_vertex_map[full_vertex]
                          for full_vertex in cells[full_cell, :]]
        ed.add_cell(local_cell, local_vertices)

    ed.close(order=True)
    l_mesh.init()

    return l_mesh, full_vertex_map, full_cell_map


def point_cells(coords, mesh):
    full_cells = np.full(coords.shape[0], -1, dtype=np.int64)
    distances = np.full(coords.shape[0], np.NAN, dtype=backend_ScalarType)

    if mesh.mpi_comm().size == 1 or not has_ghost_cells(mesh):
        full_tree = mesh.bounding_box_tree()
        for i in range(coords.shape[0]):
            point = Point(*coords[i, :])
            full_cell, distance = full_tree.compute_closest_entity(point)
            full_cells[i] = full_cell
            distances[i] = distance
    else:
        l_mesh, full_vertex_map, full_cell_map = local_mesh(mesh)
        local_tree = l_mesh.bounding_box_tree()
        for i in range(coords.shape[0]):
            point = Point(*coords[i, :])
            local_cell, distance = local_tree.compute_closest_entity(point)
            full_cells[i] = full_cell_map[local_cell]
            distances[i] = distance

    assert (full_cells[i] >= 0).all()
    assert (full_cells[i] < mesh.num_cells()).all()
    assert (distances >= 0.0).all()

    return full_cells, distances


def greedy_coloring(space):
    mesh = space.mesh()
    dofmap = space.dofmap()
    ownership_range = dofmap.ownership_range()
    N = ownership_range[1] - ownership_range[0]

    node_node_graph = tuple(set() for i in range(N))
    for i in range(mesh.num_cells()):
        if Cell(mesh, i).is_ghost():
            continue

        cell_nodes = dofmap.cell_dofs(i)
        for j in cell_nodes:
            for k in cell_nodes:
                if j != k:
                    node_node_graph[j].add(k)
    node_node_graph = tuple(sorted(nodes, reverse=True)
                            for nodes in node_node_graph)

    # A basic greedy coloring of the (process local) node-node graph, ordered
    # using an advancing front

    seen = np.full(N, False, dtype=bool)
    colors = np.full(N, -1, dtype=np.int64)
    i = 0
    while True:
        # Initialize the advancing front
        while i < N and colors[i] >= 0:
            i += 1
        if i == N:
            break  # All nodes have been considered
        front = [i]
        seen[i] = True
        while len(front) > 0:
            # Consider a new node, and the smallest non-negative available
            # color
            j = front.pop()
            neighbouring_colors = set(colors[node_node_graph[j]])
            color = 0
            while color in neighbouring_colors:
                color += 1
            colors[j] = color
            # Advance the front
            for k in node_node_graph[j]:
                if not seen[k]:
                    front.append(k)
                    seen[k] = True
        # If the graph is not connected then we need to restart the front with
        # a new starting node

    return colors


def local_solver_key(form, solver_type):
    return (form_key(form), solver_type)


class LocalSolverCache(Cache):
    """A :class:`tlm_adjoint.caches.Cache` for element-wise local block
    diagonal linear solvers.
    """

    def local_solver(self, form, solver_type=None, *,
                     replace_map=None):
        """Construct an element-wise local block diagonal linear solver and
        cache the result, or return a previously cached result.

        :arg form: An arity two UFL :class:`Form`, defining the element-wise
            local block diagonal matrix.
        :arg local_solver: DOLFIN :class:`LocalSolver.SolverType`. Defaults
            to `dolfin.LocalSolver.SolverType.LU`.
        :arg replace_map: A :class:`Mapping` defining a map from symbolic
            variables to values.
        :returns: A :class:`tuple` `(value_ref, value)`. `value` is a DOLFIN
            :class:`LocalSolver` and `value_ref` is a
            :class:`tlm_adjoint.caches.CacheRef` storing a reference to
            `value`.
        """

        if solver_type is None:
            solver_type = LocalSolver.SolverType.LU

        form = eliminate_zeros(form, force_non_empty_form=True)
        key = local_solver_key(form, solver_type)

        def value():
            if replace_map is None:
                assemble_form = form
            else:
                assemble_form = ufl.replace(form, replace_map)
            local_solver = LocalSolver(assemble_form, solver_type=solver_type)
            local_solver.factorize()
            return local_solver

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

    :arg x: A DOLFIN :class:`Function` defining the forward solution.
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

        local_solver_type = LocalSolver.SolverType.Cholesky

        super().__init__(
            lhs == rhs, x,
            form_compiler_parameters=form_compiler_parameters,
            solver_parameters={"linear_solver": "direct"},
            cache_jacobian=cache_jacobian,
            cache_rhs_assembly=cache_rhs_assembly,
            match_quadrature=match_quadrature,
            defer_adjoint_assembly=defer_adjoint_assembly)
        self._local_solver_type = local_solver_type

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
                    local_solver_cache().local_solver(self._lhs,
                                                      self._local_solver_type)
        else:
            local_solver = LocalSolver(self._lhs,
                                       solver_type=self._local_solver_type)

        local_solver.solve_local(x.vector(), b, function_space(x).dofmap())

    def adjoint_jacobian_solve(self, adj_x, nl_deps, b):
        if self._cache_jacobian:
            local_solver = self._forward_J_solver()
            if local_solver is None:
                self._forward_J_solver, local_solver = \
                    local_solver_cache().local_solver(self._lhs,
                                                      self._local_solver_type)
        else:
            local_solver = LocalSolver(self._lhs,
                                       solver_type=self._local_solver_type)

        adj_x = self.new_adj_x()
        local_solver.solve_local(adj_x.vector(), b.vector(),
                                 function_space(adj_x).dofmap())
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


def point_owners(x_coords, y_space, *,
                 tolerance=0.0):
    comm = space_comm(y_space)
    rank = comm.rank

    y_cells, distances_local = point_cells(x_coords, y_space.mesh())
    distances = np.full(x_coords.shape[0], np.NAN, dtype=distances_local.dtype)
    comm.Allreduce(distances_local, distances, op=MPI.MIN)

    owner_local = np.full(x_coords.shape[0], rank, dtype=np.int64)
    assert len(distances_local) == len(distances)
    for i, (distance_local, distance) in enumerate(zip(distances_local,
                                                       distances)):
        if distance_local != distance:
            y_cells[i] = -1
            owner_local[i] = -1
    owner = np.full(x_coords.shape[0], -1, dtype=np.int64)
    comm.Allreduce(owner_local, owner, op=MPI.MAX)

    for i in range(x_coords.shape[0]):
        if owner[i] == -1:
            raise RuntimeError("Unable to find owning process for point")
        if owner[i] == rank:
            if distances_local[i] > tolerance:
                raise RuntimeError("Unable to find owning process for point")
        else:
            y_cells[i] = -1

    return y_cells


def interpolation_matrix(x_coords, y, y_cells, y_colors):
    y_space = function_space(y)
    y_mesh = y_space.mesh()
    y_dofmap = y_space.dofmap()

    # Verify process locality assumption
    y_ownership_range = y_dofmap.ownership_range()
    for y_cell in range(y_mesh.num_cells()):
        owned = np.array([j >= y_ownership_range[0]
                          and j < y_ownership_range[1]
                          for j in [y_dofmap.local_to_global_index(i)
                                    for i in y_dofmap.cell_dofs(y_cell)]],
                         dtype=bool)
        if owned.any() and not owned.all():
            raise RuntimeError("Non-process-local node-node graph")

    y_colors_N = np.full((1,), -1, dtype=y_colors.dtype)
    comm = function_comm(y)
    comm.Allreduce(np.array([y_colors.max() + 1], dtype=y_colors.dtype),
                   y_colors_N, op=MPI.MAX)
    y_colors_N = y_colors_N[0]
    assert y_colors_N >= 0
    y_nodes = tuple([] for i in range(y_colors_N))
    for y_node, color in enumerate(y_colors):
        y_nodes[color].append(y_node)

    from scipy.sparse import dok_matrix
    P = dok_matrix((x_coords.shape[0], function_local_size(y)),
                   dtype=backend_ScalarType)

    y_v = function_new(y)
    for color, y_color_nodes in enumerate(y_nodes):
        y_v.vector()[y_color_nodes] = 1.0
        for x_node, y_cell in enumerate(y_cells):
            if y_cell < 0:
                # Skip -- x_node is owned by a different process
                continue
            if Cell(y_mesh, y_cell).is_ghost():
                raise RuntimeError("Cannot interpolate within a ghost cell")

            y_cell_nodes = y_dofmap.cell_dofs(y_cell)
            y_cell_colors = y_colors[y_cell_nodes].tolist()
            if color in y_cell_colors:
                i = y_cell_colors.index(color)
            else:
                continue
            y_node = y_cell_nodes[i]
            x_v = np.full((1,), np.NAN, dtype=backend_ScalarType)
            y_v.eval_cell(x_v, x_coords[x_node, :], Cell(y_mesh, y_cell))
            P[x_node, y_node] = x_v[0]
        y_v.vector()[y_color_nodes] = 0.0

    return P.tocsr()


class LocalMatrix(Matrix):
    def __init__(self, P):
        super().__init__(nl_deps=[], ic=False, adj_ic=False)
        self._P = P.copy()
        self._P_T = P.T

    def forward_action(self, nl_deps, x, b, *, method="assign"):
        if method == "assign":
            function_set_values(b, self._P.dot(function_get_values(x)))
        elif method == "add":
            b.vector()[:] += self._P.dot(function_get_values(x))
        elif method == "sub":
            b.vector()[:] -= self._P.dot(function_get_values(x))
        else:
            raise ValueError(f"Invalid method: '{method:s}'")

    def adjoint_action(self, nl_deps, adj_x, b, b_index=0, *, method="assign"):
        if b_index != 0:
            raise IndexError("Invalid index")
        if method == "assign":
            function_set_values(
                b, self._P_T.dot(function_get_values(adj_x)))
        elif method == "add":
            b.vector()[:] += self._P_T.dot(function_get_values(adj_x))
        elif method == "sub":
            b.vector()[:] -= self._P_T.dot(function_get_values(adj_x))
        else:
            raise ValueError(f"Invalid method: '{method:s}'")


class InterpolationMatrix(LocalMatrix):
    def __init__(self, *args, **kwargs):
        warnings.warn("InterpolationMatrix class is deprecated -- "
                      "use LocalMatrix instead",
                      DeprecationWarning, stacklevel=2)
        super().__init__(*args, **kwargs)


class Interpolation(LinearEquation):
    r"""Represents interpolation of the scalar-valued function `y` onto the
    space for `x`.

    The forward residual :math:`\mathcal{F}` is defined so that :math:`\partial
    \mathcal{F} / \partial x` is the identity.

    Internally this builds (or uses a supplied) interpolation matrix for the
    local process *only*. This behaves correctly if the there are no edges
    between owned and non-owned nodes in the degree of freedom graph associated
    with the discrete function space for `y`.

    :arg x: A scalar-valued DOLFIN :class:`Function` defining the forward
        solution.
    :arg y: A scalar-valued DOLFIN :class:`Function` to interpolate onto the
        space for `x`.
    :arg X_coords: A NumPy :class:`ndarray` defining the coordinates at which
        to interpolate `y`. Shape is `(n, d)` where `n` is the number of
        process local degrees of freedom for `x` and `d` is the geometric
        dimension. Defaults to the process local degree of freedom locations
        for `x`. Ignored if `P` is supplied.
    :arg P: The interpolation matrix. A SciPy :class:`spmatrix`.
    :arg tolerance: Maximum permitted distance of an interpolation point from
        a cell in the mesh for `y`. Ignored if `P` is supplied.
    """

    def __init__(self, x, y, *, x_coords=None, P=None,
                 tolerance=0.0):
        check_space_type(x, "primal")
        check_space_type(y, "primal")

        if not isinstance(x, backend_Function):
            raise TypeError("Solution must be a Function")
        if len(x.ufl_shape) > 0:
            raise ValueError("Solution must be a scalar-valued Function")
        if not isinstance(y, backend_Function):
            raise TypeError("y must be a Function")
        if len(y.ufl_shape) > 0:
            raise ValueError("y must be a scalar-valued Function")
        if (x_coords is not None) and (function_comm(x).size > 1):
            raise TypeError("Cannot prescribe x_coords in parallel")

        if P is None:
            y_space = function_space(y)

            if x_coords is None:
                x_coords = function_coords(x)

            y_cells, y_distances = point_cells(x_coords, y_space.mesh())
            if (y_distances > tolerance).any():
                raise RuntimeError("Unable to locate one or more cells")

            y_colors = greedy_coloring(y_space)
            P = interpolation_matrix(x_coords, y, y_cells, y_colors)
        else:
            P = P.copy()

        super().__init__(
            x, MatrixActionRHS(LocalMatrix(P), y))


class InterpolationSolver(Interpolation):
    ""

    def __init__(self, y, x, x_coords=None, y_colors=None, P=None, P_T=None,
                 tolerance=0.0):
        if y_colors is not None:
            warnings.warn("y_colors argument is deprecated and has no effect",
                          DeprecationWarning, stacklevel=2)
        if P_T is not None:
            warnings.warn("P_T argument is deprecated and has no effect",
                          DeprecationWarning, stacklevel=2)
        warnings.warn("InterpolationSolver is deprecated -- "
                      "use Interpolation instead",
                      DeprecationWarning, stacklevel=2)
        super().__init__(x, y, x_coords=x_coords, P=P,
                         tolerance=tolerance)


class PointInterpolation(Equation):
    r"""Represents interpolation of a scalar-valued function at given points.

    The forward residual :math:`\mathcal{F}` is defined so that :math:`\partial
    \mathcal{F} / \partial x` is the identity.

    Internally this builds (or uses a supplied) interpolation matrix for the
    local process *only*. This behaves correctly if the there are no edges
    between owned and non-owned nodes in the degree of freedom graph associated
    with the discrete function space for `y`.

    :arg X: A scalar function, or a :class:`Sequence` of scalar functions,
        defining the forward solution.
    :arg y: A scalar-valued DOLFIN :class:`Function` to interpolate.
    :arg X_coords: A NumPy :class:`ndarray` defining the coordinates at which
        to interpolate `y`. Shape is `(n, d)` where `n` is the number of
        interpolation points and `d` is the geometric dimension. Ignored if `P`
        is supplied.
    :arg P: The interpolation matrix. A SciPy :class:`spmatrix`.
    :arg tolerance: Maximum permitted distance of an interpolation point from
        a cell in the mesh for `y`. Ignored if `P` is supplied.
    """

    def __init__(self, X, y, X_coords=None, *,
                 P=None, tolerance=0.0):
        if is_function(X):
            X = (X,)
        for x in X:
            check_space_type(x, "primal")
            if not function_is_scalar(x):
                raise ValueError("Solution must be a scalar, or a sequence of "
                                 "scalars")
        check_space_type(y, "primal")

        if X_coords is None:
            if P is None:
                raise TypeError("X_coords required when P is not supplied")
        else:
            if len(X) != X_coords.shape[0]:
                raise ValueError("Invalid number of functions")
        if not isinstance(y, backend_Function):
            raise TypeError("y must be a Function")
        if len(y.ufl_shape) > 0:
            raise ValueError("y must be a scalar-valued Function")

        if P is None:
            y_space = function_space(y)

            y_cells = point_owners(X_coords, y_space, tolerance=tolerance)
            y_colors = greedy_coloring(y_space)
            P = interpolation_matrix(X_coords, y, y_cells, y_colors)
        else:
            P = P.copy()

        super().__init__(X, list(X) + [y], nl_deps=[], ic=False, adj_ic=False)
        self._P = P
        self._P_T = P.T

    def forward_solve(self, X, deps=None):
        if is_function(X):
            X = (X,)
        y = (self.dependencies() if deps is None else deps)[-1]

        check_space_type(y, "primal")
        y_v = function_get_values(y)
        x_v_local = np.full(len(X), np.NAN, dtype=backend_ScalarType)
        for i in range(len(X)):
            x_v_local[i] = self._P.getrow(i).dot(y_v)

        comm = function_comm(y)
        x_v = np.full(len(X), np.NAN, dtype=backend_ScalarType)
        comm.Allreduce(x_v_local, x_v, op=MPI.MAX)

        for i, x in enumerate(X):
            function_assign(x, x_v[i])

    def adjoint_derivative_action(self, nl_deps, dep_index, adj_X):
        if is_function(adj_X):
            adj_X = (adj_X,)

        if dep_index < len(adj_X):
            return adj_X[dep_index]
        elif dep_index == len(adj_X):
            adj_x_v = np.full(len(adj_X), np.NAN, dtype=backend_ScalarType)
            for i, adj_x in enumerate(adj_X):
                adj_x_v[i] = function_scalar_value(adj_x)
            F = function_new_conjugate_dual(self.dependencies()[-1])
            function_set_values(F, self._P_T.dot(adj_x_v))
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
                                      P=self._P)


class PointInterpolationSolver(PointInterpolation):
    ""

    def __init__(self, y, X, X_coords=None, y_colors=None, y_cells=None,
                 P=None, P_T=None, tolerance=0.0):
        if y_colors is not None:
            warnings.warn("y_colors argument is deprecated and has no effect",
                          DeprecationWarning, stacklevel=2)
        if y_cells is not None:
            warnings.warn("y_cells argument is deprecated and has no effect",
                          DeprecationWarning, stacklevel=2)
        if P_T is not None:
            warnings.warn("P_T argument is deprecated and has no effect",
                          DeprecationWarning, stacklevel=2)
        warnings.warn("PointInterpolationSolver is deprecated -- "
                      "use PointInterpolation instead",
                      DeprecationWarning, stacklevel=2)
        super().__init__(X, y, X_coords,
                         P=P, tolerance=tolerance)
