"""Execute an application upgrade receipt in a separate process."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.maintenance.update_engine import run

if __name__ == '__main__':
    sys.exit(run(sys.argv[1]))
