"""Config functions"""
# -----------------------------------------------------------------------------
#  Copyright (C) PyZMQ Developers
#
#  This file is part of pyzmq, copied and adapted from h5py.
#  h5py source used under the New BSD license
#
#  h5py: <http://code.google.com/p/h5py/>
#
#  Distributed under the terms of the New BSD License.  The full license is in
#  the file COPYING.BSD, distributed as part of this software.
# -----------------------------------------------------------------------------

import sys
import os
import json

from configparser import ConfigParser

pjoin = os.path.join
from .msg import debug, fatal, warn

# -----------------------------------------------------------------------------
# Utility functions (adapted from h5py: https://www.h5py.org/)
# -----------------------------------------------------------------------------


def load_config(name, base='conf'):
    """Load config dict from JSON"""
    fname = pjoin(base, name + '.json')
    if not os.path.exists(fname):
        return {}
    try:
        with open(fname) as f:
            cfg = json.load(f)
    except Exception as e:
        warn("Couldn't load %s: %s" % (fname, e))
        cfg = {}
    return cfg


def save_config(name, data, base='conf'):
    """Save config dict to JSON"""
    if not os.path.exists(base):
        os.mkdir(base)
    fname = pjoin(base, name + '.json')
    with open(fname, 'w') as f:
        json.dump(data, f, indent=2)


def v_str(v_tuple):
    """turn (2,0,1) into '2.0.1'."""
    return ".".join(str(x) for x in v_tuple)


def get_env_args():
    """ Look for options in environment vars """

    settings = {}

    zmq = os.environ.get("ZMQ_PREFIX")
    if zmq:
        debug("Found environ var ZMQ_PREFIX=%s" % zmq)
        settings['zmq_prefix'] = zmq
    draft_api = os.environ.get("ZMQ_DRAFT_API")
    if draft_api:
        debug("Found environ var ZMQ_DRAFT_API=%s" % draft_api)
        settings['zmq_draft_api'] = int(draft_api)

    return settings


def cfg2dict(cfg):
    """turn a ConfigParser into a nested dict

    because ConfigParser objects are dumb.
    """
    d = {}
    for section in cfg.sections():
        d[section] = dict(cfg.items(section))
    return d


def get_cfg_args():
    """ Look for options in setup.cfg """

    if not os.path.exists('setup.cfg'):
        return {}
    cfg = ConfigParser()
    cfg.read('setup.cfg')
    cfg = cfg2dict(cfg)

    g = cfg.setdefault('global', {})
    # boolean keys:
    for key in [
        'libzmq_extension',
        'bundle_libzmq_dylib',
        'no_libzmq_extension',
        'have_sys_un_h',
        'skip_check_zmq',
        'bundle_msvcp',
    ]:
        if key in g:
            g[key] = eval(g[key])

    # globals go to top level
    cfg.update(cfg.pop('global'))
    return cfg


def config_from_prefix(prefix):
    """Get config from zmq prefix"""
    settings = {}
    if prefix.lower() in ('default', 'auto', ''):
        settings['zmq_prefix'] = ''
        settings['libzmq_extension'] = False
        settings['no_libzmq_extension'] = False
    elif prefix.lower() in ('bundled', 'extension'):
        settings['zmq_prefix'] = ''
        settings['libzmq_extension'] = True
        settings['no_libzmq_extension'] = False
    else:
        settings['zmq_prefix'] = os.path.abspath(prefix)
        settings['libzmq_extension'] = False
        settings['no_libzmq_extension'] = True
        settings['allow_legacy_libzmq'] = True  # explicit zmq prefix allows legacy
    return settings


def merge(into, d):
    """merge two containers

    into is updated, d has priority
    """
    if isinstance(into, dict):
        for key in d.keys():
            if key not in into:
                into[key] = d[key]
            else:
                into[key] = merge(into[key], d[key])
        return into
    elif isinstance(into, list):
        return into + d
    else:
        return d


def discover_settings(conf_base=None):
    """ Discover custom settings for ZMQ path"""
    settings = {
        'zmq_prefix': '',
        'zmq_draft_api': False,
        'libzmq_extension': False,
        'no_libzmq_extension': False,
        'skip_check_zmq': False,
        'allow_legacy_libzmq': False,
        'bundle_msvcp': None,
        'build_ext': {},
        'bdist_egg': {},
        'win_ver': None,
    }
    if sys.platform.startswith('win'):
        settings['have_sys_un_h'] = False
        # target Windows version, sets WINVER, _WIN32_WINNT macros
        # see https://docs.microsoft.com/en-us/cpp/porting/modifying-winver-and-win32-winnt for reference
        # see https://github.com/python/cpython/blob/v3.9.1/PC/pyconfig.h#L137-L159
        # for CPython's own values
        if sys.version_info >= (3, 9):
            # CPython 3.9 targets Windows 8 (0x0602)
            settings["win_ver"] = "0x0602"
        else:
            # older Python, target Windows 7 (0x0601)
            # CPython itself targets Vista (0x0600)
            settings["win_ver"] = "0x0601"

    if conf_base:
        # lowest priority
        merge(settings, load_config('config', conf_base))
    merge(settings, get_cfg_args())
    merge(settings, get_env_args())

    return settings
