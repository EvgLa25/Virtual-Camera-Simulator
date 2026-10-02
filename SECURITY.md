# Security

VCamSim is intended for trusted lab networks and VMS testing. It is not a
hardened camera appliance and does not provide TLS for HTTP, RTSP or its
loopback control channel. Default credentials are deliberately simple lab
defaults; change them before making cameras reachable from other machines.

Keep configuration, the local control token, real videos and logs out of
public issues and repositories. RTSP URLs may embed credentials when that
compatibility option is enabled. Use synthetic video when sharing a report.

The Windows service may run with elevated privileges. Restrict write access to
its installation, configuration and token to trusted users; do not deploy it
on a shared or untrusted host using a world-writable application directory.
Do not expose the control channel beyond loopback.

Report a vulnerability through GitHub's private vulnerability reporting if
enabled for the repository. Do not publish usable secrets or exploit details
in a public issue while a report is being assessed.
