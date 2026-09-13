"""`python -m hacoo` -> CLI 入口 (见 hacoo/cli.py)。"""
import sys

from hacoo.cli import main

if __name__ == "__main__":
    sys.exit(main())
