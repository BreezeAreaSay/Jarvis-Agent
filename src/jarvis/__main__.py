"""`python -m jarvis …` — то же, что команда `jarvis …`."""

import sys

from jarvis.cli import main

if __name__ == "__main__":
    sys.exit(main())
