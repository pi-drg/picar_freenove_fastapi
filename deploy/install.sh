#!/usr/bin/env bash
# Idempotent installer for picar_freenove_fastapi on a Raspberry Pi (Bookworm/Trixie).
# Run it from anywhere:  ./picar_freenove_fastapi/deploy/install.sh
#
# Options:
#   --i2c-fast   switch the I2C bus to 400kHz fast mode (needs a reboot).
#                Opt-in: only useful for high-rate control loops, and it raises
#                bus-integrity risk on this HAT's long traces. See config.py.
set -euo pipefail

I2C_FAST=0
for arg in "$@"; do
    case "$arg" in
        --i2c-fast) I2C_FAST=1 ;;
        -h|--help)  sed -n '2,9p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

# PKG is the package dir itself; the venv lives inside it, and the systemd unit's
# WorkingDirectory is PKG's parent so that `import picar_freenove_fastapi` resolves.
PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$PKG/.venv"

echo "==> Installing apt deps (picamera2, lgpio, spidev, i2c-tools)"
sudo apt-get update -qq
sudo apt-get install -y python3-picamera2 python3-lgpio python3-spidev i2c-tools python3-venv

if [ ! -e /dev/i2c-1 ]; then
    echo "==> Enabling I2C (reboot required afterwards)"
    sudo raspi-config nonint do_i2c 0
    NEED_REBOOT=1
fi

# I2C bus speed is a device-tree parameter fixed at boot — it cannot be set from
# Python, so config.py's i2c_expected_hz only *checks* it. This is where it is
# actually changed. Default (no dtparam line) is 100kHz.
if [ "$I2C_FAST" = "1" ]; then
    BOOTCFG=/boot/firmware/config.txt
    [ -f "$BOOTCFG" ] || BOOTCFG=/boot/config.txt
    if grep -q '^dtparam=i2c_arm_baudrate=400000' "$BOOTCFG"; then
        echo "==> I2C fast mode already set in $BOOTCFG"
    else
        echo "==> Enabling I2C fast mode (400kHz) in $BOOTCFG (reboot required)"
        sudo sed -i '/^dtparam=i2c_arm_baudrate=/d' "$BOOTCFG"
        echo 'dtparam=i2c_arm_baudrate=400000' | sudo tee -a "$BOOTCFG" >/dev/null
        NEED_REBOOT=1
    fi
    echo "    remember to set i2c_expected_hz = 400_000 in config.py"
fi

# SPI drives the WS2812 LED strip on SPI0 MOSI (GPIO10).
if [ ! -e /dev/spidev0.0 ]; then
    echo "==> Enabling SPI (reboot required afterwards)"
    sudo raspi-config nonint do_spi 0
    NEED_REBOOT=1
fi

if ! id -nG "$USER" | grep -qw spi; then
    echo "==> Adding $USER to the spi group (re-login required)"
    sudo usermod -aG spi "$USER"
fi

if ! id -nG "$USER" | grep -qw i2c; then
    echo "==> Adding $USER to the i2c group (re-login required)"
    sudo usermod -aG i2c "$USER"
fi

# --system-site-packages is REQUIRED: picamera2 and lgpio come from apt and must
# be visible inside the venv. Without it the server cannot import them.
if [ ! -d "$VENV" ]; then
    echo "==> Creating venv with --system-site-packages"
    # --prompt picar => shell shows "(picar)" when activated, not "(.venv)"
    python3 -m venv --system-site-packages --prompt picar "$VENV"
fi

echo "==> Installing pip deps"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$PKG/requirements.txt"

echo "==> Writing /etc/yakrobot/env"
sudo mkdir -p /etc/yakrobot
if ! sudo test -f /etc/yakrobot/env; then
    TOKEN="$(openssl rand -hex 16)"
    printf 'ROBOT_TOKEN=%s\nGPIOZERO_PIN_FACTORY=lgpio\n' "$TOKEN" | sudo tee /etc/yakrobot/env >/dev/null
    sudo chmod 640 /etc/yakrobot/env
    sudo chgrp "$USER" /etc/yakrobot/env
    echo "    generated a new ROBOT_TOKEN"
else
    echo "    already exists — leaving it alone"
fi

echo "==> Verifying venv can see the apt-installed packages"
"$VENV/bin/python" -c "import picamera2, lgpio, fastapi, smbus2; print('all imports OK')"

echo
echo "Install complete."
[ "${NEED_REBOOT:-0}" = "1" ] && echo "REBOOT REQUIRED (I2C was just enabled)."
exit 0
