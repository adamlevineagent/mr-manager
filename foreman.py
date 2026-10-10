import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from foreman.cli import main
main()
