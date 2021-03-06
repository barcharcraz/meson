# Copyright 2019 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# This class contains the basic functionality needed to run any interpreter
# or an interpreter-based tool.

from .. import mlog, mesonlib
from ..mesonlib import PerMachine, Popen_safe, version_compare, MachineChoice
from ..environment import Environment

from typing import List, Tuple, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..dependencies.base import ExternalProgram

import re, os, ctypes

class CMakeExecutor:
    # The class's copy of the CMake path. Avoids having to search for it
    # multiple times in the same Meson invocation.
    class_cmakebin = PerMachine(None, None)
    class_cmakevers = PerMachine(None, None)
    class_cmake_cache = {}

    def __init__(self, environment: Environment, version: str, for_machine: MachineChoice, silent: bool = False):
        self.min_version = version
        self.environment = environment
        self.for_machine = for_machine
        self.cmakebin, self.cmakevers = self.find_cmake_binary(self.environment, silent=silent)
        if self.cmakebin is False:
            self.cmakebin = None
            return

        if not version_compare(self.cmakevers, self.min_version):
            self.cmakebin = None
            mlog.warning(
                'The version of CMake', mlog.bold(self.cmakebin.get_path()),
                'is', mlog.bold(self.cmakevers), 'but version', mlog.bold(self.min_version),
                'is required')
            return

    def find_cmake_binary(self, environment: Environment, silent: bool = False) -> Tuple['ExternalProgram', str]:
        from ..dependencies.base import ExternalProgram

        # Create an iterator of options
        def search():
            # Lookup in cross or machine file.
            potential_cmakepath = environment.binaries[self.for_machine].lookup_entry('cmake')
            if potential_cmakepath is not None:
                mlog.debug('CMake binary for %s specified from cross file, native file, or env var as %s.', self.for_machine, potential_cmakepath)
                yield ExternalProgram.from_entry('cmake', potential_cmakepath)
                # We never fallback if the user-specified option is no good, so
                # stop returning options.
                return
            mlog.debug('CMake binary missing from cross or native file, or env var undefined.')
            # Fallback on hard-coded defaults.
            # TODO prefix this for the cross case instead of ignoring thing.
            if environment.machines.matches_build_machine(self.for_machine):
                for potential_cmakepath in environment.default_cmake:
                    mlog.debug('Trying a default CMake fallback at', potential_cmakepath)
                    yield ExternalProgram(potential_cmakepath, silent=True)

        # Only search for CMake the first time and store the result in the class
        # definition
        if CMakeExecutor.class_cmakebin[self.for_machine] is False:
            mlog.debug('CMake binary for %s is cached as not found' % self.for_machine)
        elif CMakeExecutor.class_cmakebin[self.for_machine] is not None:
            mlog.debug('CMake binary for %s is cached.' % self.for_machine)
        else:
            assert CMakeExecutor.class_cmakebin[self.for_machine] is None
            mlog.debug('CMake binary for %s is not cached' % self.for_machine)
            for potential_cmakebin in search():
                mlog.debug('Trying CMake binary {} for machine {} at {}'
                           .format(potential_cmakebin.name, self.for_machine, potential_cmakebin.command))
                version_if_ok = self.check_cmake(potential_cmakebin)
                if not version_if_ok:
                    continue
                if not silent:
                    mlog.log('Found CMake:', mlog.bold(potential_cmakebin.get_path()),
                             '(%s)' % version_if_ok)
                CMakeExecutor.class_cmakebin[self.for_machine] = potential_cmakebin
                CMakeExecutor.class_cmakevers[self.for_machine] = version_if_ok
                break
            else:
                if not silent:
                    mlog.log('Found CMake:', mlog.red('NO'))
                # Set to False instead of None to signify that we've already
                # searched for it and not found it
                CMakeExecutor.class_cmakebin[self.for_machine] = False
                CMakeExecutor.class_cmakevers[self.for_machine] = None

        return CMakeExecutor.class_cmakebin[self.for_machine], CMakeExecutor.class_cmakevers[self.for_machine]

    def check_cmake(self, cmakebin: 'ExternalProgram') -> Optional[str]:
        if not cmakebin.found():
            mlog.log('Did not find CMake {!r}'.format(cmakebin.name))
            return None
        try:
            p, out = Popen_safe(cmakebin.get_command() + ['--version'])[0:2]
            if p.returncode != 0:
                mlog.warning('Found CMake {!r} but couldn\'t run it'
                             ''.format(' '.join(cmakebin.get_command())))
                return None
        except FileNotFoundError:
            mlog.warning('We thought we found CMake {!r} but now it\'s not there. How odd!'
                         ''.format(' '.join(cmakebin.get_command())))
            return None
        except PermissionError:
            msg = 'Found CMake {!r} but didn\'t have permissions to run it.'.format(' '.join(cmakebin.get_command()))
            if not mesonlib.is_windows():
                msg += '\n\nOn Unix-like systems this is often caused by scripts that are not executable.'
            mlog.warning(msg)
            return None
        cmvers = re.sub(r'\s*cmake version\s*', '', out.split('\n')[0]).strip()
        return cmvers

    def _cache_key(self, args: List[str], build_dir: str, env):
        fenv = frozenset(env.items()) if env is not None else None
        targs = tuple(args)
        return (self.cmakebin, targs, build_dir, fenv)

    def _call_real(self, args: List[str], build_dir: str, env) -> Tuple[int, str, str]:
        os.makedirs(build_dir, exist_ok=True)
        cmd = self.cmakebin.get_command() + args
        p, out, err = Popen_safe(cmd, env=env, cwd=build_dir)
        rc = p.returncode
        call = ' '.join(cmd)
        mlog.debug("Called `{}` in {} -> {}".format(call, build_dir, rc))
        return rc, out, err

    def call(self, args: List[str], build_dir: str, env=None, disable_cache: bool = False):
        if env is None:
            env = os.environ

        if disable_cache:
            return self._call_real(args, build_dir, env)

        # First check if cached, if not call the real cmake function
        cache = CMakeExecutor.class_cmake_cache
        key = self._cache_key(args, build_dir, env)
        if key not in cache:
            cache[key] = self._call_real(args, build_dir, env)
        return cache[key]

    def call_with_fake_build(self, args: List[str], build_dir: str, env=None):
        # First check the cache
        cache = CMakeExecutor.class_cmake_cache
        key = self._cache_key(args, build_dir, env)
        if key in cache:
            return cache[key]

        os.makedirs(build_dir, exist_ok=True)

        # Reset the CMake cache
        with open('{}/CMakeCache.txt'.format(build_dir), 'w') as fp:
            fp.write('CMAKE_PLATFORM_INFO_INITIALIZED:INTERNAL=1\n')

        # Fake the compiler files
        comp_dir = '{}/CMakeFiles/{}'.format(build_dir, self.cmakevers)
        os.makedirs(comp_dir, exist_ok=True)

        c_comp = '{}/CMakeCCompiler.cmake'.format(comp_dir)
        cxx_comp = '{}/CMakeCXXCompiler.cmake'.format(comp_dir)

        if not os.path.exists(c_comp):
            with open(c_comp, 'w') as fp:
                fp.write('''# Fake CMake file to skip the boring and slow stuff
set(CMAKE_C_COMPILER "{}") # Just give CMake a valid full path to any file
set(CMAKE_C_COMPILER_ID "GNU") # Pretend we have found GCC
set(CMAKE_COMPILER_IS_GNUCC 1)
set(CMAKE_C_COMPILER_LOADED 1)
set(CMAKE_C_COMPILER_WORKS TRUE)
set(CMAKE_C_ABI_COMPILED TRUE)
set(CMAKE_SIZEOF_VOID_P "{}")
'''.format(os.path.realpath(__file__), ctypes.sizeof(ctypes.c_voidp)))

        if not os.path.exists(cxx_comp):
            with open(cxx_comp, 'w') as fp:
                fp.write('''# Fake CMake file to skip the boring and slow stuff
set(CMAKE_CXX_COMPILER "{}") # Just give CMake a valid full path to any file
set(CMAKE_CXX_COMPILER_ID "GNU") # Pretend we have found GCC
set(CMAKE_COMPILER_IS_GNUCXX 1)
set(CMAKE_CXX_COMPILER_LOADED 1)
set(CMAKE_CXX_COMPILER_WORKS TRUE)
set(CMAKE_CXX_ABI_COMPILED TRUE)
set(CMAKE_SIZEOF_VOID_P "{}")
'''.format(os.path.realpath(__file__), ctypes.sizeof(ctypes.c_voidp)))

        return self.call(args, build_dir, env)

    def found(self) -> bool:
        return self.cmakebin is not None

    def version(self) -> str:
        return self.cmakevers

    def executable_path(self) -> str:
        return self.cmakebin.get_path()

    def get_command(self):
        return self.cmakebin.get_command()

    def machine_choice(self) -> MachineChoice:
        return self.for_machine
