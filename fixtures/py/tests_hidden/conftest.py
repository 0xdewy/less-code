import sys
from pathlib import Path

# hidden tests live outside the package dir; make the fixture importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.dont_write_bytecode = True
