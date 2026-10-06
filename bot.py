"""Belépési pont: `python bot.py`"""

import sys

if sys.version_info < (3, 12):
    sys.exit("Python 3.12 vagy újabb szükséges (a 3.13 is jó).")

from mircewok.app import main

if __name__ == "__main__":
    raise SystemExit(main())
