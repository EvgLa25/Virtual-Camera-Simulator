"""Entry point for VCamSimSvc.exe -- the Windows service host.

  VCamSimSvc.exe install     register the service (needs Administrator)
  VCamSimSvc.exe start|stop
  VCamSimSvc.exe remove
  VCamSimSvc.exe debug       run it in this console instead of as a service

With no arguments it is being launched by the Service Control Manager.
"""
import sys

from vcamsim.service import main

if __name__ == "__main__":
    sys.exit(main())
