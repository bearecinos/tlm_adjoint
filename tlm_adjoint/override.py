#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from .manager import (
    annotation_enabled, manager_disabled, paused_manager, tlm_enabled)

import functools

__all__ = []

_OVERRIDE_PROPERTY_NAME_KEY = "_tlm_adjoint__override_property_name_%i"
_OVERRIDE_PROPERTY_COUNTER_KEY = "_tlm_adjoint__override_property_counter"


def override_method(cls, name):
    orig = getattr(cls, name)

    def wrapper(override):
        @functools.wraps(orig)
        def wrapped_override(self, *args, **kwargs):
            return override(self, orig,
                            lambda: orig(self, *args, **kwargs),
                            *args, **kwargs)

        setattr(cls, name, wrapped_override)
        return wrapped_override

    return wrapper


def override_property(cls, name, *,
                      cached=False):
    orig = getattr(cls, name)

    def wrapper(override):
        property_decorator = functools.cached_property if cached else property

        @property_decorator
        @functools.wraps(orig)
        def wrapped_override(self, *args, **kwargs):
            return override(self, lambda: orig.__get__(self, type(self)),
                            *args, **kwargs)

        setattr(cls, name, wrapped_override)
        if cached:
            override_counter = getattr(cls, _OVERRIDE_PROPERTY_COUNTER_KEY, -1) + 1  # noqa: E501
            setattr(cls, _OVERRIDE_PROPERTY_COUNTER_KEY, override_counter)
            wrapped_override.__set_name__(
                wrapped_override,
                _OVERRIDE_PROPERTY_NAME_KEY % override_counter)
        return wrapped_override

    return wrapper


def override_function(orig):
    def wrapper(override):
        @functools.wraps(orig)
        def wrapped_override(*args, **kwargs):
            return override(orig,
                            lambda: orig(*args, **kwargs),
                            *args, **kwargs)

        return wrapped_override

    return wrapper


def add_manager_controls(orig):
    def wrapped_orig(*args, annotate=None, tlm=None, **kwargs):
        if annotate is None or annotate:
            annotate = annotation_enabled()
        if tlm is None or tlm:
            tlm = tlm_enabled()
        with paused_manager(annotate=not annotate, tlm=not tlm):
            return orig(*args, **kwargs)

    return wrapped_orig


def manager_method(cls, name, *,
                   override_without_manager=False,
                   pre_call=None, post_call=None):
    orig = getattr(cls, name)

    def wrapper(override):
        @manager_disabled()
        @functools.wraps(orig)
        def wrapped_orig(self, *args, **kwargs):
            if pre_call is not None:
                args, kwargs = pre_call(self, *args, **kwargs)
            return_value = orig(self, *args, **kwargs)
            if post_call is not None:
                return_value = post_call(self, return_value, *args, **kwargs)
            return return_value

        def wrapped_override(self, *args, annotate=None, tlm=None, **kwargs):
            if annotate is None or annotate:
                annotate = annotation_enabled()
            if tlm is None or tlm:
                tlm = tlm_enabled()
            if annotate or tlm or override_without_manager:
                return override(self, wrapped_orig,
                                lambda: wrapped_orig(self, *args, **kwargs),
                                *args, annotate=annotate, tlm=tlm, **kwargs)
            else:
                return wrapped_orig(self, *args, **kwargs)

        setattr(cls, name, wrapped_override)
        return wrapped_override

    return wrapper
