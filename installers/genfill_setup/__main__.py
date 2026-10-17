"""Entry point: python -m genfill_setup (from installers/), or the built app."""
import sys

from genfill_setup.app import main

if __name__ == "__main__":
    sys.exit(main())
