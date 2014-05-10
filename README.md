**DISCLAIMER: This is not an official google project, this is just something I wrote while at Google.**

Pyringe
=======

What this is
------------

Pyringe is a python debugger capable of attaching to running processes,  inspecting their state and even of injecting python code into them while they're running. With pyringe, you can list threads, get tracebacks, inspect locals/globals/builtins of running functions, all without having to prepare your program for it.

What this is not
----------------

A "Google project". It's my internship project that got open-sourced. Sorry for the confusion.

What do I need?
---------------

Pyringe internally uses gdb to do a lot of its heavy lifting, so you will need a fairly recent build of gdb (version 7.4 onwards, and only if gdb was configured with `--with-python`). You will also need the symbols for whatever build of python you're running.  
On Fedora, the package you're looking for is `python-debuginfo`, on Debian it's called `python2.7-dbg` (adjust according to version). Arch Linux users: see [issue #5][], Ubuntu users can only debug the `python-dbg` binary (see [issue #19][]).  
Having [Colorama](https://pypi.python.org/pypi/colorama) will get you output in boldface, but it's optional.

[issue #5]: https://github.com/google/pyringe/issues/5
[issue #19]: https://github.com/google/pyringe/issues/19

How do I get it?
----------------

Get it from the [Github repo][], [PyPI][], or via pip (`pip install pyringe`).

[Github repo]: https://github.com/google/pyringe
[PyPI]: https://pypi.python.org/pypi/pyringe

Is this Python3-friendly?
-------------------------

Short answer: **No, sorry.** Long answer:  
There's three potentially different versions of python in play here:  
1. The version running pyringe  
2. The version being debugged  
3. The version of `libpythonXX.so` your build of gdb was linked against  

`2` Is currently the dealbreaker here. Cpython has changed a bit in the meantime[1], and making all features work while debugging python3 will have to take a back seat for now until the more glaring issues have been taken care of.    
As for `1` and `3`, the `2to3` tool may be able to handle it automatically. But then, as long as `2` hasn't been taken care of, this isn't really a use case in the first place.

[1] - For example, `pendingbusy` (which is used for injection) has been renamed to `busy` and been given a function-local scope, making it harder to interact with via gdb.

Will this work with PyPy?
-------------------------

Unfortunately, no. Since this makes use of some CPython internals and implementation details, only CPython is supported. If you don't know what PyPy or CPython are, you'll probably be fine.

Why not PDB?
------------

PDB is great. Use it where applicable! But sometimes it isn't.  
Like when python itself crashes, gets stuck in some C extension, or you want to inspect data without stopping a program. In such cases, PDB (and all other debuggers that run within the interpreter itself) are next to useless, and without pyringe you'd be left with having to debug using `print` statements. Pyringe is just quite convenient in these cases.


I injected a change to a local var into a function and it's not showing up!
---------------------------------------------------------------------------

This is a known limitation. Things like `inject('var = 2')` won't work, but `inject('var[1] = 1337')` should. This is because most of the time, python internally uses a fast path for looking up local variables that doesn't actually perform the dictionary lookup in `locals()`. In general, code you inject into processes with pyringe is very different from a normal python function call.

How do I use it?
----------------

You can start the debugger by executing `python -m pyringe`. Alternatively:


```python
import pyringe
pyringe.interact()
```

If that reminds you of the code module, good; this is intentional.  
After starting the debugger, you'll be greeted by what behaves almost like a regular python REPL.  
Try the following:


```python
==> pid:[None] #threads:[0] current thread:[None]
>>> help()
Available commands:
 attach: Attach to the process with the given pid.
 bt: Get a backtrace of the current position.
 [...]
==> pid:[None] #threads:[0] current thread:[None]
>>> attach(12679)
==> pid:[12679] #threads:[11] current thread:[140108099462912]
>>> threads()
[140108099462912, 140108107855616, 140108116248323, 140108124641024, 140108133033728, 140108224739072, 140108233131776, 140108141426432, 140108241524480, 140108249917184, 140108269324032]
```

The IDs you see here correspond to what `threading.current_thread().ident` would tell you.  
All debugger functions are just regular python functions that have been exposed to the REPL, so you can do things like the following.

```python
==> pid:[12679] #threads:[11] current thread:[140108099462912]
>>> for tid in threads():
...   if not tid % 10:
...     thread(tid)
...     bt()
... 
Traceback (most recent call last):
  File "/usr/lib/python2.7/threading.py", line 524, in __bootstrap
    self.__bootstrap_inner()
  File "/usr/lib/python2.7/threading.py", line 551, in __bootstrap_inner
    self.run()
  File "/usr/lib/python2.7/threading.py", line 504, in run
    self.__target(*self.__args, **self.__kwargs)
  File "./test.py", line 46, in Idle
    Thread_2_Func(1)
  File "./test.py", line 40, in Wait
    time.sleep(n)
==> pid:[12679] #threads:[11] current thread:[140108241524480]
>>> 
```

You can access the inferior's locals and inspect them like so:

```python
==> pid:[12679] #threads:[11] current thread:[140108241524480]
>>> inflocals()
{'a': <proxy of A object at remote 0x1d9b290>, 'LOL': 'success!', 'b': <proxy of B object at remote 0x1d988c0>, 'n': 1}
==> pid:[12679] #threads:[11] current thread:[140108241524480]
>>> p('a')
<proxy of A object at remote 0x1d9b290>
==> pid:[12679] #threads:[11] current thread:[140108241524480]
>>> p('a').attr
'Some_magic_string'
==> pid:[12679] #threads:[11] current thread:[140108241524480]
>>> 
```

And sure enough, the definition of `a`'s class reads:

```python
class Example(object):
  cl_attr = False
  def __init__(self):
    self.attr = 'Some_magic_string'
```

There's limits to how far this proxying of objects goes, and everything that isn't trivial data will show up as strings (like `'<function at remote 0x1d957d0>'`).  
You can inject python code into running programs. Of course, there are caveats but... see for yourself:

```python
==> pid:[12679] #threads:[11] current thread:[140108241524480]
>>> inject('import threading')
==> pid:[12679] #threads:[11] current thread:[140108241524480]
>>> inject('print threading.current_thread().ident')
==> pid:[12679] #threads:[11] current thread:[140108241524480]
>>> 
```

The output of my program in this case reads:

```
140108241524480
```

If you need additional pointers, just try using python's help (`pyhelp()` in the debugger) on debugger commands.
