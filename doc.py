#!/usr/bin/env python
# Copyright 2013 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import print_function

import collections
import inspect
import os
import sys

from . import loader
from . import run as recipe_run
from . import package
from . import recipe_api

def trim_doc(docstring):
  """From PEP 257"""
  if not docstring:
    return ''
  # Convert tabs to spaces (following the normal Python rules)
  # and split into a list of lines:
  lines = docstring.expandtabs().splitlines()
  # Determine minimum indentation (first line doesn't count):
  indent = sys.maxint
  for line in lines[1:]:
    stripped = line.lstrip()
    if stripped:
      indent = min(indent, len(line) - len(stripped))
  # Remove indentation (first line is special):
  trimmed = [lines[0].strip()]
  if indent < sys.maxint:
    for line in lines[1:]:
      trimmed.append(line[indent:].rstrip())
  # Strip off trailing and leading blank lines:
  while trimmed and not trimmed[-1]:
    trimmed.pop()
  while trimmed and not trimmed[0]:
    trimmed.pop(0)
  return trimmed

def member_iter(obj):
  for name in sorted(dir(obj)):
    if name[0] == '_' and name != '__call__':
      continue
    # Check class first to avoid calling property functions.
    if hasattr(obj.__class__, name):
      val = getattr(obj.__class__, name)
      if callable(val) or isinstance(val, property):
        yield name, val
    else:
      val = getattr(obj, name)
      if callable(val) or inspect.ismodule(val):
        yield name, val

def map_to_cool_name(typ):
  if typ is collections.Mapping:
    return 'Mapping'
  return typ

def p(indent_lvl, *args, **kwargs):
  sys.stdout.write('  '*indent_lvl)
  print(*args, **kwargs)

def pmethod(indent_lvl, name, obj):
  if isinstance(obj, property):
    name = '@'+name
    if obj.fset:
      name += '(r/w)'
  p(indent_lvl, name, '', end='')
  if obj.__doc__:
    lines = trim_doc(obj.__doc__)
    p(0, '--', lines[0])
  else:
    p(0)

def main(package_deps):
  common_methods = set(k for k, v in member_iter(recipe_api.RecipeApi))
  p(0, 'Common Methods -- %s' % os.path.splitext(recipe_api.__file__)[0])
  for method in sorted(common_methods):
    pmethod(1, method, getattr(recipe_api.RecipeApi, method))

  universe = loader.RecipeUniverse(package_deps)
  deps = universe.deps_from_spec(
      # TODO(luqui): This doesn't handle name scoping correctly (e.g. same-named
      # modules in different packages).
      { modpath: modpath.split('/')[-1]
        for modpath in universe.loop_over_recipe_modules() })

  inst = loader.create_recipe_api(
      deps, recipe_run.RecipeEngine(None, {}, None))

  for mod_name, mod in deps.iteritems():
    p(0)
    p(0, "(%s) -- %s" % (mod_name, mod.__path__[0]))
    if mod.LOADED_DEPS:
      p(1, 'DEPS:', list(mod.LOADED_DEPS))

    subinst = getattr(inst, mod_name)
    bases = set(subinst.__class__.__bases__)
    base_fns = set()
    for base in bases:
      for name, _ in inspect.getmembers(base):
        base_fns.add(name)
    for cool_base in bases - set((recipe_api.RecipeApi,)):
      p(1, 'behaves like %s' % map_to_cool_name(cool_base))

    if mod.API.__doc__:
      for line in trim_doc(mod.API.__doc__):
        p(2, '"', line)

    for fn_name, obj in member_iter(subinst):
      if fn_name in base_fns:
        continue
      pmethod(1, fn_name, obj)
