import sys

# The mutation audit rewrites inventory.py between pytest runs, sometimes
# twice within the same second. CPython validates .pyc files by whole-second
# mtime + size, so same-size rewrites can silently reuse stale bytecode and
# test the wrong mutant. Never write bytecode for this fixture.
sys.dont_write_bytecode = True
