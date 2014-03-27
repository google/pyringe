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
"""Python code injection into arbitrary threads."""


import logging
import sys
import traceback

import inject_sentinel


class InjectPlugin(inject_sentinel.SentinelInjectPlugin):
  """Python code injection into arbitrary threads."""

  def __init__(self, inferior, name='inj'):
    super(InjectPlugin, self).__init__(inferior, name)

  @property
  def commands(self):
    return (super(InjectPlugin, self).commands +
            [('inject', self.InjectString),
             ('injectsentinel', self.InjectSentinel),
             ('_pdb', self.InjectPdb),
            ])

  def InjectString(self, codestring, wait_for_completion=True):
    """Try to inject python code into current thread.

    Args:
      codestring: Python snippet to execute in inferior. (may contain newlines)
      wait_for_completion: Block until execution of snippet has completed.
    """
    if self.inferior.is_running and self.inferior.gdb.IsAttached():
      try:
        self.inferior.gdb.InjectString(
            self.inferior.position,
            codestring,
            wait_for_completion=wait_for_completion)
      except RuntimeError:
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_exception(exc_type, exc_value, exc_traceback)
    else:
      logging.error('Not attached to any process.')

  def InjectSentinel(self):
    """Try to inject code that starts the code injection helper thread."""
    raise NotImplementedError

  def InjectPdb(self):
    """Try to inject a pdb shell into the current thread."""
    raise NotImplementedError
