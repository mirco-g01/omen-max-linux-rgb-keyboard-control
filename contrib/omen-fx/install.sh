#!/usr/bin/env bash
# omen-fx installer.
#
#   sudo ./install.sh              install daemon + CLI + config, enable services
#   sudo ./install.sh pam-enable   light the bar on sudo/polkit password prompts
#   sudo ./install.sh pam-disable  undo the above
#   sudo ./install.sh uninstall    remove everything
#
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIBDIR=/usr/local/lib/omen-fx
BINDIR=/usr/local/bin
CONFDIR=/etc/omen-fx
DOCDIR=/usr/local/share/doc/omen-fx
UNITDIR=/etc/systemd/system
USERUNITDIR=/etc/systemd/user
PAM_FILES=(/etc/pam.d/sudo)

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*"; }

need_root() { [[ $EUID -eq 0 ]] || die "run this with sudo"; }

real_user() { echo "${SUDO_USER:-$(logname 2>/dev/null || echo "")}"; }

do_install() {
  need_root
  command -v python3 >/dev/null || die "python3 is required"
  python3 -c 'import tomllib' 2>/dev/null || die "python3 3.11+ is required (tomllib)"

  info "installing to $LIBDIR"
  install -d "$LIBDIR" "$LIBDIR/omen_fx" "$CONFDIR" "$DOCDIR" /var/lib/omen-fx
  install -m 0644 "$SRC"/omen_fx/*.py "$LIBDIR/omen_fx/"
  install -m 0755 "$SRC"/omen-fx "$SRC"/omen-fxd "$SRC"/omen-fx-notifyd "$LIBDIR/"
  install -m 0755 "$SRC"/omen-fx-brightness "$SRC"/omen-fx-brightnessd "$LIBDIR/"
  install -m 0755 "$SRC"/omen-fx-gui "$LIBDIR/"
  install -m 0755 "$SRC"/omen-fx-pam "$SRC"/pam-hook.py "$LIBDIR/"
  install -m 0644 "$SRC"/README.md "$DOCDIR/" 2>/dev/null || true

  ln -sf "$LIBDIR/omen-fx" "$BINDIR/omen-fx"
  ln -sf "$LIBDIR/omen-fx-brightness" "$BINDIR/omen-fx-brightness"
  ln -sf "$LIBDIR/omen-fx-brightnessd" "$BINDIR/omen-fx-brightnessd"
  if python3 -c "import PyQt6" 2>/dev/null; then
    ln -sf "$LIBDIR/omen-fx-gui" "$BINDIR/omen-fx-gui"
    install -d /usr/local/share/applications
    install -m 0644 "$SRC/omen-fx-gui.desktop" /usr/local/share/applications/
    update-desktop-database /usr/local/share/applications 2>/dev/null || true
  else
    echo "    (PyQt6 missing: GUI not installed -- pacman -S python-pyqt6,"
    echo "     apt install python3-pyqt6, or dnf install python3-pyqt6)"
  fi

  if [[ -f "$CONFDIR/config.toml" ]]; then
    install -m 0644 "$SRC/config.toml" "$CONFDIR/config.toml.new"
    if cmp -s "$CONFDIR/config.toml" "$CONFDIR/config.toml.new"; then
      rm -f "$CONFDIR/config.toml.new"
    else
      NEW_CONFIG=1
      info "keeping your $CONFDIR/config.toml -- the shipped one changed"
    fi
  else
    install -m 0644 "$SRC/config.toml" "$CONFDIR/config.toml"
  fi

  # The daemon runs as root and could open hidraw directly, but the rule lets
  # the CLI and the GUI drive the keyboard as an ordinary user too. It hands
  # out only interface 4 -- the other four hidraw nodes of the keyboard carry
  # keystrokes and must stay root-only.
  info "installing the udev rules (LampArray, and the backlight LED)"
  install -m 0644 "$SRC/99-omen-lamparray.rules" /etc/udev/rules.d/
  udevadm control --reload-rules || true
  udevadm trigger --subsystem-match=hidraw || true
  # The LED already exists by now, and a rule only runs on a device event, so
  # without this the permissions rule would not take effect until a reboot.
  udevadm trigger --subsystem-match=leds || true

  info "installing systemd units"
  install -m 0644 "$SRC/systemd/omen-fxd.service" "$UNITDIR/"
  install -d "$USERUNITDIR"
  install -m 0644 "$SRC/systemd/omen-fx-notifyd.service" "$USERUNITDIR/"
  install -m 0644 "$SRC/systemd/omen-fx-brightnessd.service" "$USERUNITDIR/"
  install -m 0644 "$SRC/systemd/omen-fx.tmpfiles.conf" /etc/tmpfiles.d/omen-fx.conf
  systemd-tmpfiles --create /etc/tmpfiles.d/omen-fx.conf || true

  systemctl daemon-reload
  systemctl enable omen-fxd.service
  # restart, not "enable --now": on a reinstall the service is already active
  # and would keep running the previous code.
  systemctl restart omen-fxd.service
  info "omen-fxd: $(systemctl is-active omen-fxd.service) (restarted with the new code)"

  local user; user="$(real_user)"
  if [[ -n "$user" ]]; then
    info "enabling the notification watcher for $user"
    runuser -u "$user" -- systemctl --user daemon-reload || true
    runuser -u "$user" -- systemctl --user enable omen-fx-notifyd.service 2>/dev/null || true
    runuser -u "$user" -- systemctl --user restart omen-fx-notifyd.service || \
      echo "    (could not reach the user session; run 'systemctl --user enable --now omen-fx-notifyd' yourself)"
    # Not enabled by default: it changes what the brightness keys do, which is
    # the user's call. The GUI's Sistema tab is the switch, and this line is
    # only here so a reinstall picks up new code for someone who did turn it on.
    if runuser -u "$user" -- systemctl --user is-enabled omen-fx-brightnessd.service >/dev/null 2>&1; then
      runuser -u "$user" -- systemctl --user restart omen-fx-brightnessd.service || true
      info "brightness keys: on for $user (restarted with the new code)"
    else
      info "brightness keys: off -- turn them on in omen-fx-gui, tab Sistema"
    fi
    id -nG "$user" | tr ' ' '\n' | grep -qx input || \
      echo "    NOTE: $user is not in the 'input' group; run: sudo usermod -aG input $user"
  fi

  if [[ -n "${NEW_CONFIG:-}" ]]; then
    cat <<MSG

  NOTE: the shipped config has changed and yours was left alone. To see what
  is new:      diff $CONFDIR/config.toml $CONFDIR/config.toml.new
  To take it:  sudo cp $CONFDIR/config.toml.new $CONFDIR/config.toml && omen-fx reload
MSG
  fi

  cat <<MSG

Done. Try it:

  omen-fx status
  omen-fx play notify
  omen-fx demo
  omen-fx-gui                        # graphical effect editor
  notify-send -u critical "test" "the bar should flash"

To also light the bar on password prompts (sudo / howdy):

  sudo $0 pam-enable

MSG
}

do_pam() {
  need_root
  local action="$1"
  [[ -x "$LIBDIR/pam-hook.py" ]] || die "run '$0' first"
  if [[ "$action" == "enable" ]]; then
    cat <<'MSG'
This edits /etc/pam.d/sudo. Both added lines are "optional" and the hook always
exits 0, so a failure cannot lock you out -- but keep a root shell open in
another terminal until you have confirmed sudo still works.
A backup is written next to each file as <file>.omen-fx.bak.

MSG
    read -rp "Continue? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || die "aborted"
  fi
  "$LIBDIR/pam-hook.py" "$action" "${PAM_FILES[@]}"
  if [[ "$action" == "enable" ]]; then
    echo
    echo "Now, WITHOUT closing your root shell, test in another terminal:  sudo -k true"
    echo "If sudo misbehaves, undo with:  $0 pam-disable"
  fi
}

do_uninstall() {
  need_root
  info "removing the PAM hook"
  [[ -x "$LIBDIR/pam-hook.py" ]] && "$LIBDIR/pam-hook.py" disable "${PAM_FILES[@]}" || true

  local user; user="$(real_user)"
  if [[ -n "$user" ]]; then
    runuser -u "$user" -- systemctl --user disable --now omen-fx-notifyd.service 2>/dev/null || true
    runuser -u "$user" -- systemctl --user disable --now omen-fx-brightnessd.service 2>/dev/null || true
  fi
  systemctl disable --now omen-fxd.service 2>/dev/null || true
  rm -f "$UNITDIR/omen-fxd.service" "$USERUNITDIR/omen-fx-notifyd.service"
  rm -f "$USERUNITDIR/omen-fx-brightnessd.service"
  rm -f /etc/tmpfiles.d/omen-fx.conf "$BINDIR/omen-fx" "$BINDIR/omen-fx-gui"
  rm -f "$BINDIR/omen-fx-brightness" "$BINDIR/omen-fx-brightnessd"
  rm -f /usr/local/share/applications/omen-fx-gui.desktop
  rm -rf "$LIBDIR" "$DOCDIR"
  systemctl daemon-reload
  info "kept $CONFDIR and /var/lib/omen-fx -- delete them by hand if you want them gone"
}

case "${1:-install}" in
  install)      do_install ;;
  pam-enable)   do_pam enable ;;
  pam-disable)  do_pam disable ;;
  pam-status)   need_root; "$LIBDIR/pam-hook.py" show "${PAM_FILES[@]}" ;;
  uninstall)    do_uninstall ;;
  *) die "unknown action: $1 (install | pam-enable | pam-disable | pam-status | uninstall)" ;;
esac
