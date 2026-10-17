# Mac fan control while the LED app runs

Open **LED Fan Control** from the Dock, or use the **LED Fan Control** shortcut
on the Desktop. The native app is installed in `~/Applications/LED Fan Control.app`.
It shows live RPM, maximum RPM, and target RPM for both fans, with separate
40–100% speed sliders, a **100% · Full speed** button, and an **Automatic** button.
Both speed settings default to 100% and are saved between launches.
The **System temperatures** row shows CPU PECI, GPU proximity, and battery
temperatures in Celsius. Readings refresh with RPM every two seconds. Sensors
that cannot provide a valid reading show **Unavailable**.

**Only while LED app runs** is enabled by default. Turn it off to keep your
chosen speeds active even when the LED app is closed. Automatic mode always
hands control back to macOS. Closing the fan-control window leaves your saved
settings active in the background. Readings refresh every two seconds while
the window is visible; polling stops when the window closes.

The settings file is `~/Library/Application Support/LED Fan Control/settings.plist`.
The helper checks its owner, file permissions, and input values before applying
settings, and keeps requested RPM inside each fan's factory limits. Changing
speeds needs no further administrator prompts after the helper is installed.

This Mac has a local fan watcher installed as `local.led-animator.fans`.
By default it requests each fan's SMC-reported maximum RPM while this project's
`main.py`, `stream_arduino.py`, `system_audio_visualizer.py`, or
`led_animator.py` runs as your user. When the last matching process exits,
the watcher returns both fans to macOS automatic control within two seconds.
It checks again after waking and starts automatically after a restart.

The helper is compiled specifically for this MacBookPro15,1, this user's ID,
and this project's absolute path. It does not change the minimum or maximum
RPM limits. It reads each maximum and writes only the fan mode and target.
Stopping the watcher normally releases manual control. `launchd` restarts
the watcher after an unexpected exit; startup first restores automatic mode.
Sleep and power-off follow the Mac's normal hardware behavior.

Check the current state:

```sh
/Library/PrivilegedHelperTools/local.led-animator.fans --status
tail /var/log/led-animator-fans.log
```

Reinstall after moving the project, or remove and restore automatic control:

```sh
python3 scripts/install_mac_led_fans.py
python3 scripts/install_mac_led_fans.py --remove
```

Installation and removal use macOS's administrator dialog. Passwords are
handled by macOS, never this script. The installed binary and launch daemon
are owned by root; the daemon runs no shell commands and reads only the fixed
settings file described above. Source: `scripts/macos_led_fans.c`. The SMC protocol structures are
adapted from [smcFanControl](https://github.com/hholtmann/smcFanControl).

Rebuild the native app, Desktop shortcut, and Dock icon:

```sh
python3 scripts/build_mac_fan_app.py --desktop --dock
```

The fan-control simulation tests run without changing hardware:

```sh
clang -O2 -Wall -Wextra -Werror -framework IOKit -framework CoreFoundation \
  tests/mac_fan_control_test.c -o .build/fan-control/fan-tests
.build/fan-control/fan-tests
```
