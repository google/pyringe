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
"""Logic for interacting with the inferior run from within gdb.

This needs to be run from within gdb so we have access to the gdb module.
As we can't make any assumptions about which python version gdb has been
compiled to use, this shouldn't use any fancy py3k constructs.
Interaction with the REPL part of the debugger is done through a simple RPC
mechanism based on JSON dicts shoved through stdin/stdout.
"""

import collections
import json
import os
import re
import sys
import traceback
import zipfile
# GDB already imports this for us, but this shuts up lint
import gdb
import libpython

Position = collections.namedtuple('Position', 'pid tid frame_depth')


class Error(Exception):
  pass


class PositionUnavailableException(Error):
  pass


class RpcException(Error):
  pass


class GdbCache(object):
  """Cache of gdb objects for common symbols."""

  # Work around the fact that when this gets bound, we don't yet have symbols
  DICT = None
  TYPE = None
  INTERP_HEAD = None
  PENDINGBUSY = None
  PENDINGCALLS_TO_DO = None

  @staticmethod
  def Refresh():
    """looks up symbols within the inferior and caches their names / values.

    If debugging information is only partial, this method does its best to
    find as much information as it can, validation can be done using
    IsSymbolFileSane.
    """
    try:
      GdbCache.DICT = gdb.lookup_type('PyDictObject').pointer()
      GdbCache.TYPE = gdb.lookup_type('PyTypeObject').pointer()
    except gdb.error as err:
      # The symbol file we're using doesn't seem to provide type information.
      pass
    interp_head_name = GdbCache.FuzzySymbolLookup('interp_head')
    if interp_head_name:
      GdbCache.INTERP_HEAD = gdb.parse_and_eval(interp_head_name)
    else:
      # As a last resort, ask the inferior about it.
      GdbCache.INTERP_HEAD = gdb.parse_and_eval('PyInterpreterState_Head()')
    GdbCache.PENDINGBUSY = GdbCache.FuzzySymbolLookup('pendingbusy')
    GdbCache.PENDINGCALLS_TO_DO = GdbCache.FuzzySymbolLookup('pendingcalls_to_do')

  @staticmethod
  def FuzzySymbolLookup(symbol_name):
    try:
      gdb.parse_and_eval(symbol_name)
      return symbol_name
    except gdb.error as err:
      # No symbol in current context. We might be dealing with static symbol
      # disambiguation employed by compilers. For example, on debian's current
      # python build, the 'interp_head' symbol (which we need) has been renamed
      # to 'interp_head.42174'. This mangling is of course compiler-specific.
      # We try to get around it by using gdb's built-in regex support when
      # looking up variables
      # Format:
      # All variables matching regular expression "<symbol_name>":
      #
      # File <source_file>:
      # <Type><real_symbol_name>;
      #
      # Non-debugging symbols:
      # 0x<address>  <real_symbol_name>

      # We're only interested in <real_symbol_name>. The latter part
      # ('Non-debugging symbols') is only relevant if debugging info is partial.
      listing = gdb.execute('info variables %s' % symbol_name, to_string=True)
      # sigh... We want whatever was in front of ;, but barring any *s.
      # If you are a compiler dev who mangles symbols using ';' and '*',
      # you deserve this breakage.
      mangled_name = (re.search(r'\**(\S+);$', listing, re.MULTILINE)
                      or re.search(r'^0x[0-9a-fA-F]+\s+(\S+)$', listing, re.MULTILINE))
      if not mangled_name:
        raise err
      try:
        gdb.parse_and_eval('\'%s\'' % mangled_name.group(1))
        return '\'%s\'' % mangled_name.group(1)
      except gdb.error:
        # We could raise this, but the original exception will likely describe
        # the problem better
        raise err


class PyFrameObjectPtr(libpython.PyFrameObjectPtr):
  """Patched version of PyFrameObjectPtr that handles reading zip files."""

  def current_line_num(self):
    try:
      return super(PyFrameObjectPtr, self).current_line_num()
    except ValueError:
      # Work around libpython.py's mishandling of oner-liners
      return libpython.int_from_int(self.co.field('co_firstlineno'))

  def OpenFile(self, filepath):
    """open()-replacement that automatically handles zip files.

    This assumes there is at most one .zip in the file path.
    Args:
      filepath: the path to the file to open.
    Returns:
      An open file-like object.
    """
    archive = False
    if '.zip/' in filepath:
      archive = True
      archive_type = '.zip'
    if '.par/' in filepath:
      archive = True
      archive_type = '.par'
    if archive:
      path, archived_file = filepath.split(archive_type)
      path += archive_type
      zip_file = zipfile.ZipFile(path)
      return zip_file.open(archived_file.strip('/'))
    return open(filepath)

  def extract_filename(self):
    """Alternative way of getting the executed file which inspects globals."""
    globals_gdbval = self._gdbval['f_globals'].cast(GdbCache.DICT)
    global_dict = libpython.PyDictObjectPtr(globals_gdbval)
    for key, value in global_dict.iteritems():
      if str(key.proxyval(set())) == '__file__':
        return str(value.proxyval(set()))

  def current_line(self):
    if self.is_optimized_out():
      return '(frame information optimized out)'
    filename = self.filename()
    inferior_cwd = '/proc/%d/cwd' % gdb.selected_inferior().pid
    if filename.startswith('/dev/fd/'):
      filename.replace('/dev/fd/',
                       '/proc/%d/fd/' % gdb.selected_inferior().pid,
                       1)
    else:
      filename = os.path.join(inferior_cwd, filename)
    try:
      sourcefile = self.OpenFile(filename)
    except IOError:
      # couldn't find the file, let's try extracting the path from the frame
      filename = self.extract_filename()
      if filename.endswith('.pyc'):
        filename = filename[:-1]
      try:
        sourcefile = self.OpenFile(filename)
      except IOError:
        return '<file not available>'
    for _ in xrange(self.current_line_num()):
      line = sourcefile.readline()
    sourcefile.close()
    return line if line else '<file not available>'


class GdbService(object):
  """JSON-based RPC Service for commanding gdb."""

  def __init__(self, stdin=None, stdout=None, stderr=None):
    self.stdin = stdin or sys.stdin
    self.stdout = stdout or sys.stdout
    self.stderr = stderr or sys.stderr

  @property
  def breakpoints(self):
    # work around API weirdness
    if gdb.breakpoints():
      return gdb.breakpoints()
    return ()

  def _UnserializableObjectFallback(self, obj):
    """Handles sanitizing of unserializable objects for Json.

    For instances of heap types, we take the class dict, augment it with the
    instance's __dict__, tag it and transmit it over to the RPC client to be
    reconstructed there. (Works with both old and new style classes)
    Args:
      obj: The object to Json-serialize
    Returns:
      A Json-serializable version of the parameter
    """
    if isinstance(obj, libpython.PyInstanceObjectPtr):
      # old-style classes use 'classobj'/'instance'
      # get class attribute dictionary
      in_class = obj.pyop_field('in_class')
      result_dict = in_class.pyop_field('cl_dict').proxyval(set())

      # let libpython.py do the work of getting the instance dict
      instanceproxy = obj.proxyval(set())
      result_dict.update(instanceproxy.attrdict)
      result_dict['__pyringe_type_name__'] = instanceproxy.cl_name
      result_dict['__pyringe_address__'] = instanceproxy.address
      return result_dict

    if isinstance(obj, libpython.HeapTypeObjectPtr):
      # interestingly enough, HeapTypeObjectPtr seems to handle all pointers to
      # heap type PyObjects, not only pointers to PyHeapTypeObject. This
      # corresponds to new-style class instances. However, as all instances of
      # new-style classes are simple PyObject pointers to the interpreter,
      # libpython.py tends to give us HeapTypeObjectPtrs for things we can't
      # handle properly.

      try:
        # get class attribute dictionary
        type_ptr = obj.field('ob_type')
        tp_dict = type_ptr.cast(GdbCache.TYPE)['tp_dict'].cast(GdbCache.DICT)
        result_dict = libpython.PyDictObjectPtr(tp_dict).proxyval(set())
      except gdb.error:
        # There was probably a type mismatch triggered by wrong assumptions in
        # libpython.py
        result_dict = {}

      try:
        # get instance attributes
        result_dict.update(obj.get_attr_dict().proxyval(set()))
        result_dict['__pyringe_type_name__'] = obj.safe_tp_name()
        result_dict['__pyringe_address__'] = long(obj._gdbval)  # pylint: disable=protected-access
        return result_dict
      except TypeError:
        # This happens in the case where we're not really looking at a heap type
        # instance. There isn't really anything we can do, so we fall back to
        # the default handling.
        pass
    # Default handler -- this does not result in proxy objects or fancy dicts,
    # but most of the time, we end up emitting strings of the format
    # '<object at remote 0x345a235>'
    try:
      proxy = obj.proxyval(set())
      # json doesn't accept non-strings as keys, so we're helping along
      if isinstance(proxy, dict):
        return {str(key): val for key, val in proxy.iteritems()}
      return proxy
    except AttributeError:
      return str(obj)

  def _Read(self):
    return self.stdin.readline()

  def _Write(self, string):
    self.stdout.write(string + '\n')

  def _WriteObject(self, obj):
    self._Write(json.dumps(obj, default=self._UnserializableObjectFallback))

  def _ReadObject(self):
    try:
      obj = json.loads(self._Read().strip())
      return obj
    except ValueError:
      pass

  def EvalLoop(self):
    while self._AcceptRPC():
      pass

  def _AcceptRPC(self):
    """Reads RPC request from stdin and processes it, writing result to stdout.

    Returns:
      True as long as execution is to be continued, False otherwise.
    Raises:
      RpcException: if no function was specified in the RPC or no such API
          function exists.
    """
    request = self._ReadObject()
    if request['func'] == '__kill__':
      self.ClearBreakpoints()
      self._WriteObject('__kill_ack__')
      return False
    if 'func' not in request or request['func'].startswith('_'):
      raise RpcException('Not a valid public API function.')
    rpc_result = getattr(self, request['func'])(*request['args'])
    self._WriteObject(rpc_result)
    return True

  def _UnpackGdbVal(self, gdb_value):
    """Unpacks gdb.Value objects and returns the best-matched python object."""
    val_type = gdb_value.type.code
    if val_type == gdb.TYPE_CODE_INT or val_type == gdb.TYPE_CODE_ENUM:
      return int(gdb_value)
    if val_type == gdb.TYPE_CODE_VOID:
      return None
    if val_type == gdb.TYPE_CODE_PTR:
      return long(gdb_value)
    if val_type == gdb.TYPE_CODE_ARRAY:
      # This is probably a string
      return str(gdb_value)
    # I'm out of ideas, let's return it as a string
    return str(gdb_value)

  def _IterateChainedList(self, head, next_item):
    while self._UnpackGdbVal(head):
      yield head
      head = head[next_item]

  # ----- gdb command api below -----

  def EnsureGdbPosition(self, pid, tid, frame_depth):
    """Make sure our position matches the request.

    Args:
      pid: The process ID of the target process
      tid: The python thread ident of the target thread
      frame_depth: The 'depth' of the requested frame in the frame stack
    Raises:
      PositionUnavailableException: If the requested process, thread or frame
          can't be found or accessed.
    """
    position = [pid, tid, frame_depth]
    if not pid:
      return
    if not self.IsAttached():
      try:
        self.Attach(position)
      except gdb.error as exc:
        raise PositionUnavailableException(exc.message)
    if gdb.selected_inferior().pid != pid:
      self.Detach()
      try:
        self.Attach(position)
      except gdb.error as exc:
        raise PositionUnavailableException(exc.message)

    if tid:
      tstate_head = GdbCache.INTERP_HEAD['tstate_head']
      for tstate in self._IterateChainedList(tstate_head, 'next'):
        if tid == tstate['thread_id']:
          self.selected_tstate = tstate
          break
      else:
        raise PositionUnavailableException('Thread %s does not exist.' %
                                           str(tid))
      stack_head = self.selected_tstate['frame']
      if frame_depth is not None:
        frames = list(self._IterateChainedList(stack_head, 'f_back'))
        frames.reverse()
        try:
          self.selected_frame = frames[frame_depth]
        except IndexError:
          raise PositionUnavailableException('Stack is not %s frames deep' %
                                             str(frame_depth + 1))

  def IsAttached(self):
    # The gdb python api is somewhat... weird.
    inf = gdb.selected_inferior()
    if inf.is_valid() and inf.pid and inf.threads():
      return True
    return False

  def LoadSymbolFile(self, position, path):
    pos = [position[0], None, None]
    self.ExecuteRaw(pos, 'symbol-file ' + path)
    GdbCache.Refresh()

  def IsSymbolFileSane(self, position):
    """Performs basic sanity check by trying to look up a bunch of symbols."""
    pos = [position[0], None, None]
    self.EnsureGdbPosition(*pos)
    try:
      if GdbCache.DICT and GdbCache.TYPE and GdbCache.INTERP_HEAD:
        # pylint: disable=pointless-statement
        tstate = GdbCache.INTERP_HEAD['tstate_head']
        tstate['thread_id']
        frame = tstate['frame']
        frame_attrs = ['f_back',
                       'f_locals',
                       'f_localsplus',
                       'f_globals',
                       'f_builtins',
                       'f_lineno',
                       'f_lasti']
        for attr_name in frame_attrs:
          # This lookup shouldn't throw an exception
          frame[attr_name]
        code = frame['f_code']
        code_attrs = ['co_name',
                      'co_filename',
                      'co_nlocals',
                      'co_varnames',
                      'co_lnotab',
                      'co_firstlineno']
        for attr_name in code_attrs:
          # Same as above, just checking whether the lookup succeeds.
          code[attr_name]
        # if we've gotten this far, we should be fine, as it means gdb managed
        # to look up values for all of these. They might still be null, the
        # symbol file might still be bogus, but making gdb check for null values
        # and letting it run into access violations is the best we can do. We
        # haven't checked any of the python types (dict, etc.), but this symbol
        # file seems to be useful for some things, so let's give it our seal of
        # approval.
        return True
    except gdb.error:
      return False
    # looks like the initial GdbCache refresh failed. That's no good.
    return False

  def Attach(self, position):
    pos = [position[0], position[1], None]
    # Using ExecuteRaw here would throw us into an infinite recursion, we have
    # to side-step it.
    gdb.execute('attach ' + str(pos[0]), to_string=True)

    try:
      # Shortcut for handling single-threaded python applications if we've got
      # the right symbol file loaded already
      GdbCache.Refresh()
      self.selected_tstate = self._ThreadPtrs(pos)[0]
    except gdb.error:
      pass

  def Detach(self):
    """Detaches from the inferior. If not attached, this is a no-op."""
    # We have to work around the python APIs weirdness :\
    if not self.IsAttached():
      return None
    # Gdb doesn't drain any pending SIGINTs it may have sent to the inferior
    # when it simply detaches. We can do this by letting the inferior continue,
    # and gdb will intercept any SIGINT that's still to-be-delivered; as soon as
    # we do so however, we may lose control of gdb (if we're running in
    # synchronous mode). So we queue an interruption and continue gdb right
    # afterwards, it will waitpid() for its inferior and collect all signals
    # that may have been queued.
    pid = gdb.selected_inferior().pid
    self.Interrupt([pid, None, None])
    self.Continue([pid, None, None])
    result = gdb.execute('detach', to_string=True)
    if not result:
      return None
    return result

  def _ThreadPtrs(self, position):
    self.EnsureGdbPosition(position[0], None, None)
    tstate_head = GdbCache.INTERP_HEAD['tstate_head']
    return [tstate for tstate in self._IterateChainedList(tstate_head, 'next')]

  def ThreadIds(self, position):
    # This corresponds to
    # [thr.ident for thr in threading.enumerate()]
    # except we don't need the GIL for this.
    return [self._UnpackGdbVal(tstate['thread_id'])
            for tstate in self._ThreadPtrs(position)]

  def ClearBreakpoints(self):
    for bkp in self.breakpoints:
      bkp.enabled = False
      bkp.delete()

  def Continue(self, position):
    return self.ExecuteRaw(position, 'continue')

  def Interrupt(self, position):
    return self.ExecuteRaw(position, 'interrupt')

  def Call(self, position, function_call):
    """Perform a function call in the inferior.

    WARNING: Since Gdb's concept of threads can't be directly identified with
    python threads, the function call will be made from what has to be assumed
    is an arbitrary thread. This *will* interrupt the inferior. Continuing it
    after the call is the responsibility of the caller.

    Args:
      position: the context of the inferior to call the function from.
      function_call: A string corresponding to a function call. Format:
        'foo(0,0)'
    Returns:
      Thre return value of the called function.
    """
    self.EnsureGdbPosition(position[0], None, None)
    if not gdb.selected_thread().is_stopped():
      self.Interrupt(position)
    result_value = gdb.parse_and_eval(function_call)
    return self._UnpackGdbVal(result_value)

  def ExecuteRaw(self, position, command):
    """Send a command string to gdb."""
    self.EnsureGdbPosition(position[0], None, None)
    return gdb.execute(command, to_string=True)

  def _GetGdbThreadMapping(self, position):
    """Gets a mapping from python tid to gdb thread num.

    There's no way to get the thread ident from a gdb thread.  We only get the
    "ID of the thread, as assigned by GDB", which is completely useless for
    everything except talking to gdb.  So in order to translate between these
    two, we have to execute 'info threads' and parse its output. Note that this
    may only work on linux, and only when python was compiled to use pthreads.
    It may work elsewhere, but we won't guarantee it.

    Args:
      position: array of pid, tid, framedepth specifying the requested position.
    Returns:
      A dictionary of the form {python_tid: gdb_threadnum}.
    """

    if len(gdb.selected_inferior().threads()) == 1:
      # gdb's output for info threads changes and only displays PID. We cheat.
      return {position[1]: 1}
    # example:
    #   8    Thread 0x7f0a637fe700 (LWP 11894) "test.py" 0x00007f0a69563e63 in
    #   select () from /usr/lib64/libc.so.6
    thread_line_regexp = r'\s*\**\s*([0-9]+)\s+[a-zA-Z]+\s+([x0-9a-fA-F]+)\s.*'
    output = gdb.execute('info threads', to_string=True)
    matches = [re.match(thread_line_regexp, line) for line
               in output.split('\n')[1:]]
    return {int(match.group(2), 16): int(match.group(1))
            for match in matches if match}

  def InjectFile(self, position, filepath):
    file_ptr = self.Call(position, 'fopen(%s, "r")' % json.dumps(filepath))
    invoc = ('PyRun_SimpleFile(%s, %s)' % (str(file_ptr), json.dumps(filepath)))
    self._Inject(position, invoc)
    self.Call(position, 'fclose(%s)' % str(file_ptr))

  def InjectString(self, position, code):
    invoc = 'PyRun_SimpleString(%s)' % json.dumps(code)
    self._Inject(position, invoc)

  def _Inject(self, position, call):
    """Injects evaluation of 'call' in a safe location in the inferior.

    Due to the way these injected function calls work, gdb must not be killed
    until the call has returned. If that happens, the inferior will be sent
    SIGTRAP upon attempting to return from the dummy frame gdb constructs for
    us, and will most probably crash.
    Args:
      position: array of pid, tid, framedepth specifying the requested position.
      call: Any expression gdb can evaluate. Usually a function call.
    Raises:
      RuntimeError: if gdb is not being run in synchronous exec mode.
    """
    self.EnsureGdbPosition(position[0], position[1], None)
    self.ClearBreakpoints()
    self._AddThreadSpecificBreakpoint(position)
    gdb.parse_and_eval('%s = 1' % GdbCache.PENDINGCALLS_TO_DO)
    gdb.parse_and_eval('%s = 1' % GdbCache.PENDINGBUSY)
    try:
      # We're "armed", risk the blocking call to Continue
      self.Continue(position)
      # Breakpoint was hit!
      if not gdb.selected_thread().is_stopped():
        # This should not happen. Depending on how gdb is being used, the
        # semantics of self.Continue change, so I'd rather leave this check in
        # here, in case we ever *do* end up changing to async mode.
        raise RuntimeError('Gdb is not acting as expected, is it being run in '
                           'async mode?')
    finally:
      gdb.parse_and_eval('%s = 0' % GdbCache.PENDINGBUSY)
    self.Call(position, call)

  def _AddThreadSpecificBreakpoint(self, position):
    self.EnsureGdbPosition(position[0], None, None)
    tid_map = self._GetGdbThreadMapping(position)
    gdb_threadnum = tid_map[position[1]]
    # Since not all versions of gdb's python API support support creation of
    # temporary breakpoint via the API, we're back to exec'ing CLI commands
    gdb.execute('tbreak Py_MakePendingCalls thread %s' % gdb_threadnum)

  def _BacktraceFromFramePtr(self, frame_ptr):
    """Assembles and returns what looks exactly like python's backtraces."""
    # expects frame_ptr to be a gdb.Value
    frame_objs = [PyFrameObjectPtr(frame) for frame
                  in self._IterateChainedList(frame_ptr, 'f_back')]

    # We want to output tracebacks in the same format python uses, so we have to
    # reverse the stack
    frame_objs.reverse()
    tb_strings = ['Traceback (most recent call last):']
    for frame in frame_objs:
      line_string = ('  File "%s", line %s, in %s' %
                     (frame.filename(),
                      str(frame.current_line_num()),
                      frame.co_name.proxyval(set())))
      tb_strings.append(line_string)
      line_string = '    %s' % frame.current_line().strip()
      tb_strings.append(line_string)
    return '\n'.join(tb_strings)

  def StackDepth(self, position):
    self.EnsureGdbPosition(position[0], position[1], None)
    stack_head = self.selected_tstate['frame']
    return len(list(self._IterateChainedList(stack_head, 'f_back')))

  def BacktraceAt(self, position):
    self.EnsureGdbPosition(*position)
    return self._BacktraceFromFramePtr(self.selected_frame)

  def LookupInFrame(self, position, var_name):
    self.EnsureGdbPosition(*position)
    frame = PyFrameObjectPtr(self.selected_frame)
    value = frame.get_var_by_name(var_name)[0]
    return value

  def _CreateProxyValFromIterator(self, iterator):
    result_dict = {}
    for key, value in iterator():
      native_key = key.proxyval(set()) if key else key
      result_dict[native_key] = value
    return result_dict

  def InferiorLocals(self, position):
    self.EnsureGdbPosition(*position)
    frame = PyFrameObjectPtr(self.selected_frame)
    return self._CreateProxyValFromIterator(frame.iter_locals)

  def InferiorGlobals(self, position):
    self.EnsureGdbPosition(*position)
    frame = PyFrameObjectPtr(self.selected_frame)
    return self._CreateProxyValFromIterator(frame.iter_globals)

  def InferiorBuiltins(self, position):
    self.EnsureGdbPosition(*position)
    frame = PyFrameObjectPtr(self.selected_frame)
    return self._CreateProxyValFromIterator(frame.iter_builtins)


if __name__ == '__main__':

  UNBUF_STDIN = open('/dev/stdin', 'r', buffering=1)
  UNBUF_STDOUT = open('/dev/stdout', 'w', buffering=1)
  UNBUF_STDERR = open('/dev/stderr', 'w', buffering=1)

  def Excepthook(exc_type, value, trace):
    exc_string = ''.join(traceback.format_tb(trace))
    exc_string += '%s: %s' % (exc_type.__name__, value)
    UNBUF_STDERR.write(json.dumps(exc_string) + '\n')

  sys.excepthook = Excepthook
  serv = GdbService(UNBUF_STDIN, UNBUF_STDOUT, UNBUF_STDERR)
  serv.EvalLoop()
