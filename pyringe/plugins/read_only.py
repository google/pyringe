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
"""Read-only python thread inspection mode."""

import logging

import gdb_shell


class ReadonlyPlugin(gdb_shell.GdbPlugin):
  """Read-only inspection of inferior.

  This class doesn't do much more than wrap the functionality from within GDB
  and hide irrelevant implementation details.
  """

  def __init__(self, inferior, name='ro'):
    super(ReadonlyPlugin, self).__init__(inferior, name)

  @property
  def commands(self):
    return (super(ReadonlyPlugin, self).commands +
            [('bt', self.Backtrace),
             ('up', self.Up),
             ('down', self.Down),
             ('inflocals', self.InferiorLocals),
             ('infglobals', self.InferiorGlobals),
             ('infbuiltins', self.InferiorBuiltins),
             ('p', self.Lookup),
             ('threads', self.ListThreads),
             ('current_thread', self.SelectedThread),
             ('thread', self.SelectThread),
             ('c', self.Cancel),
             ('_cont', self.Continue),
             ('_interrupt', self.InterruptInferior),
             ('setsymbols', self.LoadSymbolFile),
            ])

  def Backtrace(self, to_string=False):
    """Get a backtrace of the current position."""
    if self.inferior.is_running:
      res = self.inferior.Backtrace()
      if to_string:
        return res
      print res
    else:
      logging.error('Not attached to any process.')

  def Up(self):
    """Move one frame up in the call stack."""
    return self.inferior.Up()

  def Down(self):
    """Move one frame down in the call stack."""
    return self.inferior.Down()

  def InferiorLocals(self):
    """Print the inferior's local identifiers in the current context."""
    return self.inferior.InferiorLocals()

  def InferiorGlobals(self):
    """Print the inferior's global identifiers in the current context."""
    return self.inferior.InferiorGlobals()

  def InferiorBuiltins(self):
    """Print the inferior's builtins in the current context."""
    return self.inferior.InferiorBuiltins()

  def Lookup(self, var_name):
    """Look up a value in the current context."""
    return self.inferior.Lookup(var_name)

  def ListThreads(self):
    """List the currently running python threads.

    Returns:
      A list of the inferior's thread idents, or None if the debugger is not
      attached to any process.
    """
    if self.inferior.is_running:
      return self.inferior.threads
    logging.error('Not attached to any process.')
    return []

  def SelectThread(self, tid):
    """Select a thread by ID."""
    return self.inferior.SelectThread(tid)

  def SelectedThread(self):
    """Returns the ID of the currently selected thread.

    Note that this has no correlation with the thread that the inferior is
    currently executing, but rather what the debugger considers to be the
    current command context.

    Returns:
      The ID of the currenly selected thread.
    """
    if self.inferior.is_running:
      return self.inferior.current_thread

  def Cancel(self):
    """Cancel a running command that has timeouted."""
    return self.inferior.Cancel()

  def Continue(self):
    """Continue execution of the inferior."""
    return self.inferior.Continue()

  def InterruptInferior(self):
    """Interrupt execution of the inferior."""
    return self.inferior.Interrupt()

  def LoadSymbolFile(self, path):
    """Attempt to load new symbol file from given path."""
    return self.inferior.LoadSymbolFile(path)
