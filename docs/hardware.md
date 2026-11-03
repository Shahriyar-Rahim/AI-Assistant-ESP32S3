# Hardware wiring and validation

Target board: ESP32-S3 N16R8 (16 MB flash, 8 MB OPI PSRAM). Firmware pin assignments are centralized in [`../firmware/include/hardware_config.h`](../firmware/include/hardware_config.h). Confirm the exact board/module schematic before connecting power; this document is a wiring contract, not evidence that physical hardware was tested.

## INMP441 input — I2S0

| INMP441 | Connection |
|---|---|
| VDD | 3.3 V, with 1 µF decoupling capacitor to GND |
| GND | Common ground |
| L/R | GND (left channel) |
| WS/LRC | GPIO 17 |
| SCK/BCLK | GPIO 18 |
| SD | GPIO 15 |

Firmware uses I2S0 as an independent 16 kHz microphone receiver. The microphone supplies 24-bit samples in 32-bit I2S slots; firmware reduces these to signed 16-bit mono PCM.

## MAX98357A output — I2S1

| MAX98357A | Connection |
|---|---|
| VIN | VBUS/5 V supply, within the module rating |
| GND | Common ground |
| BCLK | GPIO 3 |
| LRC | GPIO 8 |
| DIN | GPIO 16 |
| GAIN | GND |
| SD/EN | GPIO 46, with a 10 kΩ pull-down to common GND |

Connect the 4 Ω/8 Ω speaker across the amplifier's speaker output terminals, not to ground. Respect module, speaker, and power-supply current/thermal ratings. The amplifier has an independent I2S1 clock and DMA path. Add a 10 kΩ pull-down from SD/EN to common GND so the amplifier stays shut down while the ESP32 is resetting or its GPIO is not configured. Firmware also configures GPIO46 low at the beginning of audio initialization, enables the amplifier only for sound playback, and mutes it after 250 ms without queued audio. On boot it pauses microphone capture and plays a two-second 660 Hz sine tone on I2S1, writes a short silent tail, mutes the amplifier, then resumes capture before starting device networking. This is a real hardware output check; it does not depend on Wi-Fi or backend connectivity.

## SSD1306 OLED display — I2C0

| Display | Connection |
|---|---|
| VCC | 3.3 V (or the module's documented input voltage) |
| GND | Common ground |
| SDA | GPIO 1 |
| SCL | GPIO 2 |

The firmware probes the common SSD1306 I²C addresses 0x3C and 0x3D during startup and renders the live dashboard and animated face at 128×64. It configures I²C0 with internal pull-ups; use the module's pull-ups or add suitable external pull-ups if the bus is unreliable. If no OLED acknowledges either address, the firmware falls back to the ST7735 SPI display. When both are attached, the OLED is selected first. The SSD1306 panel is self-lit and does not use GPIO13.

## ST7735 1.8-inch 128×160 display — SPI2

| Display | Connection |
|---|---|
| VCC | 3.3 V |
| GND | Common ground |
| CS | GPIO 10 |
| DC/RS | GPIO 9 |
| RST | GPIO 14 |
| SDA/MOSI | GPIO 11 |
| SCK | GPIO 12 |
| LED (backlight positive) | GPIO 13 PWM, only if the TFT module's built-in current limiting and LED input current are GPIO-safe |

The firmware initializes the ST7735 controller and renders a live text dashboard with todo/reminder/note/notification/alarm counts, weather, calendar entries, and game state. Voice game moves for Tic-Tac-Toe, Rock-Paper-Scissors, Number Guessing, Trivia, Hangman, and Word Association run on the ESP32-S3 itself (`firmware/main/device_games.cpp`): the backend relays recognized speech as `game.command` (list/start/input/stop), the device applies the rules and AI opponent, draws the live game screen, plays the sound effect, and answers with `game.event` carrying the text to speak. Simulated devices keep the backend-side game path (SIMULATED). Calendar notifications update the display and queue a generated alert tone. The face blinks while idle and animates its mouth, gaze, and slight head motion during listening/speech; mapped emoji icons and generated wake/game/notification tones are rendered/played through the existing display and I2S1 paths. General animation interpolation, physical tuning, and acoustic validation remain incomplete. Rendering uses the existing SPI2 driver and does not add a physical button. The TFT is selected only when no supported OLED responds at 0x3C/0x3D; the firmware cannot probe an SPI TFT's identity, so verify its wiring if neither display appears. It assumes the common 128×160 ST7735 panel mapping with zero row/column offset; some module revisions require a different tab/offset sequence or orientation. Confirm the exact board revision if the image is shifted, clipped, or color-inverted.

GPIO 13 LEDC brightness control is implemented for the TFT backlight pin. The reported module labels this pin `LED` and it has been confirmed to light when driven directly from GPIO13; use direct PWM only if the module's current limiting keeps the GPIO current within ESP32-S3 limits. Do not also connect that positive LED pin to 3.3 V when it is connected to GPIO13. If the module requires more current or lacks suitable current limiting, use an appropriate driver; the BC547 low-side circuit is not suitable for a positive-only LED input. Firmware build/source tests do not validate actual panel colors, offsets, OLED address, or backlight current.

There is no physical button. The project does not assign GPIO 5.

## Wi-Fi setup and travel dashboard

On first boot, the firmware starts the visible WPA2 setup AP (`Buddy-Setup-<MAC suffix>`, password `BuddySetup-ESP32S3`). After Wi-Fi/backend configuration, a normal boot starts station mode only, avoiding the extra AP radio while connecting. If the saved Wi-Fi does not appear, authentication fails, or the station does not receive an IP address within 10 seconds, the firmware enables the setup AP and dashboard so credentials can be corrected. Connect to the AP and open `http://192.168.4.1/` directly. The portal accepts IPv4 clients on the setup AP subnet, including IPv4-mapped IPv6 sockets. For a deployed backend, enter its public DNS hostname, `https://` URL, or `wss://` URL (optionally with a port, and without an API path); the firmware uses secure `wss://`. For a trusted local network, enter `ws://192.168.10.163:8000` with your backend machine's private LAN IP and port; private RFC1918 IPv4 addresses are accepted and use unencrypted `ws://`. The device and backend must be on the same reachable LAN, and port 8000 must be accessible. Do not use the unencrypted option on public or untrusted Wi-Fi. Device ID and token can be left unchanged on reconfiguration. Captive DNS and Wi-Fi scanning are not implemented. The status page distinguishes router association from successful DHCP/IP connection and shows the station IP when assigned. Because the AP password is shared and compiled into firmware, anyone nearby who knows it can attempt to access the setup network; change the constant in `firmware/main/device_network.cpp` before deploying devices outside a trusted environment. NVS-at-rest encryption is not enabled; physical possession of the board can expose saved credentials.

To erase all flash contents, including saved Wi-Fi/backend configuration, then rebuild and flash firmware:

```sh
pio run -d firmware -t erase
pio run -d firmware -t upload
```

This erases the firmware as well as configuration. Connect the board over USB and run both commands from the repository root; the setup AP returns after the firmware is uploaded and boots.

## Before power-on

1. Verify board pin numbering, module logic voltage, common ground, and all connections with power disconnected.
2. Verify GPIO 46 strapping behavior on the specific board and amplifier module before relying on it as the shutdown/enable signal.
3. Check that the selected N16R8 board has OPI PSRAM and 16 MB flash; the PlatformIO board definition cannot validate the physical module.
4. Confirm speaker impedance, amplifier rating, USB/VBUS supply capacity, and backlight driver current.
5. Flash and test microphone capture, playback, display buses, simultaneous audio, and thermal behavior individually before end-to-end use.

No physical board, display, microphone, amplifier, speaker, or electrical measurement was available during software validation.
