"""Build tree_sitter_jac.binding from the sibling tree-sitter-jac grammar repo.

Compiles the grammar's committed src/parser.c + src/scanner.c plus a local
PyCapsule binding shim — no changes to the grammar repo itself.
"""
from pathlib import Path

from setuptools import Extension, setup

HERE = Path(__file__).parent
GRAMMAR = HERE.parent.parent / "tree-sitter-jac" / "src"

setup(
    name="tree-sitter-jac-local",
    version="0.1.0",
    description="Local Python binding for the tree-sitter-jac grammar",
    packages=["tree_sitter_jac"],
    package_data={"tree_sitter_jac": ["py.typed"]},
    ext_modules=[
        Extension(
            "tree_sitter_jac.binding",
            sources=[
                str(HERE / "binding.c"),
                str(GRAMMAR / "parser.c"),
                str(GRAMMAR / "scanner.c"),
            ],
            include_dirs=[str(GRAMMAR / "tree_sitter")],
            define_macros=[
                ("PY_SSIZE_T_CLEAN", None),
                ("TREE_SITTER_HIDE_SYMBOLS", None),
            ],
            extra_compile_args=["-std=c11", "-O2"],
        )
    ],
)
