"""Start the engine in a detached process."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.maintenance.start_engine import main

if __name__ == '__main__':
    main()
