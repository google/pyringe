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

from distutils.core import setup
import pyringe

setup(
    name='pyringe',
    author='Max Wagner (Google Inc.)',
    maintainer='Google',
    maintainer_email='pyringe-dev@googlegroups.com',
    version=pyringe.__version__,
    url='https://github.com/google/pyringe',
    license='Apache License, Version 2.0',
    description='Python debugger capable of attaching to processes',
    long_description=open('README.md').read(),
    packages=['pyringe', 'pyringe.plugins', 'pyringe.payload'],
    classifiers=[
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Programming Language :: Python',
        'License :: OSI Approved :: Apache Software License',
        'Topic :: Software Development :: Debuggers',
    ],
)
