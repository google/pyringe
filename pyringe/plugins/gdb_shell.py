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
"""Plugin which drops the user to a raw gdb prompt."""


import os

from pyringe.plugins import mod_base


class GdbPlugin(mod_base.DebuggingPlugin):
  """Plugin which can drop the user to a raw gdb prompt."""

  gdb_args = []

  def __init__(self, inferior, name='gdb'):
    super(GdbPlugin, self).__init__(inferior, name)

  @property
  def commands(self):
    return (super(GdbPlugin, self).commands +
            [('gdb', self.StartGdb),
             ('setgdbargs', self.SetGdbArgs)])

  def StartGdb(self):
    """Hands control over to a new gdb process."""
    if self.inferior.is_running:
      self.inferior.ShutDownGdb()
      program_arg = 'program %d ' % self.inferior.pid
    else:
      program_arg = ''
    os.system('gdb ' + program_arg + ' '.join(self.gdb_args))
    reset_position = raw_input('Reset debugger position? [y]/n ')
    if not reset_position or reset_position == 'y' or reset_position == 'yes':
      self.position = None

  def SetGdbArgs(self, newargs):
    """Set additional custom arguments for Gdb."""
    self.gdb_args = newargs

