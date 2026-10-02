"""GUI entry point.

  VCamSim.exe                      use config.yaml next to the exe
  VCamSim.exe other.yaml           use a specific config
  VCamSim.exe --autostart          start all cameras immediately
"""
import sys

from vcamsim.config import resolve_path
from vcamsim.gui import main

if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    autostart = "--autostart" in sys.argv
    cfg = args[0] if args else str(resolve_path("config.yaml"))
    main(cfg, autostart)
