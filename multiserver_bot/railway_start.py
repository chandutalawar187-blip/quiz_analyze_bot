"""Launch the application from its src-layout when deployed on Railway."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from quizcord_bot.__main__ import main


if __name__ == "__main__":
    main()
