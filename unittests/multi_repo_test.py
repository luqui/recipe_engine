#!/usr/bin/env python

import contextlib
import copy
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

RECIPE_ENGINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THIRD_PARTY = os.path.dirname(RECIPE_ENGINE)
sys.path.insert(0, THIRD_PARTY)

from google import protobuf
from recipe_engine import package
from recipe_engine import package_pb2

@contextlib.contextmanager
def _in_directory(target_dir):
  old_dir = os.getcwd()
  os.chdir(target_dir)
  try:
    yield
  finally:
    os.chdir(old_dir)


def _updated_deps(inp, updates):
  if inp is None:
    return updates

  outp = inp.__class__()
  outp.CopyFrom(inp)
  for dep in outp.deps:
    if dep.project_id in updates:
      dep.revision = updates[dep.project_id]
  return outp


def _get_dep(inp, dep_id):
  for dep in inp.deps:
    if dep.project_id == dep_id:
      return dep
  else:
    raise Exception('Dependency %s not found in %s' % (dep, inp))


def _to_text(buf):
  return protobuf.text_format.MessageToString(buf)


def _recstrify(thing):
  if isinstance(thing, basestring):
    return str(thing)
  elif isinstance(thing, dict):
    out = {}
    for k,v in thing.iteritems():
      out[str(k)] = _recstrify(v)
    return out
  elif isinstance(thing, list):
    return map(_recstrify, thing)
  else:
    return thing


class RecipeRollError(Exception):
  def __init__(self, stdout, stderr):
    self.stdout = stdout
    self.stderr = stderr


class MultiRepoTest(unittest.TestCase):
  def _run_cmd(self, cmd, env=None):
    subprocess.call(cmd, env=env)

  def _create_repo(self, name, spec):
    repo_dir = os.path.join(self._root_dir, name)
    os.mkdir(repo_dir)
    with _in_directory(repo_dir):
      self._run_cmd(['git', 'init'])
      config_file = os.path.join('infra', 'config', 'recipes.cfg')
      os.makedirs(os.path.dirname(config_file))
      package.ProtoFile(config_file).write(spec)
      self._run_cmd(['git', 'add', config_file])
      self._run_cmd(['git', 'commit', '-m', 'New recipe package'])
      rev = subprocess.check_output(['git', 'rev-parse', 'HEAD']).strip()
    return {
        'root': repo_dir,
        'revision': rev,
        'spec': spec,
    }

  def _commit_in_repo(self, repo, message='Empty commit'):
    with _in_directory(repo['root']):
      env = dict(os.environ)
      self._run_cmd(['git', 'commit', '-a', '--allow-empty', '-m', message],
                    env=env)
      rev = subprocess.check_output(['git', 'rev-parse', 'HEAD']).strip()
    return {
        'root': repo['root'],
        'revision': rev,
        'spec': repo['spec'],
    }

  def setUp(self):
    self.maxDiff = None

    self._root_dir = tempfile.mkdtemp()
    self._recipe_tool = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'recipes.py')

  def tearDown(self):
    shutil.rmtree(self._root_dir)

  def _repo_setup(self, repo_deps):
    # In order to avoid a topsort, we require that repo names are in
    # alphebetical dependency order -- i.e. later names depend on earlier
    # ones.
    repos = {}
    for k in sorted(repo_deps):
      repos[k] = self._create_repo(k, package_pb2.Package(
          api_version=1,
          project_id=k,
          recipes_path='',
          deps=[
              package_pb2.DepSpec(
                  project_id=d,
                  url=repos[d]['root'],
                  branch='master',
                  revision=repos[d]['revision'],
              )
              for d in repo_deps[k]
          ],
      ))
    return repos

  def _run_roll(self, repo, expect_updates, commit=False):
    with _in_directory(repo['root']):
      popen = subprocess.Popen(['python', self._recipe_tool, 'roll'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
      stdout, stderr = popen.communicate()

      if popen.returncode != 0:
        raise RecipeRollError(stdout, stderr)

      if expect_updates:
        self.assertRegexpMatches(stdout, r'Wrote \S*recipes.cfg')
      else:
        self.assertRegexpMatches(stdout, r'No consistent rolls found')

      if commit:
        assert expect_updates, 'Cannot commit when not expecting updates'
        git_match = re.search(r'^git commit .*', stdout, re.MULTILINE)
        self.assertTrue(git_match)
        git_command = git_match.group(0)
        subprocess.call(git_command, shell=True)
        rev = subprocess.check_output(['git', 'rev-parse', 'HEAD']).strip()
        return {
            'root': repo['root'],
            'revision': rev,
            'spec': repo['spec'],
        }


  def _get_spec(self, repo):
    proto_file = package.ProtoFile(
        os.path.join(repo['root'], 'infra', 'config', 'recipes.cfg'))
    return proto_file.read()

  def test_empty_roll(self):
    repos = self._repo_setup({
      'a': [],
      'b': [ 'a' ],
    })
    self._run_roll(repos['b'], expect_updates=False)

  def test_simple_roll(self):
    repos = self._repo_setup({
      'a': [],
      'b': ['a'],
    })
    new_a = self._commit_in_repo(repos['a'])
    self._run_roll(repos['b'], expect_updates=True)
    self.assertEqual(
        _to_text(self._get_spec(repos['b'])),
        _to_text(_updated_deps(repos['b']['spec'], {
            'a': new_a['revision'],
        })))
    self._run_roll(repos['b'], expect_updates=False)

  def test_indepdendent_roll(self):
    repos = self._repo_setup({
        'b': [],
        'c': [],
        'd': ['b', 'c'],
    })
    new_b = self._commit_in_repo(repos['b'])
    new_c = self._commit_in_repo(repos['c'])
    self._run_roll(repos['d'], expect_updates=True)
    # There is no guarantee on the order the two updates come in.
    # (Usually we sort by date but these commits are within 1 second)
    # However after one roll we expect only one of the two updates to
    # have come in.
    d_spec = self._get_spec(repos['d'])
    self.assertTrue(
         (_get_dep(d_spec, 'b').revision == new_b['revision'])
      != (_get_dep(d_spec, 'c').revision == new_c['revision']))
    self._run_roll(repos['d'], expect_updates=True)
    self.assertEqual(
        _to_text(self._get_spec(repos['d'])),
        _to_text(_updated_deps(repos['d']['spec'], {
            'b': new_b['revision'],
            'c': new_c['revision'],
        })))
    self._run_roll(repos['d'], expect_updates=False)

  def test_dependent_roll(self):
    repos = self._repo_setup({
        'a': [],
        'b': ['a'],
        'c': ['a'],
        'd': ['b', 'c'],
    })
    new_a = self._commit_in_repo(repos['a'])
    new_b = self._run_roll(repos['b'], expect_updates=True, commit=True)
    new_c = self._run_roll(repos['c'], expect_updates=True, commit=True)

    # We only expect one roll here because to roll b without c would
    # result in an inconsistent revision of a, so we should skip it.
    self._run_roll(repos['d'], expect_updates=True)
    d_spec = self._get_spec(repos['d'])
    self.assertEqual(
        _to_text(self._get_spec(repos['d'])),
        _to_text(_updated_deps(repos['d']['spec'], {
            'b': new_b['revision'],
            'c': new_c['revision'],
        })))
    self._run_roll(repos['d'], expect_updates=False)

  def test_cyclic_dependency(self):
    repos = self._repo_setup({
        'a': [],
        'b': ['a'],
    })
    config_file = os.path.join(
        repos['a']['root'], 'infra', 'config', 'recipes.cfg')
    package.ProtoFile(config_file).write(
        package_pb2.Package(
            api_version=1,
            project_id='a',
            recipes_path='',
            deps=[
                package_pb2.DepSpec(
                    project_id='b',
                    url=repos['b']['root'],
                    branch='master',
                    revision=repos['b']['revision'],
                ),
            ],
        )
    )
    self._commit_in_repo(repos['a'])
    with self.assertRaises(RecipeRollError) as raises:
      self._run_roll(repos['b'], expect_updates=True)
    self.assertRegexpMatches(raises.exception.stderr, 'CyclicDependencyError')

if __name__ == '__main__':
  unittest.main()
