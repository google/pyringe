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
"""Listens on a socket in /tmp and execs what it reads from it."""

import json
import os
import socket
import threading


def StartExecServer():
  """Opens a socket in /tmp, execs data from it and writes results back."""
  sockdir = '/tmp/pyringe_%s' % os.getpid()
  if not os.path.isdir(sockdir):
    os.mkdir(sockdir)
  socket_path = ('%s/%s.execsock' %
                 (sockdir, threading.current_thread().ident))

  if os.path.exists(socket_path):
    os.remove(socket_path)

  exec_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  exec_sock.bind(socket_path)
  exec_sock.listen(5)
  shutdown = False
  while not shutdown:
    conn, _ = exec_sock.accept()

    data = conn.recv(1024)
    if data:
      if data == '__kill__':
        shutdown = True
        conn.send('__kill_ack__')
        break
      data = json.loads(data)
      try:
        conn.sendall(json.dumps(eval(data)))
      except SyntaxError:
        # Okay, so it probably wasn't an expression
        try:
          exec data  # pylint: disable=exec-used
        except:  # pylint: disable=bare-except
          # Whatever goes wrong when exec'ing this, we don't want to crash.
          # TODO: think of a way to properly tunnel exceptions, if
          # possible without introducing more magic strings.
          pass
        finally:
          conn.sendall(json.dumps(None))
  exec_sock.shutdown(socket.SHUT_RDWR)
  exec_sock.close()
  os.remove(socket_path)

if __name__ == '__main__':
  StartExecServer()
