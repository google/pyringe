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
"""Extensible remote python debugger.

Upon running, presents the user with an augmented python REPL able to load
plugins providing more advanced debugging capabilities.
"""

import code
import logging
import readline
import rlcompleter  # pylint: disable=unused-import
import sys
import inferior
from plugins import inject


# Optionally support colorama
try:
  import colorama  # pylint: disable=g-import-not-at-top
except ImportError:

  # mock the whole thing
  class EmptyStringStruct(object):

    def __getattr__(self, name):
      return ''

  class colorama(object):
    _mock = EmptyStringStruct()
    Fore = _mock
    Back = _mock
    Style = _mock

    @staticmethod
    def init():
      pass


_WELCOME_MSG = ('For a list of debugger commands, try "help()". '
                '(python\'s help is available as pyhelp.)')


class Error(Exception):
  """Base error class for this project."""
  pass


class DebuggingConsole(code.InteractiveConsole):
  """Provides a python REPL augmented with debugging capabilities.

  Attributes:
    commands: A dictionary containing the debugger's base commands.
    plugins: A list of currently loaded plugins
    inferior: The pid of the inferior process
  """

  def __init__(self):
    self.inferior = inferior.Inferior(None)
    self.commands = {'help': self.ListCommands,
                     'pyhelp': help,  # we shouldn't completely hide this
                     'attach': self.Attach,
                     'detach': self.Detach,
                     'setarch': self.SetArchitecture,
                     'setloglevel': self.SetLogLevel,
                     'loadplugin': self.LoadCommandPlugin,
                     'quit': self.Quit,
                    }
    self.plugins = [inject.InjectPlugin(self.inferior)]
    readline.parse_and_bind('tab: complete')
    colorama.init()

    locals_dir = dict([
        # This being a debugger, we trust the user knows what she's
        # doing when she messes with this key.
        ('__repl__', self),
        ('__doc__', __doc__),
        ('__name__', '__pyringe__')
    ])
    locals_dir.update(self.commands)
    code.InteractiveConsole.__init__(self)
    self.locals = locals_dir
    self.LoadCommandPlugin(inject.InjectPlugin(self.inferior))

  def LoadCommandPlugin(self, plugin):
    """Load a command plugin."""
    self.locals.update(plugin.commands)

  def ListCommands(self):
    """Print a list of currently available commands and their descriptions."""
    print 'Available commands:'
    commands = dict(self.commands)
    for plugin in self.plugins:
      commands.update(plugin.commands)
    for com in sorted(commands):
      if not com.startswith('_'):
        self.PrintHelpTextLine(com, commands[com])

  def PrintHelpTextLine(self, title, obj):
    if obj.__doc__:
      # only print the first line of the object's docstring
      docstring = obj.__doc__.splitlines()[0]
    else:
      docstring = 'No description available.'
    print ' %s%s%s: %s' % (colorama.Style.BRIGHT, title,
                           colorama.Style.RESET_ALL, docstring)

  def StatusLine(self):
    """Generate the colored line indicating plugin status."""
    pid = self.inferior.pid
    curthread = None
    threadnum = 0
    if pid:
      if not self.inferior.is_running:
        logging.warning('Inferior is not running.')
        self.Detach()
        pid = None
      else:
        try:
          # get a gdb running if it wasn't already.
          if not self.inferior.attached:
            self.inferior.StartGdb()
          curthread = self.inferior.current_thread
          threadnum = len(self.inferior.threads)
        except (inferior.ProxyError,
                inferior.TimeoutError,
                inferior.PositionError) as err:
          # This is not the kind of thing we want to be held up by
          logging.debug('Error while getting information in status line:%s'
                        % err.message)
          pass
    status = ('==> pid:[%s] #threads:[%s] current thread:[%s]' %
              (pid, threadnum, curthread))
    return status

  def Attach(self, pid):
    """Attach to the process with the given pid."""
    if self.inferior.is_running:
      answer = raw_input('Already attached to process ' +
                         str(self.inferior.pid) +
                         '. Detach? [y]/n ')
      if answer and answer != 'y' and answer != 'yes':
        return None
      self.Detach()
    # Whatever position we had before will not make any sense now
    for plugin in self.plugins:
      plugin.position = None
    self.inferior.Reinit(pid)

  def Detach(self):
    """Detach from the inferior (Will exit current mode)."""
    for plugin in self.plugins:
      plugin.position = None
    self.inferior.Reinit(None)

  def SetArchitecture(self, arch):
    """Set inferior target architecture

    This is directly forwarded to gdb via its command line, so
    possible values are defined by what the installed gdb supports.
    Only takes effect after gdb has been restarted.

    Args:
      arch: The target architecture to set gdb to.
    """
    self.inferior.arch = arch

  def SetLogLevel(self, level):
    """Set log level. Corresponds to levels from logging module."""
    # This is mostly here to jog people into enabling logging without
    # requiring them to have looked at the pyringe code.
    return logging.getLogger().setLevel(level)

  def runcode(self, co):
    try:
      exec co in self.locals  # pylint: disable=exec-used
    except SystemExit:
      self.inferior.Cancel()
      raise
    except KeyboardInterrupt:
      raise
    except inferior.PositionError as err:
      print 'PositionError: %s' % err.message
    except:
      self.showtraceback()
    else:
      if code.softspace(sys.stdout, 0):
        print

  def Quit(self):
    """Raises SystemExit, thereby quitting the debugger."""
    raise SystemExit

  def interact(self, banner=None):
    """Closely emulate the interactive Python console.

    This method overwrites its superclass' method to specify a different help
    text and to enable proper handling of the debugger status line.

    Args:
      banner: Text to be displayed on interpreter startup.
    """
    sys.ps1 = getattr(sys, 'ps1', '>>> ')
    sys.ps2 = getattr(sys, 'ps2', '... ')
    if banner is None:
      print ('Pyringe (Python %s.%s.%s) on %s\n%s' %
             (sys.version_info.major, sys.version_info.minor,
              sys.version_info.micro, sys.platform, _WELCOME_MSG))
    else:
      print banner
    more = False
    while True:
      try:
        if more:
          prompt = sys.ps2
        else:
          prompt = self.StatusLine() + '\n' + sys.ps1
        try:
          line = self.raw_input(prompt)
        except EOFError:
          print ''
          break
        else:
          more = self.push(line)
      except KeyboardInterrupt:
        print '\nKeyboardInterrupt'
        self.resetbuffer()
        more = False


