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
"""Python code injection into helper thread."""


import json
import logging
import os
import socket
import read_only


class SentinelInjectPlugin(read_only.ReadonlyPlugin):
  """Python code injection into helper (sentinel) thread.

  Note that while the command interface of this mode is the same as that of
  InjectPlugin, the `inject` and `pdb` commands are always executed in the
  context of the helper thread.
  """

  def __init__(self, inferior, name='sent'):
    super(SentinelInjectPlugin, self).__init__(inferior, name)

  @property
  def commands(self):
    return (super(SentinelInjectPlugin, self).commands +
            [('execsocks', self.ThreadsWithRunningExecServers),
             ('send', self.SendToExecSocket),
             ('closesock', self.CloseExecSocket),
             ('pdb', self.InjectPdb),
            ])

  def ThreadsWithRunningExecServers(self):
    """Returns a list of tids of inferior threads with open exec servers."""
    socket_dir = '/tmp/pyringe_%s' % self.inferior.pid
    if os.path.isdir(socket_dir):
      return [int(fname[:-9])
              for fname in os.listdir(socket_dir)
              if fname.endswith('.execsock')]
    return []

  def SendToExecSocket(self, code, tid=None):
    """Inject python code into exec socket."""
    response = self._SendToExecSocketRaw(json.dumps(code), tid)
    return json.loads(response)

  def _SendToExecSocketRaw(self, string, tid=None):
    if not tid:
      tid = self.inferior.current_thread
    socket_dir = '/tmp/pyringe_%s' % self.inferior.pid
    if tid not in self.ThreadsWithRunningExecServers():
      logging.error('Couldn\'t find socket for thread ' + str(tid))
      return
    # We have to make sure the inferior can process the request
    # TODO: replace this gdb kill with a call to the continue command when we
    # add async command execution
    self.inferior.ShutDownGdb()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect('%s/%s.execsock' % (socket_dir, tid))
    sock.sendall(string)
    response = sock.recv(1024)
    sock.shutdown(socket.SHUT_RDWR)
    sock.close()
    return response

  def CloseExecSocket(self, tid=None):
    """Send closing request to exec socket."""
    response = self._SendToExecSocketRaw('__kill__', tid)
    if response != '__kill_ack__':
      logging.warning('May not have succeeded in closing socket, make sure '
                      'using execsocks().')

  def InjectPdb(self):
    """Start pdb in the context of the helper thread."""
    raise NotImplementedError
