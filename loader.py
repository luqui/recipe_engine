# Copyright 2013-2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import contextlib
import imp
import inspect
import os
import sys

from .config import ConfigContext
from .config_types import Path, ModuleBasePath, RECIPE_MODULE_PREFIX
from .recipe_api import RecipeApi, RecipeApiPlain, Property, UndefinedPropertyException
from .recipe_test_api import RecipeTestApi, DisabledTestData
from .util import scan_directory


class NoSuchRecipe(Exception):
  """Raised by load_recipe is recipe is not found."""


class RecipeScript(object):
  """Holds dict of an evaluated recipe script."""

  def __init__(self, recipe_dict):
    recipe_dict.setdefault('PROPERTIES', {})
    # Let each property object know about the property name.
    for name, value in recipe_dict['PROPERTIES'].items():
      value.name = name

    for k, v in recipe_dict.iteritems():
      setattr(self, k, v)

  @classmethod
  def from_script_path(cls, script_path, universe):
    """Evaluates a script and returns RecipeScript instance."""

    script_vars = {}
    script_vars['__file__'] = script_path

    with _preserve_path():
      execfile(script_path, script_vars)

    script_vars['LOADED_DEPS'] = universe.deps_from_spec(
        script_vars.get('DEPS', []))
    return cls(script_vars)


class Dependency(object):
  def load(self, universe):
    raise NotImplementedError()

  @property
  def local_name(self):
    raise NotImplementedError()

  @property
  def unique_name(self):
    """A unique identifier for the module that this dependency refers to.
    This must be generated without loading the module."""
    raise NotImplementedError()


class PathDependency(Dependency):
  def __init__(self, path, local_name, universe):
    assert os.path.isabs(path), (
        'Path dependencies must be absolute, but %s is not' % path)
    self._path = path
    self._local_name = local_name

    # We forbid modules from living outside our main paths to keep clients
    # from going crazy before we have standardized recipe locations.
    mod_dir = os.path.dirname(path)
    assert mod_dir in universe.module_dirs, (
      'Modules living outside of approved directories are forbidden: '
      '%s is not in %s' % (mod_dir, universe.module_dirs))

  def load(self, universe):
    return _load_recipe_module_module(self._path, universe)

  @property
  def local_name(self):
    return self._local_name

  @property
  def unique_name(self):
    return self._path


class NamedDependency(PathDependency):
  def __init__(self, name, universe):
    for path in universe.module_dirs:
      mod_path = os.path.join(path, name)
      if _is_recipe_module_dir(mod_path):
        super(NamedDependency, self).__init__(mod_path, name, universe=universe)
        return
    raise NoSuchRecipe('Recipe module named %s does not exist' % name)


class PackageDependency(PathDependency):
  # TODO(luqui): Forbid depending on a module from a (locally) undeclared
  # dependency.
  def __init__(self, package, module, local_name, universe):
    mod_path = (
        universe.package_deps.get_package(package).module_path(module))
    super(PackageDependency, self).__init__(
        mod_path, local_name, universe=universe)


class RecipeUniverse(object):
  def __init__(self, package_deps):
    self._loaded = {}
    self._package_deps = package_deps

  @property
  def module_dirs(self):
    return self._package_deps.all_module_dirs

  @property
  def recipe_dirs(self):
    return self._package_deps.all_recipe_dirs

  @property
  def package_deps(self):
    return self._package_deps

  def load(self, dep):
    """Load a Dependency."""
    name = dep.unique_name
    if name in self._loaded:
      mod = self._loaded[name]
      assert mod is not None, (
          'Cyclic dependency when trying to load %s' % name)
      return mod
    else:
      self._loaded[name] = None
      mod = dep.load(self)
      self._loaded[name] = mod
      return mod

  def _dep_from_name(self, name):
    if '/' in name:
      [package,module] = name.split('/')
      dep = PackageDependency(package, module, module, universe=self)
    else:
      # Old style: bare module name, search paths to find it.
      module = name
      dep = NamedDependency(name, universe=self)

    return module, dep

  def deps_from_spec(self, spec):
    # Automatic local names.
    if isinstance(spec, (list, tuple)):
      deps = {}
      for item in spec:
        name, dep = self._dep_from_name(item)
        deps[name] = self.load(dep)
    # Explicit local names.
    elif isinstance(spec, dict):
      deps = {}
      for name, item in spec.iteritems():
        _, dep = self._dep_from_name(item)
        deps[name] = self.load(dep)
    return deps

  def load_recipe(self, recipe):
    """Given name of a recipe, loads and returns it as RecipeScript instance.

    Args:
      recipe (str): name of a recipe, can be in form '<module>:<recipe>'.

    Returns:
      RecipeScript instance.

    Raises:
      NoSuchRecipe: recipe is not found.
    """
    # If the recipe is specified as "module:recipe", then it is an recipe
    # contained in a recipe_module as an example. Look for it in the modules
    # imported by load_recipe_modules instead of the normal search paths.
    if ':' in recipe:
      module_name, example = recipe.split(':')
      assert example.endswith('example')
      for module_dir in self.module_dirs:
        if os.path.isdir(module_dir):
          for subitem in os.listdir(module_dir):
            if module_name == subitem:
              return RecipeScript.from_script_path(
                  os.path.join(module_dir, subitem, 'example.py'), self)
      raise NoSuchRecipe(recipe,
                         'Recipe example %s:%s does not exist' %
                         (module_name, example))
    else:
      for recipe_path in (os.path.join(p, recipe) for p in self.recipe_dirs):
        if os.path.exists(recipe_path + '.py'):
          return RecipeScript.from_script_path(recipe_path + '.py', self)
    raise NoSuchRecipe(recipe)

  def loop_over_recipe_modules(self):
    for path in self.module_dirs:
      if os.path.isdir(path):
        for item in os.listdir(path):
          subpath = os.path.join(path, item)
          if _is_recipe_module_dir(subpath):
            yield subpath

  def loop_over_recipes(self):
    """Yields pairs (path to recipe, recipe name).

    Enumerates real recipes in recipes/* as well as examples in recipe_modules/*.
    """
    for path in self.recipe_dirs:
      for recipe in scan_directory(
          path, lambda f: f.endswith('.py') and f[0] != '_'):
        yield recipe, recipe[len(path)+1:-len('.py')]
    for path in self.module_dirs:
      for recipe in scan_directory(
          path, lambda f: f.endswith('example.py')):
        module_name = os.path.dirname(recipe)[len(path)+1:]
        yield recipe, '%s:example' % module_name


def _is_recipe_module_dir(path):
  return (os.path.isdir(path) and
          os.path.isfile(os.path.join(path, '__init__.py')))


@contextlib.contextmanager
def _preserve_path():
  old_path = sys.path[:]
  try:
    yield
  finally:
    sys.path = old_path


def _find_and_load_module(fullname, modname, path):
  imp.acquire_lock()
  try:
    if fullname not in sys.modules:
      fil = None
      try:
        fil, pathname, descr = imp.find_module(modname,
                                               [os.path.dirname(path)])
        imp.load_module(fullname, fil, pathname, descr)
      finally:
        if fil:
          fil.close()
    return sys.modules[fullname]
  finally:
    imp.release_lock()


def _load_recipe_module_module(path, universe):
  modname = os.path.splitext(os.path.basename(path))[0]
  fullname = '%s.%s' % (RECIPE_MODULE_PREFIX, modname)
  mod = _find_and_load_module(fullname, modname, path)

  # This actually loads the dependencies.
  mod.LOADED_DEPS = universe.deps_from_spec(getattr(mod, 'DEPS', []))

  # Prevent any modules that mess with sys.path from leaking.
  with _preserve_path():
    # TODO(luqui): Remove this hack once configs are cleaned.
    sys.modules['%s.DEPS' % fullname] = mod.LOADED_DEPS
    _recursive_import(path, RECIPE_MODULE_PREFIX)
    _patchup_module(modname, mod)

  return mod


def _recursive_import(path, prefix):
  modname = os.path.splitext(os.path.basename(path))[0]
  fullname = '%s.%s' % (prefix, modname)
  mod = _find_and_load_module(fullname, modname, path)
  if not os.path.isdir(path):
    return mod

  for subitem in os.listdir(path):
    subpath = os.path.join(path, subitem)
    subname = os.path.splitext(subitem)[0]
    if os.path.isdir(subpath):
      if not os.path.exists(os.path.join(subpath, '__init__.py')):
        continue
    elif not subpath.endswith('.py') or subitem.startswith('__init__.py'):
      continue

    submod = _recursive_import(subpath, fullname)

    if not hasattr(mod, subname):
      setattr(mod, subname, submod)
    else:
      prev = getattr(mod, subname)
      assert submod is prev, (
        'Conflicting modules: %s and %s' % (prev, mod))

  return mod


def _patchup_module(name, submod):
  """Finds framework related classes and functions in a |submod| and adds
  them to |submod| as top level constants with well known names such as
  API, CONFIG_CTX, TEST_API, and PROPERTIES.

  |submod| is a recipe module (akin to python package) with submodules such as
  'api', 'config', 'test_api'. This function scans through dicts of that
  submodules to find subclasses of RecipeApi, RecipeTestApi, etc.
  """
  submod.NAME = name
  submod.UNIQUE_NAME = name  # TODO(luqui): use a luci-config unique name
  submod.MODULE_DIRECTORY = Path(ModuleBasePath(submod))
  submod.CONFIG_CTX = getattr(submod, 'CONFIG_CTX', None)

  if hasattr(submod, 'config'):
    for v in submod.config.__dict__.itervalues():
      if isinstance(v, ConfigContext):
        assert not submod.CONFIG_CTX, (
          'More than one configuration context: %s, %s' %
          (submod.config, submod.CONFIG_CTX))
        submod.CONFIG_CTX = v
    assert submod.CONFIG_CTX, 'Config file, but no config context?'

  submod.API = getattr(submod, 'API', None)
  for v in submod.api.__dict__.itervalues():
    if inspect.isclass(v) and issubclass(v, RecipeApiPlain):
      assert not submod.API, (
        '%s has more than one Api subclass: %s, %s' % (name, v, submod.api))
      submod.API = v
  assert submod.API, 'Submodule has no api? %s' % (submod)

  submod.TEST_API = getattr(submod, 'TEST_API', None)
  if hasattr(submod, 'test_api'):
    for v in submod.test_api.__dict__.itervalues():
      if inspect.isclass(v) and issubclass(v, RecipeTestApi):
        assert not submod.TEST_API, (
          'More than one TestApi subclass: %s' % submod.api)
        submod.TEST_API = v
    assert submod.API, (
      'Submodule has test_api.py but no TestApi subclass? %s'
      % (submod)
    )

  submod.PROPERTIES = getattr(submod, 'PROPERTIES', {})
  # Let each property object know about the property name.
  for name, value in submod.PROPERTIES.items():
    value.name = name


class DependencyMapper(object):
  """DependencyMapper topologically traverses the dependency DAG beginning at
  a module, executing a callback ("instantiator") for each module.

  For example, if the dependency DAG looked like this:

          A
         / \
        B   C
         \ /
          D

  (with D depending on B and C, etc.), DependencyMapper(f).instantiate(D) would
  construct

  f_A = f(A, {})
  f_B = f(B, { 'A': f_A })
  f_C = f(C, { 'A': f_A })
  f_D = f(D, { 'B': f_B, 'C': f_C })

  finally returning f_D.  instantiate can be called multiple times, which reuses
  already-computed results.
  """

  def __init__(self, instantiator):
    self._instantiator = instantiator
    self._instances = {}

  def instantiate(self, mod):
    if mod in self._instances:
      return self._instances[mod]
    deps_dict = { name: self.instantiate(dep)
                  for name, dep in mod.LOADED_DEPS.iteritems() }
    self._instances[mod] = self._instantiator(mod, deps_dict)
    return self._instances[mod]

def invoke_with_properties(callable_obj, all_props, prop_defs,
                           **additional_args):
  """
  Invokes callable with filtered, type-checked properties.

  Args:
    callable_obj: The function to call, or class to instantiate.
                  This supports passing in either RunSteps, or a recipe module,
                  which is a class.
    all_props: A dictionary containing all the properties
               currently defined in the system.
    prop_defs: A dictionary of name to property definitions for this callable.
    additional_args: kwargs to pass through to the callable.
                     Note that the names of the arguments can correspond to
                     positional arguments as well.

  Returns:
    The result of calling callable with the filtered properties
    and additional arguments.
  """
  # To detect when they didn't specify a property that they have as a
  # function argument, list the arguments, through inspection,
  # and then comparing this list to the provided properties. We use a list
  # instead of a dict because getargspec returns a list which we would have to
  # convert to a dictionary, and the benefit of the dictionary is pretty small.
  props = []
  if inspect.isclass(callable_obj):
    arg_names = inspect.getargspec(callable_obj.__init__).args

    arg_names.pop(0)
  else:
    arg_names = inspect.getargspec(callable_obj).args

  for arg in arg_names:
    if arg in additional_args:
      props.append(additional_args.pop(arg))
      continue

    if arg not in prop_defs:
      raise UndefinedPropertyException(
        "Missing property definition for '{}'.".format(arg))

    props.append(prop_defs[arg].interpret(all_props.get(
      arg, Property.sentinel)))

  return callable_obj(*props, **additional_args)

def create_recipe_api(toplevel_deps, engine, test_data=DisabledTestData()):
  def instantiator(mod, deps):
    kwargs = {
      'module': mod,
      'engine': engine,
      # TODO(luqui): test_data will need to use canonical unique names.
      'test_data': test_data.get_module_test_data(mod.NAME)
    }
    prop_defs = mod.PROPERTIES
    mod_api = invoke_with_properties(
      mod.API, engine.properties, prop_defs, **kwargs)
    mod_api.test_api = (getattr(mod, 'TEST_API', None)
                        or RecipeTestApi)(module=mod)
    for k, v in deps.iteritems():
      setattr(mod_api.m, k, v)
      setattr(mod_api.test_api.m, k, v.test_api)
    return mod_api

  mapper = DependencyMapper(instantiator)
  api = RecipeApi(module=None, engine=engine,
                  test_data=test_data.get_module_test_data(None))
  for k, v in toplevel_deps.iteritems():
    setattr(api, k, mapper.instantiate(v))
  return api


def create_test_api(toplevel_deps, universe):
  def instantiator(mod, deps):
    modapi = (getattr(mod, 'TEST_API', None) or RecipeTestApi)(module=mod)
    for k,v in deps.iteritems():
      setattr(modapi.m, k, v)
    return modapi

  mapper = DependencyMapper(instantiator)
  api = RecipeTestApi(module=None)
  for k,v in toplevel_deps.iteritems():
    setattr(api, k, mapper.instantiate(v))
  return api



