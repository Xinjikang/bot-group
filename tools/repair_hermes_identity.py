"""Preview or apply legacy Hermes identity repairs."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.maintenance.repair_hermes_identity import main

if __name__ == '__main__':
    main()
