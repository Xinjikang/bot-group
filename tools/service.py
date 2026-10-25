"""Start or stop this installation's engine."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.maintenance.service import main

if __name__ == '__main__':
    sys.exit(main())
