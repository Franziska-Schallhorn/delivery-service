import os

import setuptools

import setup


own_dir = os.path.abspath(os.path.dirname(__file__))


def requirements():
    yield 'delivery-gear-utils'
    yield 'dacite'
    yield 'urllib3'
    yield 'PyYAML'
    yield 'kubernetes'


setuptools.setup(
    name='odg-operator',
    version=setup.finalize_version(),
    py_modules=[],
    packages=['odg_operator'],
    install_requires=list(requirements()),
)
