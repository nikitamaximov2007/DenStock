# No sudoers policy

The launcher is activated through a root-owned systemd socket. DenisStock does
not receive a sudo rule, and this deployment layer must not install one.
