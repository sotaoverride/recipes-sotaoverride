#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# -----------------------------------------------------------------------------
#  Copyright (C) PyZMQ Developers
#  Distributed under the terms of the Modified BSD License.
#
#  The `configure` subcommand is copied and adaped from h5py
#  h5py source used under the New BSD license
#
#  h5py: <http://code.google.com/p/h5py/>
#
#  The code to bundle libzmq as an Extension is from pyzmq-static
#  pyzmq-static source used under the New BSD license
#
#  pyzmq-static: <https://github.com/brandon-rhodes/pyzmq-static>
# -----------------------------------------------------------------------------

from __future__ import with_statement, print_function

from contextlib import contextmanager
import copy
import io
import os
import shutil
import subprocess
import sys
import time
import errno
import platform
from pprint import pprint
from traceback import print_exc

try:
    import cffi
except ImportError:
    cffi = None

from setuptools import setup, Command
from setuptools.command.bdist_egg import bdist_egg
from setuptools.command.build_ext import build_ext
from setuptools.command.sdist import sdist
from setuptools.extension import Extension

import distutils.util
from distutils.ccompiler import get_default_compiler
from distutils.ccompiler import new_compiler
from distutils.sysconfig import customize_compiler, get_config_var
from distutils.version import LooseVersion as V

from glob import glob
from os.path import splitext, basename, join as pjoin

from subprocess import Popen, PIPE, check_call, CalledProcessError

# local script imports:
from buildutils import (
    discover_settings,
    v_str,
    save_config,
    detect_zmq,
    merge,
    config_from_prefix,
    info,
    warn,
    fatal,
    debug,
    line,
    localpath,
    locate_vcredist_dir,
    fetch_libzmq,
    fetch_libzmq_dll,
    stage_platform_hpp,
    bundled_version,
    customize_mingw,
    compile_and_forget,
    patch_lib_paths,
)

# -----------------------------------------------------------------------------
# Flags
# -----------------------------------------------------------------------------


# name of the libzmq library - can be changed by --libzmq <name>
libzmq_name = "libzmq"

doing_bdist = any(arg.startswith("bdist") for arg in sys.argv[1:])
pypy = platform.python_implementation() == 'PyPy'

# reference points for zmq compatibility

min_legacy_zmq = (2, 1, 4)
min_good_zmq = (3, 2)
target_zmq = bundled_version
dev_zmq = (target_zmq[0], target_zmq[1] + 1, 0)

# set dylib ext:
if sys.platform.startswith('win'):
    lib_ext = '.dll'
elif sys.platform == 'darwin':
    lib_ext = '.dylib'
else:
    lib_ext = '.so'

# allow `--zmq=foo` to be passed at any point,
# but always assign it to configure

configure_idx = -1
fetch_idx = -1
for idx, arg in enumerate(list(sys.argv)):
    # track index of configure and fetch_libzmq
    if arg == 'configure':
        configure_idx = idx
    elif arg == 'fetch_libzmq':
        fetch_idx = idx

    if arg.startswith('--zmq='):
        sys.argv.pop(idx)
        if configure_idx < 0:
            if fetch_idx < 0:
                configure_idx = 1
            else:
                configure_idx = fetch_idx + 1
            sys.argv.insert(configure_idx, 'configure')
        sys.argv.insert(configure_idx + 1, arg)
        break

for idx, arg in enumerate(list(sys.argv)):
    if arg.startswith('--libzmq='):
        sys.argv.remove(arg)
        libzmq_name = arg.split("=", 1)[1]
    if arg == '--enable-drafts':
        sys.argv.remove(arg)
        os.environ['ZMQ_DRAFT_API'] = '1'


if sys.platform.startswith('win'):
    # ensure vcredist is on PATH
    locate_vcredist_dir()
else:
    cxx_flags = os.getenv("CXXFLAGS", "")
    if "-std" not in cxx_flags:
        cxx_flags = "-std=c++11 " + cxx_flags
        os.environ["CXXFLAGS"] = cxx_flags
    if cxx_flags:
        # distutils doesn't support $CXXFLAGS
        cxx = os.getenv("CXX", get_config_var("CXX"))
        # get_config_var is broken on some old versions of pypy, add a fallback
        if cxx is None:
            cxx = "c++ -pthread"
        os.environ["CXX"] = cxx + " " + cxx_flags

# -----------------------------------------------------------------------------
# Configuration (adapted from h5py: https://www.h5py.org/)
# -----------------------------------------------------------------------------

# --- compiler settings -------------------------------------------------


def bundled_settings(debug):
    """settings for linking extensions against bundled libzmq"""
    settings = {}
    settings['libraries'] = []
    settings['library_dirs'] = []
    settings['include_dirs'] = [pjoin("bundled", "zeromq", "include")]
    settings['runtime_library_dirs'] = []
    # add pthread on freebsd
    # is this necessary?
    if sys.platform.startswith('freebsd'):
        settings['libraries'].append('pthread')
    elif sys.platform.startswith('win'):
        # link against libzmq in build dir:
        plat = distutils.util.get_platform()
        temp = 'temp.%s-%i.%i' % (plat, sys.version_info[0], sys.version_info[1])
        if hasattr(sys, 'gettotalrefcount'):
            temp += '-pydebug'

        # Python 3.5 adds EXT_SUFFIX to libs
        ext_suffix = distutils.sysconfig.get_config_var("EXT_SUFFIX")
        suffix = os.path.splitext(ext_suffix)[0]

        if debug:
            release = 'Debug'
        else:
            release = 'Release'

        settings['libraries'].append(libzmq_name + suffix)
        settings['library_dirs'].append(pjoin('build', temp, release, 'buildutils'))

    return settings


def check_pkgconfig():
    """ pull compile / link flags from pkg-config if present. """
    pcfg = None
    zmq_config = None
    try:
        pkg_config = os.environ.get('PKG_CONFIG', 'pkg-config')
        check_call([pkg_config, '--exists', 'libzmq'])
        # this would arguably be better with --variable=libdir /
        # --variable=includedir, but would require multiple calls
        pcfg = Popen(
            [pkg_config, '--libs', '--cflags', 'libzmq'], stdout=subprocess.PIPE
        )
    except OSError as osexception:
        if osexception.errno == errno.ENOENT:
            info('pkg-config not found')
        else:
            warn("Running pkg-config failed - %s." % osexception)
    except CalledProcessError:
        info("Did not find libzmq via pkg-config.")

    if pcfg is not None:
        output, _ = pcfg.communicate()
        output = output.decode('utf8', 'replace')
        bits = output.strip().split()
        zmq_config = {'library_dirs': [], 'include_dirs': [], 'libraries': []}
        for tok in bits:
            if tok.startswith("-L"):
                zmq_config['library_dirs'].append(tok[2:])
            if tok.startswith("-I"):
                zmq_config['include_dirs'].append(tok[2:])
            if tok.startswith("-l"):
                zmq_config['libraries'].append(tok[2:])
        info("Settings obtained from pkg-config: %r" % zmq_config)

    return zmq_config


def _add_rpath(settings, path):
    """Add rpath to settings

    Implemented here because distutils runtime_library_dirs doesn't do anything on darwin
    """
    if sys.platform == 'darwin':
        settings['extra_link_args'].extend(['-Wl,-rpath', '-Wl,%s' % path])
    else:
        settings['runtime_library_dirs'].append(path)


def settings_from_prefix(prefix=None):
    """load appropriate library/include settings from ZMQ prefix"""
    settings = {}
    settings['libraries'] = []
    settings['include_dirs'] = []
    settings['library_dirs'] = []
    settings['runtime_library_dirs'] = []
    settings['extra_link_args'] = []

    if sys.platform.startswith('win'):
        global libzmq_name

        if prefix:
            # add prefix itself as well, for e.g. libzmq Windows releases
            for include_dir in [pjoin(prefix, 'include'), prefix]:
                if os.path.exists(pjoin(include_dir, "zmq.h")):
                    settings['include_dirs'].append(include_dir)
                    info(f"Found zmq.h in {include_dir}")
                    break
            else:
                warn(f"zmq.h not found in {prefix} or {prefix}/include")
            for library_dir in [pjoin(prefix, 'lib'), prefix]:
                matches = glob(pjoin(library_dir, f"{libzmq_name}*.dll"))
                if matches:
                    libzmq_path = matches[0]
                    libzmq_lib, libzmq_dll_name = os.path.split(libzmq_path)
                    libzmq_name, _ = os.path.splitext(libzmq_dll_name)
                    info(f"Found {libzmq_path} in {libzmq_lib}")
                    if libzmq_lib not in os.environ["PATH"].split(os.pathsep):
                        info(f"Adding {libzmq_lib} to $PATH")
                        os.environ["PATH"] += os.pathsep + libzmq_lib
                    settings['library_dirs'].append(library_dir)
                    break
            else:
                warn(f"{libzmq_name}.dll not found in {prefix} or {prefix}/lib")
        settings['libraries'].append(libzmq_name)

    else:
        # add pthread on freebsd
        if sys.platform.startswith('freebsd'):
            settings['libraries'].append('pthread')

        if sys.platform.startswith('sunos'):
            if platform.architecture()[0] == '32bit':
                settings['extra_link_args'] += ['-m32']
            else:
                settings['extra_link_args'] += ['-m64']

        if prefix:
            settings['libraries'].append('zmq')

            settings['include_dirs'] += [pjoin(prefix, 'include')]
            if (
                sys.platform.startswith('sunos')
                and platform.architecture()[0] == '64bit'
            ):
                settings['library_dirs'] += [pjoin(prefix, 'lib/amd64')]
            settings['library_dirs'] += [pjoin(prefix, 'lib')]
        else:
            # If prefix is not explicitly set, pull it from pkg-config by default.
            # this is probably applicable across platforms, but i don't have
            # sufficient test environments to confirm
            pkgcfginfo = check_pkgconfig()
            if pkgcfginfo is not None:
                # we can get all the zmq-specific values from pkgconfg
                for key, value in pkgcfginfo.items():
                    settings[key].extend(value)
            else:
                settings['libraries'].append('zmq')

                if sys.platform == 'darwin' and os.path.isdir('/opt/local/lib'):
                    # allow macports default
                    settings['include_dirs'] += ['/opt/local/include']
                    settings['library_dirs'] += ['/opt/local/lib']
                if os.environ.get('VIRTUAL_ENV', None):
                    # find libzmq installed in virtualenv
                    env = os.environ['VIRTUAL_ENV']
                    settings['include_dirs'] += [pjoin(env, 'include')]
                    settings['library_dirs'] += [pjoin(env, 'lib')]

        for path in settings['library_dirs']:
            _add_rpath(settings, os.path.abspath(path))
    info(settings)

    return settings


class LibZMQVersionError(Exception):
    pass


# -----------------------------------------------------------------------------
# Extra commands
# -----------------------------------------------------------------------------


class bdist_egg_disabled(bdist_egg):
    """Disabled version of bdist_egg

    Prevents setup.py install from performing setuptools' default easy_install,
    which it should never ever do.
    """

    def run(self):
        sys.exit(
            "Aborting implicit building of eggs. Use `pip install .` to install from source."
        )


class Configure(build_ext):
    """Configure command adapted from h5py"""

    description = "Discover ZMQ version and features"

    user_options = build_ext.user_options + [
        ('zmq=', None, "libzmq install prefix"),
        (
            'build-base=',
            'b',
            "base directory for build library",
        ),  # build_base from build
    ]

    def initialize_options(self):
        super().initialize_options()
        self.zmq = os.environ.get("ZMQ_PREFIX") or None
        self.build_base = 'build'

    def finalize_options(self):
        super().finalize_options()
        self.tempdir = pjoin(self.build_temp, 'scratch')
        self.has_run = False
        self.config = discover_settings(self.build_base)
        if self.zmq is not None:
            merge(self.config, config_from_prefix(self.zmq))
        self.init_settings_from_config()

    def save_config(self, name, cfg):
        """write config to JSON"""
        save_config(name, cfg, self.build_base)
        # write to zmq.utils.[name].json
        save_config(name, cfg, os.path.join('zmq', 'utils'))
        # also write to build_lib, because we might be run after copying to
        # build_lib has already happened.
        build_lib_utils = os.path.join(self.build_lib, 'zmq', 'utils')
        if os.path.exists(build_lib_utils):
            save_config(name, cfg, build_lib_utils)

    def init_settings_from_config(self):
        """set up compiler settings, based on config"""
        cfg = self.config

        if cfg['libzmq_extension']:
            settings = bundled_settings(self.debug)
        else:
            settings = settings_from_prefix(cfg['zmq_prefix'])

        if 'have_sys_un_h' not in cfg:
            # don't link against anything when checking for sys/un.h
            minus_zmq = copy.deepcopy(settings)
            try:
                minus_zmq['libraries'] = []
            except Exception:
                pass
            try:
                compile_and_forget(
                    self.tempdir, pjoin('buildutils', 'check_sys_un.c'), **minus_zmq
                )
            except Exception as e:
                warn("No sys/un.h, IPC_PATH_MAX_LEN will be undefined: %s" % e)
                cfg['have_sys_un_h'] = False
            else:
                cfg['have_sys_un_h'] = True

            self.save_config('config', cfg)

        settings.setdefault('define_macros', [])
        if cfg['have_sys_un_h']:
            settings['define_macros'].append(('HAVE_SYS_UN_H', 1))

        if cfg['win_ver']:
            # set target minimum Windows version
            settings['define_macros'].extend(
                [
                    ('WINVER', cfg['win_ver']),
                    ('_WIN32_WINNT', cfg['win_ver']),
                ]
            )

        if cfg.get('zmq_draft_api'):
            settings['define_macros'].append(('ZMQ_BUILD_DRAFT_API', 1))

        use_static_zmq = cfg.get('use_static_zmq', 'False').upper()
        if use_static_zmq in ('TRUE', '1'):
            settings['define_macros'].append(('ZMQ_STATIC', '1'))

        if os.environ.get("PYZMQ_CYTHON_COVERAGE"):
            settings['define_macros'].append(('CYTHON_TRACE', '1'))

        # include internal directories
        settings.setdefault('include_dirs', [])
        settings['include_dirs'] += [pjoin('zmq', sub) for sub in ('utils',)]

        settings.setdefault('libraries', [])
        # Explicitly link dependencies, not necessary if zmq is dynamic
        if sys.platform.startswith('win'):
            settings['libraries'].extend(('ws2_32', 'iphlpapi', 'advapi32'))

        for ext in self.distribution.ext_modules:
            if ext.name.startswith('zmq.lib'):
                continue
            for attr, value in settings.items():
                setattr(ext, attr, value)

        self.compiler_settings = settings
        self.save_config('compiler', settings)

    def create_tempdir(self):
        self.erase_tempdir()
        os.makedirs(self.tempdir)

    def erase_tempdir(self):
        try:
            shutil.rmtree(self.tempdir)
        except Exception:
            pass

    @property
    def compiler_type(self):
        compiler = self.compiler
        if compiler is None:
            return get_default_compiler()
        elif isinstance(compiler, str):
            return compiler
        else:
            return compiler.compiler_type

    @property
    def cross_compiling(self):
        return self.config['bdist_egg'].get('plat-name', sys.platform) != sys.platform

    def check_zmq_version(self):
        """check the zmq version"""
        cfg = self.config
        # build test program
        zmq_prefix = cfg['zmq_prefix']
        detected = self.test_build(zmq_prefix, self.compiler_settings)
        # now check the libzmq version

        vers = tuple(detected['vers'])
        vs = v_str(vers)
        if cfg['allow_legacy_libzmq']:
            min_zmq = min_legacy_zmq
        else:
            min_zmq = min_good_zmq
        if vers < min_zmq:
            msg = [
                "Detected ZMQ version: %s, but require ZMQ >= %s"
                % (vs, v_str(min_zmq)),
            ]
            if zmq_prefix:
                msg.append("    ZMQ_PREFIX=%s" % zmq_prefix)
            if vers >= min_legacy_zmq:

                msg.append(
                    "    Explicitly allow legacy zmq by specifying `--zmq=/zmq/prefix`"
                )

            raise LibZMQVersionError('\n'.join(msg))
        if vers < min_good_zmq:
            msg = [
                "Detected legacy ZMQ version: %s. It is STRONGLY recommended to use ZMQ >= %s"
                % (vs, v_str(min_good_zmq)),
            ]
            if zmq_prefix:
                msg.append("    ZMQ_PREFIX=%s" % zmq_prefix)
            warn('\n'.join(msg))
        elif vers < target_zmq:
            warn(
                "Detected ZMQ version: %s, but pyzmq targets ZMQ %s."
                % (vs, v_str(target_zmq))
            )
            warn(
                "libzmq features and fixes introduced after %s will be unavailable."
                % vs
            )
            line()
        elif vers >= dev_zmq:
            warn(
                "Detected ZMQ version: %s. Some new features in libzmq may not be exposed by pyzmq."
                % vs
            )
            line()

        if sys.platform.startswith('win'):
            # fetch libzmq.dll into local dir
            local_dll = localpath('zmq', libzmq_name + '.dll')
            if not zmq_prefix and not os.path.exists(local_dll):
                fatal(
                    "ZMQ directory must be specified on Windows via setup.cfg or 'python setup.py configure --zmq=/path/to/libzmq'"
                )

    def bundle_libzmq_extension(self):
        bundledir = "bundled"
        ext_modules = self.distribution.ext_modules
        if ext_modules and any(m.name == 'zmq.libzmq' for m in ext_modules):
            # I've already been run
            return

        line()
        info("Using bundled libzmq")

        # fetch sources for libzmq extension:
        if not os.path.exists(bundledir):
            os.makedirs(bundledir)

        fetch_libzmq(bundledir)

        stage_platform_hpp(pjoin(bundledir, 'zeromq'))

        sources = [pjoin('buildutils', 'initlibzmq.cpp')]
        sources.extend(
            [
                src
                for src in glob(pjoin(bundledir, 'zeromq', 'src', '*.cpp'))
                # exclude draft ws transport files
                if not os.path.basename(src).startswith(("ws_", "wss_"))
            ]
        )

        includes = [pjoin(bundledir, 'zeromq', 'include')]

        if bundled_version < (4, 2, 0):
            tweetnacl = pjoin(bundledir, 'zeromq', 'tweetnacl')
            tweetnacl_sources = glob(pjoin(tweetnacl, 'src', '*.c'))

            randombytes = pjoin(tweetnacl, 'contrib', 'randombytes')
            if sys.platform.startswith('win'):
                tweetnacl_sources.append(pjoin(randombytes, 'winrandom.c'))
            else:
                tweetnacl_sources.append(pjoin(randombytes, 'devurandom.c'))

            sources += tweetnacl_sources
            includes.append(pjoin(tweetnacl, 'src'))
            includes.append(randombytes)
        else:
            # >= 4.2
            sources += glob(pjoin(bundledir, 'zeromq', 'src', 'tweetnacl.c'))

        # construct the Extensions:
        libzmq = Extension(
            'zmq.libzmq',
            sources=sources,
            include_dirs=includes,
        )

        # register the extension:
        # doing this here means we must be run
        # before finalize_options in build_ext
        self.distribution.ext_modules.insert(0, libzmq)

        # use tweetnacl to provide CURVE support
        libzmq.define_macros.append(('ZMQ_HAVE_CURVE', 1))
        libzmq.define_macros.append(('ZMQ_USE_TWEETNACL', 1))

        # select polling subsystem based on platform
        if sys.platform == "darwin" or "bsd" in sys.platform:
            libzmq.define_macros.append(('ZMQ_USE_KQUEUE', 1))
            libzmq.define_macros.append(('ZMQ_IOTHREADS_USE_KQUEUE', 1))
            libzmq.define_macros.append(('ZMQ_POLL_BASED_ON_POLL', 1))
        elif 'linux' in sys.platform:
            libzmq.define_macros.append(('ZMQ_USE_EPOLL', 1))
            libzmq.define_macros.append(('ZMQ_IOTHREADS_USE_EPOLL', 1))
            libzmq.define_macros.append(('ZMQ_POLL_BASED_ON_POLL', 1))
        elif sys.platform.startswith('win'):
            libzmq.define_macros.append(('ZMQ_USE_SELECT', 1))
            libzmq.define_macros.append(('ZMQ_IOTHREADS_USE_SELECT', 1))
            libzmq.define_macros.append(('ZMQ_POLL_BASED_ON_SELECT', 1))
        else:
            # this may not be sufficiently precise
            libzmq.define_macros.append(('ZMQ_USE_POLL', 1))
            libzmq.define_macros.append(('ZMQ_IOTHREADS_USE_POLL', 1))
            libzmq.define_macros.append(('ZMQ_POLL_BASED_ON_POLL', 1))

        if sys.platform.startswith('win'):
            # include defines from zeromq msvc project:
            libzmq.define_macros.append(('FD_SETSIZE', 16384))
            libzmq.define_macros.append(('DLL_EXPORT', 1))
            libzmq.define_macros.append(('_CRT_SECURE_NO_WARNINGS', 1))

            # When compiling the C++ code inside of libzmq itself, we want to
            # avoid "warning C4530: C++ exception handler used, but unwind
            # semantics are not enabled. Specify /EHsc".
            if self.compiler_type == 'msvc':
                libzmq.extra_compile_args.append('/EHsc')
            elif self.compiler_type == 'mingw32':
                libzmq.define_macros.append(('ZMQ_HAVE_MINGW32', 1))

            # And things like sockets come from libraries that must be named.
            libzmq.libraries.extend(['rpcrt4', 'ws2_32', 'advapi32', 'iphlpapi'])

        else:
            libzmq.include_dirs.append(bundledir)

            # check if we need to link against Realtime Extensions library
            cc = new_compiler(compiler=self.compiler_type)
            customize_compiler(cc)
            cc.output_dir = self.build_temp
            if not sys.platform.startswith(('darwin', 'freebsd')):
                line()
                info("checking for timer_create")
                if not cc.has_function('timer_create'):
                    info("no timer_create, linking librt")
                    libzmq.libraries.append('rt')
                else:
                    info("ok")

        # copy the header files to the source tree.
        bundledincludedir = pjoin('zmq', 'include')
        if not os.path.exists(bundledincludedir):
            os.makedirs(bundledincludedir)
        if not os.path.exists(pjoin(self.build_lib, bundledincludedir)):
            os.makedirs(pjoin(self.build_lib, bundledincludedir))

        for header in glob(pjoin(bundledir, 'zeromq', 'include', '*.h')):
            shutil.copyfile(header, pjoin(bundledincludedir, basename(header)))
            shutil.copyfile(
                header, pjoin(self.build_lib, bundledincludedir, basename(header))
            )

        # update other extensions, with bundled settings
        self.config['libzmq_extension'] = True
        self.init_settings_from_config()
        self.save_config('config', self.config)

    def fallback_on_bundled(self):
        """Couldn't build, fallback after waiting a while"""

        line()

        warn(
            '\n'.join(
                [
                    "Couldn't find an acceptable libzmq on the system.",
                    "",
                    "If you expected pyzmq to link against an installed libzmq, please check to make sure:",
                    "",
                    "    * You have a C compiler installed",
                    "    * A development version of Python is installed (including headers)",
                    "    * A development version of ZMQ >= %s is installed (including headers)"
                    % v_str(min_good_zmq),
                    "    * If ZMQ is not in a default location, supply the argument --zmq=<path>",
                    "    * If you did recently install ZMQ to a default location,",
                    "      try rebuilding the ld cache with `sudo ldconfig`",
                    "      or specify zmq's location with `--zmq=/usr/local`",
                    "",
                ]
            )
        )

        info(
            '\n'.join(
                [
                    "You can skip all this detection/waiting nonsense if you know",
                    "you want pyzmq to bundle libzmq as an extension by passing:",
                    "",
                    "    `--zmq=bundled`",
                    "",
                    "I will now try to build libzmq as a Python extension",
                    "unless you interrupt me (^C) in the next 10 seconds...",
                    "",
                ]
            )
        )

        for i in range(10, 0, -1):
            sys.stdout.write('\r%2i...' % i)
            sys.stdout.flush()
            time.sleep(1)

        info("")

        return self.bundle_libzmq_extension()

    def test_build(self, prefix, settings):
        """do a test build ob libzmq"""
        self.create_tempdir()
        settings = settings.copy()
        line()
        info("Configure: Autodetecting ZMQ settings...")
        info("    Custom ZMQ dir:       %s" % prefix)
        try:
            detected = detect_zmq(self.tempdir, compiler=self.compiler_type, **settings)
        finally:
            self.erase_tempdir()

        info("    ZMQ version detected: %s" % v_str(detected['vers']))

        return detected

    def finish_run(self):
        self.save_config('config', self.config)
        line()

    def run(self):
        cfg = self.config

        if cfg['libzmq_extension']:
            self.bundle_libzmq_extension()
            self.finish_run()
            return

        # When cross-compiling and zmq is given explicitly, we can't testbuild
        # (as we can't testrun the binary), we assume things are alright.
        if cfg['skip_check_zmq'] or self.cross_compiling:
            warn("Skipping zmq version check")
            self.finish_run()
            return

        zmq_prefix = cfg['zmq_prefix']
        # There is no available default on Windows, so start with fallback unless
        # zmq was given explicitly, or libzmq extension was explicitly prohibited.
        if (
            sys.platform.startswith("win")
            and not cfg['no_libzmq_extension']
            and not zmq_prefix
        ):
            self.fallback_on_bundled()
            self.finish_run()
            return

        # first try with given config or defaults
        try:
            self.check_zmq_version()
        except LibZMQVersionError as e:
            info("\nBad libzmq version: %s\n" % e)
        except Exception as e:
            # print the error as distutils would if we let it raise:
            info("\nerror: %s\n" % e)
        else:
            self.finish_run()
            return

        # try fallback on /usr/local on *ix if no prefix is given
        if not zmq_prefix and not sys.platform.startswith('win'):
            info("Failed with default libzmq, trying again with /usr/local")
            time.sleep(1)
            zmq_prefix = cfg['zmq_prefix'] = '/usr/local'
            self.init_settings_from_config()
            try:
                self.check_zmq_version()
            except LibZMQVersionError as e:
                info("\nBad libzmq version: %s\n" % e)
            except Exception as e:
                # print the error as distutils would if we let it raise:
                info("\nerror: %s\n" % e)
            else:
                # if we get here the second run succeeded, so we need to update compiler
                # settings for the extensions with /usr/local prefix
                self.finish_run()
                return

        # finally, fallback on bundled

        if cfg['no_libzmq_extension']:
            fatal(
                "Falling back on bundled libzmq,"
                " but config has explicitly prohibited building the libzmq extension."
            )

        self.fallback_on_bundled()

        self.finish_run()


class FetchCommand(Command):
    """Fetch libzmq, that's it."""

    description = "Fetch libzmq sources or dll"

    user_options = [
        ('dll', None, "Fetch binary dll (Windows only)"),
    ]

    def initialize_options(self):
        self.dll = False

    def finalize_options(self):
        pass

    def run(self):
        # fetch sources for libzmq extension:
        if self.dll:
            self.fetch_libzmq_dll()
        else:
            self.fetch_libzmq_src()

    def fetch_libzmq_dll(self):
        libdir = "libzmq-dll"
        if os.path.exists(libdir):
            info("Scrubbing directory: %s" % libdir)
            shutil.rmtree(libdir)
        if not os.path.exists(libdir):
            os.makedirs(libdir)
        fetch_libzmq_dll(libdir)
        for archive in glob(pjoin(libdir, '*.zip')):
            os.remove(archive)

    def fetch_libzmq_src(self):
        bundledir = "bundled"
        if os.path.exists(bundledir):
            info("Scrubbing directory: %s" % bundledir)
            shutil.rmtree(bundledir)
        if not os.path.exists(bundledir):
            os.makedirs(bundledir)
        fetch_libzmq(bundledir)
        for tarball in glob(pjoin(bundledir, '*.tar.gz')):
            os.remove(tarball)


class TestCommand(Command):
    """Custom distutils command to run the test suite."""

    description = (
        "Test PyZMQ (must have been built inplace: `setup.py build_ext --inplace`)"
    )

    user_options = []

    def initialize_options(self):
        self._dir = os.getcwd()

    def finalize_options(self):
        pass

    def run(self):
        """Run the test suite with py.test"""
        # crude check for inplace build:
        try:
            import zmq
        except ImportError:
            print_exc()
            fatal(
                '\n       '.join(
                    [
                        "Could not import zmq!",
                        "You must build pyzmq with 'python setup.py build_ext --inplace' for 'python setup.py test' to work.",
                        "If you did build pyzmq in-place, then this is a real error.",
                    ]
                )
            )
            sys.exit(1)

        info(
            "Testing pyzmq-%s with libzmq-%s" % (zmq.pyzmq_version(), zmq.zmq_version())
        )
        p = Popen([sys.executable, '-m', 'pytest', '-v', os.path.join('zmq', 'tests')])
        p.wait()
        sys.exit(p.returncode)


class GitRevisionCommand(Command):
    """find the current git revision and add it to zmq.sugar.version.__revision__"""

    description = "Store current git revision in version.py"

    user_options = []

    def initialize_options(self):
        self.version_py = pjoin('zmq', 'sugar', 'version.py')

    def run(self):
        try:
            p = Popen('git log -1'.split(), stdin=PIPE, stdout=PIPE, stderr=PIPE)
        except IOError:
            warn("No git found, skipping git revision")
            return

        if p.wait():
            warn("checking git branch failed")
            info(p.stderr.read())
            return

        line = p.stdout.readline().decode().strip()
        if not line.startswith('commit'):
            warn("bad commit line: %r" % line)
            return

        rev = line.split()[-1]

        # now that we have the git revision, we can apply it to version.py
        with open(self.version_py) as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            if line.startswith('__revision__'):
                lines[i] = "__revision__ = '%s'\n" % rev
                break
        with open(self.version_py, 'w') as f:
            f.writelines(lines)

    def finalize_options(self):
        pass


class CleanCommand(Command):
    """Custom distutils command to clean the .so and .pyc files."""

    user_options = [
        ('all', 'a', "remove all build output, not just temporary by-products")
    ]

    boolean_options = ['all']

    def initialize_options(self):
        self.all = None

    def finalize_options(self):
        pass

    def run(self):
        _clean_me = []
        _clean_trees = []

        for d in ('build', 'dist', 'conf'):
            if os.path.exists(d):
                _clean_trees.append(d)

        for root, dirs, files in os.walk('buildutils'):
            if any(root.startswith(pre) for pre in _clean_trees):
                continue
            for f in files:
                if os.path.splitext(f)[-1] == '.pyc':
                    _clean_me.append(pjoin(root, f))

            if '__pycache__' in dirs:
                _clean_trees.append(pjoin(root, '__pycache__'))

        for root, dirs, files in os.walk('zmq'):
            if any(root.startswith(pre) for pre in _clean_trees):
                continue

            for f in files:
                if os.path.splitext(f)[-1] in ('.pyc', '.so', '.o', '.pyd', '.json'):
                    _clean_me.append(pjoin(root, f))

            # remove generated cython files
            if self.all:
                for f in files:
                    f2 = os.path.splitext(f)
                    if f2[1] == '.c' and os.path.isfile(
                        os.path.join(root, f2[0]) + '.pyx'
                    ):
                        _clean_me.append(pjoin(root, f))

            for d in dirs:
                if d == '__pycache__':
                    _clean_trees.append(pjoin(root, d))

        bundled = glob(pjoin('zmq', 'libzmq*'))
        _clean_me.extend([b for b in bundled if b not in _clean_me])

        bundled_headers = glob(pjoin('zmq', 'include', '*.h'))
        _clean_me.extend([h for h in bundled_headers if h not in _clean_me])

        for clean_me in _clean_me:
            print("removing %s" % clean_me)
            try:
                os.unlink(clean_me)
            except Exception as e:
                print(e, file=sys.stderr)
        for clean_tree in _clean_trees:
            print("removing %s/" % clean_tree)
            try:
                shutil.rmtree(clean_tree)
            except Exception as e:
                print(e, file=sys.stderr)


class CheckSDist(sdist):
    """Custom sdist that ensures Cython has compiled all pyx files to c."""

    def initialize_options(self):
        sdist.initialize_options(self)
        self._pyxfiles = []
        for root, dirs, files in os.walk('zmq'):
            for f in files:
                if f.endswith('.pyx'):
                    self._pyxfiles.append(pjoin(root, f))

    def run(self):
        self.run_command('fetch_libzmq')
        if 'cython' in cmdclass:
            self.run_command('cython')
        else:
            for pyxfile in self._pyxfiles:
                cfile = pyxfile[:-3] + 'c'
                msg = (
                    "C-source file '%s' not found." % (cfile)
                    + " Run 'setup.py cython' before sdist."
                )
                assert os.path.isfile(cfile), msg
        sdist.run(self)


@contextmanager
def use_cxx(compiler):
    """use C++ compiler in this context

    used in fix_cxx which detects when C++ should be used
    """
    compiler_so_save = compiler.compiler_so[:]
    compiler_so_cxx = compiler.compiler_cxx + compiler.compiler_so[1:]
    # actually use CXX compiler
    compiler.compiler_so = compiler_so_cxx
    try:
        yield
    finally:
        # restore original state
        compiler.compiler_so = compiler_so_save


@contextmanager
def fix_cxx(compiler, extension):
    """Fix C++ compilation to use C++ compiler

    See https://bugs.python.org/issue1222585 for Python support for C++,
    which apparently doesn't exist and only works by accident.
    """
    if compiler.detect_language(extension.sources) != "c++":
        # no c++, nothing to do
        yield
        return
    _compile_save = compiler._compile

    def _compile_cxx(obj, src, ext, *args, **kwargs):
        if compiler.language_map.get(ext) == "c++":
            with use_cxx(compiler):
                _compile_save(obj, src, ext, *args, **kwargs)
        else:
            _compile_save(obj, src, ext, *args, **kwargs)

    compiler._compile = _compile_cxx
    try:
        yield
    finally:
        compiler._compile = _compile_save


class CheckingBuildExt(build_ext):
    """Subclass build_ext to get clearer report if Cython is necessary."""

    def check_cython_extensions(self, extensions):
        for ext in extensions:
            for src in ext.sources:
                if not os.path.exists(src):
                    fatal(
                        """Cython-generated file '%s' not found.
                Cython >= %s is required to compile pyzmq from a development branch.
                Please install Cython or download a release package of pyzmq.
                """
                        % (src, min_cython_version)
                    )

    def build_extensions(self):
        self.check_cython_extensions(self.extensions)
        self.check_extensions_list(self.extensions)

        if self.compiler.compiler_type == 'mingw32':
            customize_mingw(self.compiler)

        for ext in self.extensions:
            self.build_extension(ext)

    def build_extension(self, ext):
        with fix_cxx(self.compiler, ext):
            super().build_extension(ext)

        ext_path = self.get_ext_fullpath(ext.name)
        patch_lib_paths(ext_path, self.compiler.library_dirs)

    def finalize_options(self):
        # check version, to prevent confusing undefined constant errors
        self.distribution.run_command("configure")
        return super().finalize_options()


class ConstantsCommand(Command):
    """Rebuild templated files for constants

    To be run after adding new constants to `utils/constant_names`.
    """

    user_options = []

    def initialize_options(self):
        return

    def finalize_options(self):
        pass

    def run(self):
        from buildutils.constants import render_constants

        render_constants()


cmdclass = {
    "bdist_egg": bdist_egg if "bdist_egg" in sys.argv else bdist_egg_disabled,
    "clean": CleanCommand,
    "configure": Configure,
    "constants": ConstantsCommand,
    "fetch_libzmq": FetchCommand,
    "revision": GitRevisionCommand,
    "sdist": CheckSDist,
    "test": TestCommand,
}

# -----------------------------------------------------------------------------
# Extensions
# -----------------------------------------------------------------------------


def makename(path, ext):
    return os.path.abspath(pjoin('zmq', *path)) + ext


pxd = lambda *path: makename(path, '.pxd')
pxi = lambda *path: makename(path, '.pxi')
pyx = lambda *path: makename(path, '.pyx')
dotc = lambda *path: makename(path, '.c')
doth = lambda *path: makename(path, '.h')

libzmq = pxd('backend', 'cython', 'libzmq')
buffers = pxd('utils', 'buffers')
message = pxd('backend', 'cython', 'message')
context = pxd('backend', 'cython', 'context')
socket = pxd('backend', 'cython', 'socket')
checkrc = pxd('backend', 'cython', 'checkrc')
monqueue = pxd('devices', 'monitoredqueue')
mutex = doth('utils', 'mutex')

submodules = {
    'backend.cython': {
        'constants': [libzmq, pxi('backend', 'cython', 'constants')],
        'error': [libzmq, checkrc],
        '_poll': [libzmq, socket, context, checkrc],
        'utils': [libzmq, checkrc],
        'context': [context, libzmq, checkrc],
        'message': [libzmq, buffers, message, checkrc, mutex],
        'socket': [context, message, socket, libzmq, buffers, checkrc],
        '_device': [libzmq, socket, context, checkrc],
        '_proxy_steerable': [libzmq, socket, checkrc],
        '_version': [libzmq],
    },
    'devices': {
        'monitoredqueue': [buffers, libzmq, monqueue, socket, context, checkrc],
    },
}

# require cython 0.29
min_cython_version = "0.29"
cython_language_level = "3str"

try:
    import Cython

    if V(Cython.__version__) < V(min_cython_version):
        raise ImportError(
            "Cython >= %s required for cython build, found %s"
            % (min_cython_version, Cython.__version__)
        )
    from Cython.Build import cythonize
    from Cython.Distutils.build_ext import new_build_ext as build_ext_cython

    cython = True
except Exception:
    cython = False
    suffix = '.c'
    cmdclass['build_ext'] = CheckingBuildExt

    class MissingCython(Command):

        user_options = []

        def initialize_options(self):
            pass

        def finalize_options(self):
            pass

        def run(self):
            try:
                import Cython
            except ImportError:
                warn("Cython is missing")
            else:
                cv = getattr(Cython, "__version__", None)
                if cv is None or V(cv) < V(min_cython_version):
                    warn(
                        "Cython >= %s is required for compiling Cython sources, "
                        "found: %s" % (min_cython_version, cv or Cython)
                    )

    cmdclass['cython'] = MissingCython

else:

    suffix = '.pyx'

    class CythonCommand(build_ext_cython):
        """Custom distutils command subclassed from Cython.Distutils.build_ext
        to compile pyx->c, and stop there. All this does is override the
        C-compile method build_extension() with a no-op."""

        description = "Compile Cython sources to C"

        def build_extension(self, ext):
            pass

    class zbuild_ext(build_ext_cython):
        def build_extensions(self):
            if self.compiler.compiler_type == 'mingw32':
                customize_mingw(self.compiler)
            return super().build_extensions()

        def build_extension(self, ext):
            with fix_cxx(self.compiler, ext):
                super().build_extension(ext)
            ext_path = self.get_ext_fullpath(ext.name)
            patch_lib_paths(ext_path, self.compiler.library_dirs)

        def finalize_options(self):
            self.distribution.run_command("configure")
            return super().finalize_options()

    cmdclass["cython"] = CythonCommand
    cmdclass["build_ext"] = zbuild_ext

extensions = []
ext_include_dirs = [pjoin('zmq', sub) for sub in ('utils',)]
ext_kwargs = {
    'include_dirs': ext_include_dirs,
}

for submod, packages in submodules.items():
    for pkg in sorted(packages):
        sources = [pjoin("zmq", submod.replace(".", os.path.sep), pkg + suffix)]
        ext = Extension("zmq.%s.%s" % (submod, pkg), sources=sources, **ext_kwargs)
        extensions.append(ext)

if cython:
    # set binding so that compiled methods can be inspected
    # set language-level to 3str, requires Cython 0.29
    cython_directives = {"binding": True, "language_level": "3str"}
    if os.environ.get("PYZMQ_CYTHON_COVERAGE"):
        cython_directives["linetrace"] = True
    extensions = cythonize(extensions, compiler_directives=cython_directives)

if pypy:
    extensions = []

if pypy or os.environ.get("PYZMQ_BACKEND_CFFI"):
    cffi_modules = ['buildutils/build_cffi.py:ffi']
else:
    cffi_modules = []

package_data = {
    'zmq': ['*.pxd', '*.pyi', '*' + lib_ext, 'py.typed'],
    'zmq.backend': ['*.pyi'],
    'zmq.backend.cython': ['*.pxd', '*.pxi'],
    'zmq.backend.cffi': ['*.h', '*.c'],
    'zmq.devices': ['*.pxd'],
    'zmq.sugar': ['*.pyi'],
    'zmq.utils': ['*.pxd', '*.h', '*.json'],
}


def extract_version():
    """extract pyzmq version from sugar/version.py, so it's not multiply defined"""
    with open(pjoin('zmq', 'sugar', 'version.py')) as f:
        while True:
            line = f.readline()
            if line.startswith('VERSION'):
                lines = ["from typing import *\n"]
                while line and not line.startswith('def'):
                    lines.append(line)
                    line = f.readline()
                break
    ns = {}
    exec(''.join(lines), ns)
    return ns['__version__']


def find_packages():
    """adapted from IPython's setupbase.find_packages()"""
    packages = []
    for dir, subdirs, files in os.walk('zmq'):
        package = dir.replace(os.path.sep, '.')
        if '__init__.py' not in files:
            # not a package
            continue
        packages.append(package)
    return packages


# -----------------------------------------------------------------------------
# Main setup
# -----------------------------------------------------------------------------

with io.open('README.md', encoding='utf-8') as f:
    long_desc = f.read()

setup_args = dict(
    name="pyzmq",
    version=extract_version(),
    packages=find_packages(),
    ext_modules=extensions,
    cffi_modules=cffi_modules,
    package_data=package_data,
    author="Brian E. Granger, Min Ragan-Kelley",
    author_email="zeromq-dev@lists.zeromq.org",
    url="https://pyzmq.readthedocs.org",
    description="Python bindings for 0MQ",
    long_description=long_desc,
    long_description_content_type="text/markdown",
    license="LGPL+BSD",
    cmdclass=cmdclass,
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: GNU Library or Lesser General Public License (LGPL)",
        "License :: OSI Approved :: BSD License",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Microsoft :: Windows",
        "Operating System :: POSIX",
        "Topic :: System :: Networking",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
    ],
    zip_safe=False,
    python_requires=">=3.6",
    install_requires=[
        "py; implementation_name == 'pypy'",
        "cffi; implementation_name == 'pypy'",
    ],
    setup_requires=[
        "cffi; implementation_name == 'pypy'",
    ],
)
if not os.path.exists(os.path.join("zmq", "backend", "cython", "socket.c")):
    setup_args["setup_requires"].append(
        f"cython>={min_cython_version}; implementation_name == 'cpython'"
    )

setup(**setup_args)
