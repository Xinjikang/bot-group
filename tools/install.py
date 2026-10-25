"""Install, validate or repair this source installation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.maintenance.install import main

if __name__ == '__main__':
    sys.exit(main())
