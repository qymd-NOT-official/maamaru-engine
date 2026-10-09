"""Create the app-only update asset and full installed-file manifest."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launcher.light_update import build_package

if __name__ == "__main__":
    print(build_package(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
