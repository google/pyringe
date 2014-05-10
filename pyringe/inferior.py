#! /usr/bin/env python
#
# Copyright 2014 Google Inc.  All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module handling communication with gdb.

Users of this module probably want to use the Inferior class, as it provides a
clean interface for communicating with gdb and a couple of functions for
performing common tasks (e.g. listing threads, moving around the stack, etc.)
"""
# TODO: split this file in two, with GdbProxy in a separate file.

import collections
import errno
import functools
import json
import logging
import os
import re
import select
import signal
import subprocess
import tempfile
import time


# Setting these overrides the defaults. See _SymbolFilePath.
SYMBOL_FILE = None  # default: <PAYLOAD_DIR>/python2.7.debug
PAYLOAD_DIR = os.path.join(os.path.dirname(__file__), 'payload')
TIMEOUT_DEFAULT = 3
TIMEOUT_FOREVER = None

_GDB_STARTUP_FILES = [
    'importsetup.py',
    'gdb_service.py',
]
_GDB_ARGS = ['gdb', '--nw', '--quiet', '--batch-silent']


def _SymbolFilePath():
  return SYMBOL_FILE or os.path.join(PAYLOAD_DIR, 'python2.7.debug')


class Error(Exception):
  pass


class ProxyError(Error):
  """A proxy for an exception that happened within gdb."""


class TimeoutError(Error):
  pass


class PositionError(Error):
  """Raised when a nonsensical debugger position is requested."""


class GdbProcessError(Error):
  """Thrown when attempting to start gdb when it's already running."""


### RPC protocol for gdb service ###
#
# In order to ensure compatibility with all versions of python JSON was
# chosen as the main data format for the communication protocol between
# the gdb-internal python process and the process using this module.
# RPC requests to GdbService ('the service') are JSON objects containing exactly
# two keys:
# * 'func' : the name of the function to be called in the service. RPCs for
#            function names starting with _ will be rejected by the service.
# * 'args' : An array containing all the parameters for the function. Due to
#            JSON's limitations, only positional arguments work. Most API
#            functions require a 'position' argument which is required to be a
#            3-element array specifying the selected pid, python thread id and
#            depth of the selected frame in the stack (where 0 is the outermost
#            frame).
# The session is terminated upon sending an RPC request for the function
# '__kill__' (upon which args are ignored).
#
# RPC return values are not wrapped in JSON objects, but are bare JSON
# representations of return values.
# Python class instances (old and new-style) will also be serialized to JSON
# objects with keys '__pyringe_type_name__' and '__pyringe_address__', which
# carry the expected meaning. The remaining keys in these objects are simple
# JSON representations of the attributes visible in the instance (this means the
# object includes class-level attributes, but these are overshadowed by any
# instance attributes. (There is currently no recursion in this representation,
# only one level of object references is serialized in this way.)
# Should an exception be raised to the top level within the service, it will
# write a JSON-representation of the traceback string to stderr

# TODO: add message-id to the protocol to make sure that canceled operations
# that never had their output read don't end up supplying output for the wrong
# command


class ProxyObject(object):

  def __init__(self, attrdict):
    self.__dict__ = attrdict

  def __repr__(self):
    return ('<proxy of %s object at remote 0x%x>'
            % (self.__pyringe_type_name__, self.__pyringe_address__))


class GdbProxy(object):
  """The gdb that is being run as a service for the inferior.

  Most of the logic of this service is actually run from within gdb, this being
  a stub which handles RPC for that service. Communication with that service
  is done by pushing around JSON encoded dicts specifying RPC requests and
  their results. Automatic respawning is not handled by this class and must be
  implemented on top of this if it is to be available.
  """

  firstrun = True

  def __init__(self, args=None, arch=None):
    super(GdbProxy, self).__init__()
    gdb_version = GdbProxy.Version()
    if gdb_version < (7, 4, None) and GdbProxy.firstrun:
      # The user may have a custom-built version, so we only warn them
      logging.warning('Your version of gdb may be unsupported (< 7.4), '
                      'proceed with caution.')
      GdbProxy.firstrun = False

    arglist = _GDB_ARGS
    # Due to a design flaw in the C part of the gdb python API, setting the
    # target architecture from within a running script doesn't work, so we have
    # to do this with a command line flag.
    if arch:
        arglist = arglist + ['--eval-command', 'set architecture ' + arch]
    arglist = (arglist +
               ['--command=' + os.path.join(PAYLOAD_DIR, fname)
                for fname in _GDB_STARTUP_FILES])

    # Add version-specific args
    if gdb_version >= (7, 6, 1):
      # We want as little interference from user settings as possible,
      # but --nh was only introduced in 7.6.1
      arglist.append('--nh')

    if args:
      arglist.extend(args)

    # We use a temporary file for pushing IO between pyringe and gdb so we
    # don't have to worry about writes larger than the capacity of one pipe
    # buffer and handling partial writes/reads.
    # Since file position is automatically advanced by file writes (so writing
    # then reading from the same file will yield an 'empty' read), we need to
    # reopen the file to get different file offset. We can't use os.dup for
    # this because of the way os.dup is implemented.
    outfile_w = tempfile.NamedTemporaryFile(mode='w', bufsize=1)
    errfile_w = tempfile.NamedTemporaryFile(mode='w', bufsize=1)
    self._outfile_r = open(outfile_w.name)
    self._errfile_r = open(errfile_w.name)

    logging.debug('Starting new gdb process...')
    self._process = subprocess.Popen(
        bufsize=0,
        args=arglist,
        stdin=subprocess.PIPE,
        stdout=outfile_w.file,
        stderr=errfile_w.file,
        close_fds=True,
        preexec_fn=os.setpgrp,
        )
    outfile_w.close()
    errfile_w.close()

    self._poller = select.poll()
    self._poller.register(self._outfile_r.fileno(),
                          select.POLLIN | select.POLLPRI)
    self._poller.register(self._errfile_r.fileno(),
                          select.POLLIN | select.POLLPRI)

  def __getattr__(self, name):
    """Handles transparent proxying to gdb subprocess.

    This returns a lambda which, when called, sends an RPC request to gdb
    Args:
      name: The method to call within GdbService
    Returns:
      The result of the RPC.
    """
    return lambda *args, **kwargs: self._Execute(name, *args, **kwargs)

  def Kill(self):
    """Send death pill to Gdb and forcefully kill it if that doesn't work."""
    try:
      if self.is_running:
        self.Detach()
      if self._Execute('__kill__') == '__kill_ack__':
        # acknowledged, let's give it some time to die in peace
        time.sleep(0.1)
    except (TimeoutError, ProxyError):
      logging.debug('Termination request not acknowledged, killing gdb.')
    if self.is_running:
      # death pill didn't seem to work. We don't want the inferior to get killed
      # the next time it hits a dangling breakpoint, so we send a SIGINT to gdb,
      # which makes it disable instruction breakpoints for the time being.
      os.kill(self._process.pid, signal.SIGINT)
      # Since SIGINT has higher priority (with signal number 2) than SIGTERM
      # (signal 15), SIGTERM cannot preempt the signal handler for SIGINT.
      self._process.terminate()
      self._process.wait()
    self._errfile_r.close()
    self._outfile_r.close()

  @property
  def is_running(self):
    return self._process.poll() is None

  @staticmethod
  def Version():
    """Gets the version of gdb as a 3-tuple.

    The gdb devs seem to think it's a good idea to make --version
    output multiple lines of welcome text instead of just the actual version,
    so we ignore everything it outputs after the first line.
    Returns:
      The installed version of gdb in the form
      (<major>, <minor or None>, <micro or None>)
      gdb 7.7 would hence show up as version (7,7)
    """
    output = subprocess.check_output(['gdb', '--version']).split('\n')[0]
    # Example output (Arch linux):
    # GNU gdb (GDB) 7.7
    # Example output (Debian sid):
    # GNU gdb (GDB) 7.6.2 (Debian 7.6.2-1)
    # Example output (Debian wheezy):
    # GNU gdb (GDB) 7.4.1-debian
    # Example output (centos 2.6.32):
    # GNU gdb (GDB) Red Hat Enterprise Linux (7.2-56.el6)

    # As we've seen in the examples above, versions may be named very liberally
    # So we assume every part of that string may be the "real" version string
    # and try to parse them all. This too isn't perfect (later strings will
    # overwrite information gathered from previous ones), but it should be
    # flexible enough for everything out there.
    major = None
    minor = None
    micro = None
    for potential_versionstring in output.split():
      version = re.split('[^0-9]', potential_versionstring)
      try:
        major = int(version[0])
      except (IndexError, ValueError):
        pass
      try:
        minor = int(version[1])
      except (IndexError, ValueError):
        pass
      try:
        micro = int(version[2])
      except (IndexError, ValueError):
        pass
    return (major, minor, micro)

  # On JSON handling:
  # The python2 json module ignores the difference between unicode and str
  # objects, emitting only unicode objects (as JSON is defined as
  # only having unicode strings). In most cases, this is the wrong
  # representation for data we were sent from the inferior, so we try to convert
  # the unicode objects to normal python strings to make debugger output more
  # readable and to make "real" unicode objects stand out.
  # Luckily, the json module just throws an exception when trying to serialize
  # binary data (that is, bytearray in py2, byte in py3).
  # The only piece of information deemed relevant that is lost is the type of
  # non-string dict keys, as these are not supported in JSON. {1: 1} in the
  # inferior will thus show up as {"1": 1} in the REPL.
  # Properly transmitting python objects would require either substantially
  # building on top of JSON or switching to another serialization scheme.

  def _TryStr(self, maybe_unicode):
    try:
      return str(maybe_unicode)
    except UnicodeEncodeError:
      return maybe_unicode

  def _JsonDecodeList(self, data):
    rv = []
    for item in data:
      if isinstance(item, unicode):
        item = self._TryStr(item)
      elif isinstance(item, list):
        item = self._JsonDecodeList(item)
      rv.append(item)
    return rv

  def _JsonDecodeDict(self, data):
    """Json object decode hook that automatically converts unicode objects."""
    rv = {}
    for key, value in data.iteritems():
      if isinstance(key, unicode):
        key = self._TryStr(key)
      if isinstance(value, unicode):
        value = self._TryStr(value)
      elif isinstance(value, list):
        value = self._JsonDecodeList(value)
      rv[key] = value
    if '__pyringe_type_name__' in data:
      # We're looking at a proxyobject
      rv = ProxyObject(rv)
    return rv

  # There is a reason for this messy method signature, it's got to do with
  # python 2's handling of function arguments, how this class is expected to
  # behave and the responsibilities of __getattr__. Suffice it to say that if
  # this were python 3, we wouldn't have to do this.
  def _Execute(self, funcname, *args, **kwargs):
    """Send an RPC request to the gdb-internal python.

    Blocks for 3 seconds by default and returns any results.
    Args:
      funcname: the name of the function to call.
      *args: the function's arguments.
      **kwargs: Only the key 'wait_for_completion' is inspected, which decides
        whether to wait forever for completion or just 3 seconds.
    Returns:
      The result of the function call.
    """
    wait_for_completion = kwargs.get('wait_for_completion', False)
    rpc_dict = {'func': funcname, 'args': args}
    self._Send(json.dumps(rpc_dict))
    timeout = TIMEOUT_FOREVER if wait_for_completion else TIMEOUT_DEFAULT

    result_string = self._Recv(timeout)

    try:
      result = json.loads(result_string, object_hook=self._JsonDecodeDict)
      if isinstance(result, unicode):
        result = self._TryStr(result)
      elif isinstance(result, list):
        result = self._JsonDecodeList(result)
    except ValueError:
      raise ValueError('Response JSON invalid: ' + str(result_string))
    except TypeError:
      raise ValueError('Response JSON invalid: ' + str(result_string))

    return result

  def _Send(self, string):
    """Write a string of data to the gdb-internal python interpreter."""
    self._process.stdin.write(string + '\n')

  def _Recv(self, timeout):
    """Receive output from gdb.

    This reads gdb's stdout and stderr streams, returns a single line of gdb's
    stdout or rethrows any exceptions thrown from within gdb as well as it can.

    Args:
      timeout: floating point number of seconds after which to abort.
          A value of None or TIMEOUT_FOREVER means "there is no timeout", i.e.
          this might block forever.
    Raises:
      ProxyError: All exceptions received from the gdb service are generically
          reraised as this.
      TimeoutError: Raised if no answer is received from gdb in after the
          specified time.
    Returns:
      The current contents of gdb's stdout buffer, read until the next newline,
      or `None`, should the read fail or timeout.
    """

    buf = ''
    # The messiness of this stems from the "duck-typiness" of this function.
    # The timeout parameter of poll has different semantics depending on whether
    # it's <=0, >0, or None. Yay.

    wait_for_line = timeout is TIMEOUT_FOREVER
    deadline = time.time() + (timeout if not wait_for_line else 0)

    def TimeLeft():
      return max(1000 * (deadline - time.time()), 0)

    continue_reading = True

    while continue_reading:
      poll_timeout = None if wait_for_line else TimeLeft()

      fd_list = [event[0] for event in self._poller.poll(poll_timeout)
                 if event[1] & (select.POLLIN | select.POLLPRI)]
      if not wait_for_line and TimeLeft() == 0:
        continue_reading = False
      if self._outfile_r.fileno() in fd_list:
        buf += self._outfile_r.readline()
        if buf.endswith('\n'):
          return buf

      # GDB-internal exception passing
      if self._errfile_r.fileno() in fd_list:
        exc = self._errfile_r.readline()
        if exc:
          exc_text = '\n-----------------------------------\n'
          exc_text += 'Error occurred within GdbService:\n'
          try:
            exc_text += json.loads(exc)
          except ValueError:
            # whatever we got back wasn't valid JSON.
            # This usually means we've run into an exception before the special
            # exception handling was turned on. The first line we read up there
            # will have been "Traceback (most recent call last):". Obviously, we
            # want the rest, too, so we wait a bit and read it.
            deadline = time.time() + 0.5
            while self.is_running and TimeLeft() > 0:
              exc += self._errfile_r.read()
            try:
              exc_text += json.loads(exc)
            except ValueError:
              exc_text = exc
          raise ProxyError(exc_text)
    # timeout
    raise TimeoutError()


class Inferior(object):
  """Class modeling the inferior process.

  Defines the interface for communication with the inferior and handles
  debugging context and automatic respawning of the underlying gdb service.
  """

  _gdb = None
  _Position = collections.namedtuple('Position', 'pid tid frame_depth')  # pylint: disable=invalid-name
  # tid is the thread ident as reported by threading.current_thread().ident
  # frame_depth is the 'depth' (as measured from the outermost frame) of the
  # requested frame. A value of -1 will hence mean the most recent frame.

  def __init__(self, pid, auto_symfile_loading=True, architecture='i386:x86-64'):
    super(Inferior, self).__init__()
    self.position = self._Position(pid=pid, tid=None, frame_depth=-1)
    self._symbol_file = None
    self.arch = architecture
    self.auto_symfile_loading = auto_symfile_loading

    # Inferior objects are created before the user ever issues the 'attach'
    # command, but since this is used by `Reinit`, we call upon gdb to do this
    # for us.
    if pid:
      self.StartGdb()

  def needsattached(func):
    """Decorator to prevent commands from being used when not attached."""

    @functools.wraps(func)
    def wrap(self, *args, **kwargs):
      if not self.attached:
        raise PositionError('Not attached to any process.')
      return func(self, *args, **kwargs)
    return wrap

  @needsattached
  def Cancel(self):
    self.ShutDownGdb()

  def Reinit(self, pid, auto_symfile_loading=True):
    """Reinitializes the object with a new pid.

    Since all modes might need access to this object at any time, this object
    needs to be long-lived. To make this clear in the API, this shorthand is
    supplied.
    Args:
      pid: the pid of the target process
      auto_symfile_loading: whether the symbol file should automatically be
        loaded by gdb.
    """
    self.ShutDownGdb()
    self.__init__(pid, auto_symfile_loading, architecture=self.arch)

  @property
  def gdb(self):
    # when requested, make sure we have a gdb session to return
    # (in case it crashed at some point)
    if not self._gdb or not self._gdb.is_running:
      self.StartGdb()
    return self._gdb

  def StartGdb(self):
    """Starts gdb and attempts to auto-load symbol file (unless turned off).

    Raises:
      GdbProcessError: if gdb is already running
    """
    if self.attached:
      raise GdbProcessError('Gdb is already running.')
    self._gdb = GdbProxy(arch=self.arch)
    self._gdb.Attach(self.position)

    if self.auto_symfile_loading:
      try:
        self.LoadSymbolFile()
      except (ProxyError, TimeoutError) as err:
        self._gdb = GdbProxy(arch=self.arch)
        self._gdb.Attach(self.position)
        if not self.gdb.IsSymbolFileSane(self.position):
          logging.warning('Failed to automatically load a sane symbol file, '
                          'most functionality will be unavailable until symbol'
                          'file is provided.')
          logging.debug(err.message)

  def ShutDownGdb(self):
    if self._gdb and self._gdb.is_running:
      self._gdb.Kill()
    self._gdb = None

  def LoadSymbolFile(self, path=None):
    # As automatic respawning of gdb may happen between calls to this, we have
    # to remember which symbol file we're supposed to load.
    if path:
      self._symbol_file = path
    s_path = self._symbol_file or _SymbolFilePath()
    logging.debug('Trying to load symbol file: %s' % s_path)
    if self.attached:
      self.gdb.LoadSymbolFile(self.position, s_path)
      if not self.gdb.IsSymbolFileSane(self.position):
        logging.warning('Symbol file failed sanity check, '
                        'proceed at your own risk')

  @needsattached
  def Backtrace(self):
    return self.gdb.BacktraceAt(self.position)

  @needsattached
  def Up(self):
    depth = self.position.frame_depth
    if self.position.frame_depth < 0:
      depth = self.gdb.StackDepth(self.position) + self.position.frame_depth
    if not depth:
      raise PositionError('Already at outermost stack frame')
    self.position = self._Position(pid=self.position.pid,
                                   tid=self.position.tid,
                                   frame_depth=depth-1)

  @needsattached
  def Down(self):
    if (self.position.frame_depth + 1 >= self.gdb.StackDepth(self.position)
        or self.position.frame_depth == -1):
      raise PositionError('Already at innermost stack frame')
    frame_depth = self.position.frame_depth + 1
    self.position = self._Position(pid=self.position.pid,
                                   tid=self.position.tid,
                                   frame_depth=frame_depth)

  @needsattached
  def Lookup(self, var_name):
    return self.gdb.LookupInFrame(self.position, var_name)

  @needsattached
  def InferiorLocals(self):
    return self.gdb.InferiorLocals(self.position)

  @needsattached
  def InferiorGlobals(self):
    return self.gdb.InferiorGlobals(self.position)

  @needsattached
  def InferiorBuiltins(self):
    return self.gdb.InferiorBuiltins(self.position)

  @property
  def is_running(self):
    if not self.position.pid:
      return False
    try:
      # sending a 0 signal to a process does nothing
      os.kill(self.position.pid, 0)
      return True
    except OSError as err:
      # We might (for whatever reason) simply not be permitted to do this.
      if err.errno == errno.EPERM:
        logging.debug('Reveived EPERM when trying to signal inferior.')
        return True
      return False

  @property
  def pid(self):
    return self.position.pid

  @property
  @needsattached
  def threads(self):
    # return array of python thread idents. Unfortunately, we can't easily
    # access the given thread names without taking the GIL.
    return self.gdb.ThreadIds(self.position)

  @property
  @needsattached
  def current_thread(self):
    threads = self.threads
    if not threads:
      self.position = self._Position(pid=self.position.pid, tid=None,
                                     frame_depth=-1)
      return None
    if not self.position.tid or self.position.tid not in threads:
      self.position = self._Position(pid=self.position.pid, tid=self.threads[0],
                                     frame_depth=-1)
    return self.position.tid

  @needsattached
  def SelectThread(self, tid):
    if tid in self.gdb.ThreadIds(self.position):
      self.position = self._Position(self.position.pid, tid, frame_depth=-1)
    else:
      logging.error('Thread ' + str(tid) + ' does not exist')

  @needsattached
  def Continue(self):
    self.gdb.Continue(self.position)

  @needsattached
  def Interrupt(self):
    return self.gdb.Interrupt(self.position)

  @property
  def attached(self):
    if (self.position.pid
        and self.is_running
        and self._gdb
        and self._gdb.is_running):
      return True
    return False
