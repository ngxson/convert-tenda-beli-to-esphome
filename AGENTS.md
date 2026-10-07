# Maintainer guide

This file replaces the original `PROGRESS.md` and holds the complete operating procedure, technical reference, and investigation history. [README.md](README.md) is the short user quick start. Personal identifiers, credentials, captures, and machine-specific paths belong only in ignored `tmp/`.

- [Detailed conversion procedure](#detailed-conversion-procedure)
- [Recovery and future updates](#recovery-and-future-updates)
- [Status LED](#status-led)
- [Power estimate and calibration](#power-estimate-and-calibration)
- [Development and publication checks](#development-and-publication-checks)
- [Stock cloud OTA protocol](#stock-cloud-ota-protocol)
- [Investigation record](#investigation-record)

## Firmware scope and capabilities

**Experimental, tested on three SP9 V1.0 units with RTL8710BX, 1 MiB flash, and stock firmware `V1.1.0.17(139)_SP9_EU`.** The first conversion required native OTA recovery after a GPIO experiment. The second installed the relay and provisional meter build directly from stock in one transfer, using the public proxy with a fresh cloud challenge. The third installed the current relay/button/meter/LED template directly from stock in one transfer. All three matched their recorded MAC and passed network uptime checks without an API key. Other models and stock versions are unverified. The older SP3 dump informed the protocol investigation; it does not establish SP3 conversion support.

The supplied firmware provides a **Main Plug** on/off switch, Wi-Fi, an API without encryption or a password, native OTA, a web page, uptime, and restart. The relay uses **PA12 / GPIO12, active high**, and defaults to OFF after every boot. An unloaded on/off test produced both physical relay clicks on the first SP9. **Power Estimated** now reads load-correlated pulses on **PA18 / GPIO18**, with a provisional calibration from one externally measured 4.1 W lamp. The meter IC and accuracy at other loads are unverified. The physical button on **PA11 / GPIO11** toggles the outlet and has been confirmed on the first SP9. The LED indicates Wi-Fi and Home Assistant connectivity as described below. The earlier all-pins GPIO probe boot-looped; do not repeat it.

A failed flash can leave the plug requiring physical recovery. Use a spare plug with nothing connected to its outlet and keep power stable during a transfer.

## Repository layout and private files

| Path | Purpose |
| --- | --- |
| `esphome/` | ESPHome template, private-secret setup, Docker build, image preparation, and verification |
| `boards/tenda-sp3.json` | Custom 1 MiB board layout; historical filename, tested with SP9 |
| `mitm/` | Native LAN proxy, provisioning helper, observer, and offline protocol checks |
| [Protocol reference](#stock-cloud-ota-protocol) | Verified wire format and flash layout |
| `AGENTS.md` | Detailed setup, recovery, calibration, and investigation history |

Local investigation files belong in `tmp/`, which is ignored by Git. Stock dumps, compiled firmware, packet captures, old GPIO sweeps, and personal device configuration are not distributed. Firmware and generated build source embed credentials. Keep them private even if a build succeeds.

## Detailed conversion procedure

Run commands from the repository root unless specified otherwise. You need Python 3.10+, Docker, a 2.4 GHz Wi-Fi network, and a computer on the same LAN as the plug. The proxy must run **natively**, since Docker Desktop hides the plug's source IP. Docker is used for ESPHome builds and verification.

### 1. Identify one target

Set up the plug with the Tenda Beli app. Record its model, hardware and firmware versions, serial number, MAC address, and current IP. Use the router's DHCP lease information to confirm which physical plug you selected and reserve its IP during conversion.

In each terminal that needs them, set these values interactively (the entered identifiers stay out of shell command history):

```sh
printf 'Stock plug IPv4: '; read -r PLUG_IP
printf 'Plug serial number: '; read -r PLUG_SN
printf 'Plug MAC address: '; read -r PLUG_MAC
printf 'This computer LAN IPv4: '; read -r PROXY_IP
export PLUG_IP PLUG_SN PLUG_MAC PROXY_IP
```

Confirm `http://<plug-ip>:5000/getSta` responds with HTTP 200 and `resp_code: 0`. Keep any returned identifiers private. Verify the serial against the selected plug; do not copy an example serial.

Allow incoming TCP **1821**, **11812**, and **11813** from the plug to the computer. The proxy's admin interface binds only to **127.0.0.1:9999**. The computer also needs access to the stock cloud for registration and a real upgrade challenge. Cloud addresses are configurable and may change.

### 2. Create private credentials

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 esphome/init_secrets.py
```

Enter the plug's current Wi-Fi network. This creates ignored `esphome/secrets.yaml` with fresh native OTA and fallback AP credentials. Keep a private backup. It refuses to overwrite an existing file, preserving access to previously flashed devices.

Edit the name and friendly name in `esphome/sp9.yaml` if needed. Use a unique lowercase name with hyphens. For another plug, use a separate private checkout or preserve its configuration and secrets before changing them. Do not enable unverified hardware pins.

### 3. Build and prepare the stock OTA image

```sh
./esphome/build.sh
```

The builder pins ESPHome **2026.9.1**, installs the custom board definition, compiles the template, and prepares `esphome/sp9_esphome_ota2.bin`. The original successful build used LibreTiny **1.13.0**. Check the build output before using a different toolchain.

`prepare_cloud_image.py` validates the image headers and slot size, and restores only missing trailing RAM zeros confirmed by the untrimmed build artifact. Do not substitute a renamed UF2 or a generic 2/4 MiB board image.

Two different outputs have different uses:

- `esphome/sp9_esphome_ota2.bin`: first conversion through the stock cloud updater.
- `esphome/.esphome/build/<device-name>/.pioenvs/<device-name>/firmware.uf2`: later ESPHome native OTA and safe-mode recovery.

Both contain secrets and are ignored by Git. The builder stops its temporary container after building. `ESPHOME_IMAGE` can override the container image for deliberate toolchain experiments.

### 4. Start the proxy and redirect the selected plug

In terminal A:

```sh
./mitm/run.sh --my-ip "$PROXY_IP" --target-ip "$PLUG_IP" --target-sn "$PLUG_SN"
```

In terminal B, activate `.venv` and set the same target variables. Point the stock plug at the proxy, providing your local timezone and UTC offset in minutes (the example below uses UTC):

```sh
python3 mitm/repoint.py --target-ip "$PLUG_IP" --proxy-ip "$PROXY_IP" \
  --location Etc/UTC --timezone 0
python3 mitm/admin.py status
```

Wait for a runtime connection and **`serial_verified: True`**. The proxy checks both the source IP and the registration serial before allowing runtime injection. If the plug has not reconnected, a power cycle may be needed at this point, before any push.

Startup relays traffic but does not flash. The public proxy requests a fresh challenge from the cloud for this plug; no captured device token is embedded. **Automatic challenge acquisition and the target guards were used successfully for the second stock-to-ESPHome conversion.** The original successful conversion replayed a real captured challenge. A private 32-byte capture can be supplied with `--challenge-file` when investigating handshake failures.

### 5. Send one update and observe

Once the target and stock status are correct:

```sh
python3 mitm/admin.py streamcfg 1024 5 empty fe
python3 mitm/observe_trial.py --target-ip "$PLUG_IP" --push --seconds 300
```

**`--push` starts the flash operation.** The first command sent to the plug advertises `IMAGE_SIZE` and erases its inactive OTA2 slot. The observer checks the proxy target, verified serial, and stock status before asking for the push.

Expect FA → FB → upgrade registration/challenge/config → FC → FE data → `clean stream done`. The proxy calculates the stream size from your image. Do not send FD packets or experiment with checksum matrices.

The first successful transfer took about 15 seconds; the second took 11.5 seconds and exposed ESPHome services about 39 seconds after the push began. Leave the plug powered and allow several minutes for Wi-Fi or safe-mode recovery. A completed TCP send alone does not prove installation. Local diagnostics are written to ignored `logs/` and may contain account or device identifiers.

### 6. Verify ESPHome

Find the current IP by the MAC recorded earlier; DHCP may have changed it. Open `http://<current-ip>/`. Expected services are web **80**, API **6053**, and native OTA **8892**; stock HTTP **5000** should close.

Set `PLUG_IP` to the current address, then run the identity and stability check:

```sh
docker run --rm --entrypoint python3 -v "$PWD/esphome:/project:ro" \
  ghcr.io/esphome/esphome:2026.9.1 /project/verify_device.py \
  --host "$PLUG_IP" --mac "$PLUG_MAC" --config /project/sp9.yaml
```

It must match the recorded MAC and configured name, connect without an API key, and observe increasing uptime for 70 seconds. Then stop terminal A with Ctrl-C. Add the device to Home Assistant's ESPHome integration by its current IP; this demo does not request an API key. Use **Main Plug** to switch the outlet. It starts OFF after each reboot; a short physical-button press toggles it locally. **Power Estimated** uses the provisional calibration described below.

## Recovery and future updates

If only port **8892** opens, ESPHome may be in safe mode. Upload the known working build with ESPHome's native uploader:

```sh
docker run --rm --entrypoint esphome -v "$PWD/esphome:/project" \
  -v "$PWD/.pio_cache:/root/.platformio" ghcr.io/esphome/esphome:2026.9.1 \
  upload /project/sp9.yaml --device "$PLUG_IP"
```

The `/project` mount matches the builder's cached artifact paths. This selects the compiled UF2 and reads the OTA password from `secrets.yaml`. Wait for `OTA successful`, then repeat verification. The tested safe-mode window lasted five minutes; a missed window may require waiting for the next one. Do not send a stock `push` once ESPHome is running.

If stock firmware stays responsive after an ignored attempt, save the private log. Once the sender is idle and no transfer is active, stop the proxy, power-cycle only the selected plug, restart the proxy, and wait for a fresh verified runtime connection. A stale upgrade session did not always clear by closing the socket alone.

If neither stock nor ESPHome returns, check DHCP and the `Beli Recovery` fallback AP. After confirming no transfer is active, try one power cycle. If it still does not return, investigate boot/UART recovery instead of repeatedly sending updates.

For future changes, keep the same credentials, rebuild with `./esphome/build.sh`, use the native upload command above, and repeat verification. Do not resume the all-pins output sweep.

## Status LED

The indicator follows the outlet state and connection status:

| Connection status | Outlet ON | Outlet OFF |
| --- | --- | --- |
| Wi-Fi disconnected or still connecting | Solid white | Blinking white |
| Wi-Fi connected, Home Assistant disconnected | Solid yellow | Blinking yellow |
| Home Assistant connected | Solid blue | Off |

“White” is the name used here for both LED channels on together. Blinking
means 500 ms on, then 500 ms off. Wi-Fi status takes priority, including while
connecting at boot. A fallback hotspot does not count as a connection to the
configured Wi-Fi network.

The channels use active-low GPIO22 (blue) and GPIO19 (yellow), based on the
stock mapping. Mode selection and transitions have been checked in software;
physical colour confirmation is still pending. The combined colour is a naming
convention, not a measured white colour.

Home Assistant is recognised by its authenticated ESPHome API client name;
opening the web page, logs or a standalone API tool does not count. API
client-loss reboots are disabled so Home Assistant being unavailable does not
periodically restart the plug. The physical button still toggles the outlet
locally. A reboot or power cycle starts the outlet OFF.

## Power estimate and calibration

The template uses ESPHome's [pulse meter](https://esphome.io/components/sensor/pulse_meter/)
on PA18 as an input without pull resistors. A measured 4.1 W lamp produced
about 26.70 rising edges per minute; no pulses were observed with the outlet
OFF. The trial conversion is `watts = pulses_per_minute * 0.15356649`.
After native OTA, the sensor reported 3.94–4.04 W for that reference lamp
and returned to 0 W on OFF; identity and uptime checks passed.
PA5 also carries a roughly 669 Hz signal, but its role is unconfirmed and
it is not used. No unknown meter-select pin is driven.

This is a **single-point estimate on one unit**, not recovered factory
calibration. The meter chip, accuracy across different loads and power factors,
and transferability to another SP9 remain unverified. Voltage, current and
energy totals are not provided. To calibrate another unit:

1. Measure a steady, small test load with a reference power meter.
2. Connect it to the converted plug, turn **Main Plug** ON in the web page
   or Home Assistant, and wait for several stable **Power Estimated** readings.
3. Change the `multiply` value in `esphome/sp9.yaml` using
   `new_multiplier = old_multiplier * reference_watts / displayed_watts`.
   If the reading stays zero or is erratic, investigate the pin/hardware
   revision instead of dividing by zero or guessing a scale.
4. Rebuild and upload through native ESPHome OTA. Check the same load,
   then a second measured load, and confirm 0 W with the outlet OFF.

Readings arrive on pulses, roughly every 2.25 seconds for the reference lamp.
The outlet-OFF action publishes 0 W immediately. With the outlet ON, 30 seconds
without a pulse produces 0 W; very small loads can therefore alternate with
zero. High-load accuracy has not been characterized. Keep the measured
reference values and per-device calibration in ignored `tmp/`.

## Development and publication checks

```sh
python3 -m unittest discover -s mitm -p 'test_*.py'
python3 -m unittest discover -s esphome -p 'test_*.py'
python3 tools/check_public_tree.py
```

The last command checks the files Git would publish, including files already tracked despite ignore rules. It also works before `git init`. It flags likely local identifiers, embedded credentials, and forbidden artifacts without printing their contents. It is a guard, not proof that arbitrary future files contain no sensitive information. Review `git status` and your staged diff before pushing; `.gitignore` does not remove files or secrets already committed to Git history.

The optional `mitm/check_cloud_stream.py` emulator requires Unicorn and matching, privately held SP3 XIP/RAM payloads via `--dump-dir`. It is research support, not a compatibility check for an arbitrary plug. See the [investigation record](#investigation-record) for its limits.

## Stock cloud OTA protocol

These findings combine three live SP9 conversions with emulation of an older SP3 dump. The investigation record below preserves the evidence and limits.

### Flash layout used

| Region | Flash offset |
| --- | --- |
| Original active OTA1 | `0x0B000` |
| Inactive OTA2 | `0x80000` |
| Configuration starts | `0xF5000` |
| Maximum OTA2 composite | `0x75000` bytes |

The prepared composite starts with `81958711`, a little-endian XIP length and zero load-address field, and a 32-byte segment header. The OTA2 ELF links XIP at `0x08080020`; that address is not encoded in the composite header, so a header check alone cannot distinguish OTA1 from OTA2. Always use the build helper's OTA2 artifact. The subsequent RAM segment is linked at `0x10005000`. The preparation helper compares it with the untrimmed RAM artifact before restoring trailing zeros.

### Wire versus internal commands

| Operation | Wire command, module 0 | Internal module 3 |
| --- | --- | --- |
| Version query / reply | F8 / F9 | 18 / 19 |
| Upgrade address query / reply | FA / FB | 1A / 1B |
| Service request | FC | 1C |
| Incoming firmware | **FE** | **1E** |

Frames have a 16-byte header: `24 00`, direction, status, module, command, big-endian payload length, and eight flag bytes. TLVs use a big-endian 16-bit tag and length.

### Successful sequence

1. Runtime D5, direction 03, flags beginning `03 19`, with JSON `IMAGE_SIZE = 56 + 2 * image_length` and `VERSION = V2.0.0(1)_LT`. This erases inactive flash.
2. After three seconds, runtime D5 with flags `02 04` triggers the update. The tested device delayed about six seconds before continuing.
3. Plug FA with flags `03 1A`; reply FB with TLV 14 = proxy IPv4 and TLV 15 = upgrade port. Drop the duplicate upstream FB.
4. On the upgrade connection, REGISTER 17 carries serial TLV 01. Reply 18, direction 08, TLV 04 = the real 32-byte cloud challenge. The plug replies 19.
5. Reply config 1A, direction 08: TLVs 07/08/09 each zero, 0A = big-endian 100, 0B = big-endian 0x15180.
6. The plug sends FC with flags `03 1C` and JSON `UPGRADE_TYPE: 1`. Wait one second.
7. Stream **FE / module 0 / direction 08 / zero flags**. TLV 1A is the one-byte state; TLV 1C holds data. State 1 carries the 56-byte header. State 2 carries the complete image twice in 1024-byte chunks, five milliseconds apart. Send state 3 without a data TLV to finish.

The successful header contains two 24-byte entries after an eight-byte prefix, ordered OTA2 then OTA1. `build_dl_header()` preserves the observed working format. Raw-updater checksum fields in this header do not establish a checksum requirement in the emulated cloud path.

The emulated write-mode-1 path skips the first image copy, writes the second at OTA2, and reaches boot-slot selection after the final marker. Network completion is followed by device reboot and still needs ESPHome identity/stability verification.

### Local proxy boundaries

The public CLI requires one IPv4 target and one 17-character serial. Runtime injection requires a matching REGISTER on the current connection. The upgrade listener rejects other source IPs and mismatched registration serials. The control endpoint binds to loopback.

The original conversion used a captured real cloud challenge. The public source contains no such token: it sends the selected plug's REGISTER to the configurable real upgrade service and validates the 18/TLV04 response, or reads a private `--challenge-file`. Offline fixtures cover validation, and the fresh-fetch path completed the second matching SP9 conversion successfully.

Capture mode allows only session/authentication replies through to the plug and withholds firmware/service data. It is a research feature, not part of the normal conversion instructions. Low-level admin commands remain experimental; do not infer that every exposed command is safe to try or needed for conversion.

## Investigation record

### Current result and scope

A sealed SP9 V1.0 / RTL8710BX / 1 MiB plug running stock `V1.1.0.17(139)_SP9_EU` was converted over the network. The successful stock FE stream installed an exploratory GPIO image. That image boot-looped, entered ESPHome safe mode, and was replaced through native OTA with a network-only image. The final device matched its recorded MAC, exposed HTTP 80/API 6053/OTA 8892, closed stock port 5000, and passed 70 seconds of increasing uptime.

The supplied template adds the physically verified PA12 relay and PA11 button, a provisional PA18 pulse-based power estimate, and the requested connection-status LED logic, with credentials externalized. The earlier network-only template's cloud image was prepared and checked. The relay and meter additions were tested through native ESPHome OTA on the first unit and then installed directly from stock on a second matching unit. A third matching unit later received the current relay/button/meter/LED template directly from stock. These three units do not establish support for other hardware revisions or stock versions.

ESPHome 2026.9.1 and LibreTiny 1.13.0 were used. PA12 is physically verified for the relay on the tested SP9. PA18 carries load-correlated pulses used for a one-point power estimate; the meter IC and cross-load accuracy remain unverified. The PA11 active-low button has since been physically confirmed. LED support is currently being verified. PA6–PA11 can serve flash/SPI in the variant; the GPIO sweep likely disturbed flash operation, but the individual cause was not isolated. Never restore that sweep as a recommended discovery method.

### Demo API update

At the user's request, API encryption was removed from the installed network-only configuration. This was the only device configuration change. A fresh 876544-byte UF2 compiled with ESPHome 2026.9.1 / LibreTiny 1.13.0 and uploaded successfully through native OTA. The new public verifier then connected without an API key, matched the recorded MAC and configured name, and passed 70 seconds of increasing uptime. No power cycle was needed.

The corresponding prepared stock-OTA composite is 437768 bytes, with four confirmed trailing RAM zeros restored. It has not been flashed through stock OTA. The composite XIP header's load-address field is zero; the OTA2 link address belongs to the ELF/linker layout, not that header. Both the public preparation helper and proxy validate this actual header format.

### Working protocol

- Wire FE, module 0, direction 08, zero flags dispatches to internal module 3 command 1E.
- The 56-byte header is followed by the OTA2 image twice, using 1024-byte chunks and a 5 ms delay. State 1 carries the header, state 2 both image copies, and an empty state 3 completes the stream. No FD preamble or final FD is used.
- Announce `IMAGE_SIZE = 56 + 2 * len(image)` using D5/0319, wait three seconds, then D5/0204. The announcement erases the inactive slot.
- Reply to FA with FB containing the local upgrade endpoint; suppress the real cloud's duplicate FB because it can re-trigger and abort the FSM.
- Use a real cloud challenge and the observed session config. Random challenge bytes caused immediate disconnects.
- The original transfer sent 941504 bytes in 922 frames in 14.8 seconds; the upgrade socket closed about six seconds later. Those numbers describe the old probe, not a fixed size to reuse.
- In write mode 1, the first image copy is skipped and the second is written at 0x80000. Never extend writes into configuration at 0xF5000.

### Recovery evidence

ESPHome's five-byte OTA hello `6c26f75c45` received `0002` while only port 8892 was available. A native OTA upload of a 943616-byte UF2 then succeeded in 14.8 seconds. The safe-mode window observed was five minutes. One earlier upload missed the window; a subsequent attempt succeeded.

The prepared network-only OTA2 composite was 471460 bytes. `prepare_cloud_image.py` restored four trailing RAM zeros confirmed by the untrimmed artifact, because ltchiptool trimmed zeros while retaining the ELF-derived header length. Preserve this verification; do not blindly pad an image.

### Investigation findings and rejected theories

The available SP3 dump was `V1.1.0.13(115)`, not the live SP9 firmware. Parts were erased or overwritten. Its real Thumb registration routines assign no receiver to internal 1D and assign the firmware receiver to 1E. Raw module-3 1D, raw module-3 1E, D5/031E, and wire FF did not install the image on the live SP9. Wire FE supplied the missing distinction between internal and network commands.

`mitm/check_cloud_stream.py` executes the dump's real registration, incoming handler, TLV parser, FSM, counters, and write calculations. Hardware, missing library routines, logging, and teardown are stubbed. It checks byte-for-byte writes within 0x80000..0xF5000 and stops before the boot-slot change. It does not emulate a complete SP9 boot or prove a new stock version is compatible. It needs private `xip_payload.bin` and `ram_payload.bin` files matching the investigated SP3 binary; arbitrary dumps are not interchangeable.

Native macOS Unicorn 2.1.0 crashed during an earlier offline run; Linux Unicorn 2.1.4 worked. The stdlib network proxy does not use Unicorn.

The earlier claim that a missing checksum/header was the sole blocker was unsupported. The visible SP3 cloud completion path changes slots and reboots without invoking the separate raw TCP updater's checksum verifier. Preserve the successful header format without claiming its raw-path checksum semantics were established for every cloud implementation.

### Public-tree maintenance

- Keep target IP and serial explicit; require the runtime registration serial to match before injecting commands. Verify the post-flash MAC and configured name through the native API. The demo API intentionally has no encryption key or password, as requested by the user.
- Public code fetches a fresh challenge or reads an explicitly supplied private 32-byte challenge file. This replaces the original hardcoded capture. Automatic acquisition and the CLI target guards have offline coverage and were used in the second successful live conversion.
- Use `!secret` for credentials. Treat all captures, dumps, binaries, build source, and personal configuration as private. No real device ID belongs in fixtures or examples; use documentation addresses and synthetic serials.
- Run the offline tests and `tools/check_public_tree.py` after edits. Never use a live push as an ordinary software test. Any future live device work must be within the user's explicitly requested scope.
- The public layout intentionally excludes obsolete protocol matrices, failed GPIO configurations, cloud-account tooling, and generated/decompiled third-party artifacts. The local working files are retained under ignored `tmp/`: `tmp/original/` preserves the original workspace, `tmp/gpio-research/` holds the current investigation, and `tmp/repo-migration/` holds cleanup staging. Keep all future scratch files, captures, and private builds under `tmp/`; never force-add its contents to Git. See the local `tmp/README.md` for the workspace map.

### GPIO and APK investigation

The archived SP3 `V1.1.0.13(115)` code establishes PA12 as its relay pin,
PA11 as its button, and PA22/PA19 as its LEDs. The literal at `0x080165F4`
points to the RAM board table at `0x10005DAC`; offsets +4/+8/+12/+16 hold
12/11/22/19. Relay function `0x08015CA8` initializes the +4 pin as output and
passes its requested 0/1 state directly to `gpio_write` at `0x0802039C`.
The relay assignment has since been physically verified on the test SP9; the button was subsequently confirmed on SP9. PA22/PA19 LED colour confirmation is in progress.

The preserved Beli 1.6.4 APK has no embedded firmware image. Its
`CheckUpdatePresenter` calls `DirectInfo?Module=2&Cmd=3&AppVer=2` for version
information and command 4 to request a device-side update, then polls the
version again. `UpdateVer` has only current/latest version fields. The
`app/NewVer` endpoint concerns the Android app itself. A credential-free
connection check found an expired certificate on the cloud API; no firmware
update was triggered during this investigation.

The relay-only candidate under `tmp/original/custom_fw/esphome/` was uploaded
after the user confirmed an empty outlet. It exposes Main Plug on `GPIO12`
(the variant's PA12), active high, with `restore_mode: ALWAYS_OFF`. The
883200-byte UF2 uses 441260/479232 flash bytes (92.1%). Post-upload checks
matched identity, observed boot-OFF and 70 seconds of increasing uptime, and
reported ON then OFF during a three-second pulse. The user heard both clicks
on a repeated pulse. The final requested relay state was OFF. The public
and private main YAML configurations now include this verified switch.

ESPHome's custom-board validation rejects the `PA12` alias; the LibreTiny
variant explicitly maps numeric 12 to PA12. Do not use Arduino D12, which is
a different pin. Build, upload, and verification logs remain under ignored
`tmp/gpio-research/`. The previous private network-only YAML is preserved.

Meter research is under `tmp/gpio-research/`. Receive-only PA18 UART tests
returned zero bytes across six formats, both through the normal IRQ receive
path and direct hardware FIFO polling. The private probe explicitly used
SerialClass(0, 18, PIN_INVALID) to avoid the normal global Serial0 object's
default TX pin. It did not drive an unknown transmit pin.

Read-only GPIO observations found activity on PA5. Its direction register
confirmed input; only the verified PA12 relay was configured as a GPIO output.
A fast PA5 capture found a near-50%-duty square wave, approximately 668 Hz,
with the outlet OFF. Other watched inputs (PA0, PA14, PA15, PA18, PA19,
PA22, PA23, PA29) stayed constant. Treat PA5 as a candidate meter pulse
signal, not an identified CF/CF1 pin or proof of a particular meter IC.

The first waveform capture used a millisecond-quantized micros() clock;
discard its apparent 1000 Hz estimate. The corrected probe reads the existing
32768 Hz hardware timer and converts tick differences precisely to microseconds.
It changes no pin direction, mux or pull setting. The earlier private
`sp9-meter-wave.yaml` provides Start Meter Capture for 40 short timing bursts
followed by a bounded 30-second passive input watch. There is no automatic
capture at boot. Main Plug and API without encryption remain available.

The user subsequently connected a lamp measured at 4.1 W with another meter
and confirmed it illuminated during the controlled test. PA18 produced
26 transitions in the 30-second ON watch, versus zero with the relay OFF.
A hardware-timer capture measured 12 rising-edge intervals averaging
2.247314453125 seconds (26.6985334 pulses/minute). PA5 remained near 669 Hz
in both states. A follow-up OFF capture confirmed PA18 stopped pulsing.

The installed private `sp9-meter-candidate.yaml` and public template now use
standard ESPHome `pulse_meter` on GPIO18 as an input without pulls. EDGE mode
uses rising interrupts; avoid PULSE/CHANGE because the LibreTiny RTL8710B
CHANGE workaround actually selects rising edges. The filter is 1 ms and
no-pulse timeout 30 seconds. Power Estimated applies a provisional multiplier
of 0.15356649 W per pulse/minute. Relay OFF immediately publishes 0 W.
The 887808-byte UF2 uses 443372/479232 flash bytes. After native upload,
14 settled samples for the 4.1 W reference ranged from 3.94 to 4.04 W
(median 3.99 W). Identity, keyless API, boot OFF, final OFF/0 W and over
70 seconds of increasing uptime passed. Public hardware/API settings match
the compiled private candidate; the prepared stock composite was not flashed.
No unknown SEL/output is driven. The temporary Start Meter Capture diagnostic
is absent from the new configuration. The relay-only recovery image remains
preserved separately.

This is a load-correlated pulse estimate calibrated at one measured load,
not recovered factory calibration or proof of the meter IC. Validate other
loads and power factors before claiming general power accuracy. Very low
loads can hit the 30-second timeout; high-load timing accuracy remains
uncharacterized. See [Power estimate and calibration](#power-estimate-and-calibration) for the per-device procedure.

The SP3 dump lacks a meter driver. The inspected SP9 certification photos
show another MCU revision, RTL8720CF, and do not establish this RTL8710BX
unit's meter interface. PA18 is supported by live pulse evidence; the chip,
other meter pins and factory calibration remain unknown. Do not add a guessed
driver or restore the old GPIO sweep.

### Second stock-to-ESPHome conversion

A second SP9 V1.0 reported the same stock V1.1.0.17(139)_SP9_EU. The user
selected it explicitly and confirmed that its connected load could lose power.
A separate name and private OTA credentials were prepared. Stock identity and
image SHA256 were rechecked before provisioning, and the runtime registration
serial matched before one push.

The public proxy fetched a fresh cloud challenge and sent the relay/meter
composite in the verified FE format: 887096 stream bytes, 869 frames,
11.5 seconds. The 443520-byte composite had four verified RAM zeros restored;
the UF2 was 887808 bytes. ESPHome services appeared about 39 seconds after
push. No manual power cycle, safe-mode recovery, or second push was needed.

The API matched the original MAC and unique name without encryption. Stock
port 5000 closed, web/API/OTA opened, and more than 70 seconds of increasing
uptime was observed. It booted OFF/0 W. A later ON state changed the outcome of
the stricter OFF-throughout assertion; the read-only verifier did not command
that change. Continued identity/uptime checks passed. Do not describe that
run as an OFF-throughout pass. Private notes retain the state observation and
pending user clarification. The proxy was stopped once streaming finished.
The provisional meter coefficient comes from the first unit and has not been
calibrated against a reference load on the second unit.

### Physical button and LED update

The PA11 button is physically confirmed on the first SP9. The passive input
watch initially saw nothing because PA11 remained muxed to flash. A read-only
pad snapshot showed dual-data flash with PA11/FD3 unused, consistent with the
stock button mapping. Configuring GPIO11 input with pull-up, inversion, polling
and 30 ms debounce enabled one local relay toggle per press; the user confirmed
it works. Avoid CHANGE interrupts on this platform and never sweep flash pins.
The earlier timed verification captured no presses before the user responded;
retain that failed capture as such, not as an automated button-test pass.

A native LED candidate on the first unit retains the confirmed button, relay
and provisional meter. It uses active-low PA22/PA19, matching the stock relay
and connection indicators. A human colour check is still pending. The candidate
selects blue for HA connected + outlet ON, darkness for connected + OFF, solid
yellow for disconnected + ON, and yellow blinking 500 ms on / 500 ms off for
disconnected + OFF. It inspects authenticated live API clients whose names start
with `Home Assistant `, and requires Wi-Fi connected; log and verification clients
do not satisfy it. This uses ESPHome 2026.9.1's public `active_clients()` view,
so recheck the API when changing the pinned ESPHome version. API reboot timeout
is disabled to retain local operation when Home Assistant is unavailable.

The 895488-byte LED UF2 uses 447244/479232 flash bytes and uploaded successfully
through native OTA. A live test passed all four logical modes, ignored a
non-HA logger/state subscriber, handled two overlapping simulated HA clients,
restored the initial OFF state, and observed 80 seconds of increasing uptime.
These were simulated API sessions, not a real Home Assistant outage. Logs
verify selected modes; human confirmation of physical colours is pending.
The private canonical configuration was aligned with that installed candidate.
The public template now also includes the requested LED behavior and the Wi-Fi
extension described below; physical colour confirmation remains pending.

The second unit still runs its relay/meter build, without the button or LEDs.

### Wi-Fi status LED extension

The user explicitly requested calling both LED channels together white and
using white while Wi-Fi is not connected: solid with the outlet ON, blinking
with it OFF. This takes priority over Home Assistant status, including a stale
HA connection during Wi-Fi loss. Once Wi-Fi connects, the previous yellow/blue
HA behavior applies. White is a label for both outputs, not a measured colour.
All blinking uses 500 ms on / 500 ms off. The fallback AP does not satisfy the
station-connected check.

The interval runs during initial Wi-Fi setup: its polling setup priority
precedes Wi-Fi, and the ESPHome setup wait services the scheduler. Only the
two known LED outputs are changed; relay, button, meter and credentials stay
the same. The Wi-Fi recovery timeout retains its existing default.

A host harness compiled the actual YAML lambda and checked all six modes,
both blink phases, stale HA priority, ordinary client exclusion, and clearing
the unused channel when leaving mixed white. These are simulated inputs,
not a real Wi-Fi outage test or optical colour measurement. Public and private
hardware/LED blocks are compared for equality, with credentials externalized
in the public configuration. The earlier two-colour build is preserved.

The Wi-Fi LED extension was compiled and installed on the first unit through
native OTA. UF2 895488 bytes, flash 447308/479232, RAM 35980/262144; upload
took 13.41 seconds. Post-upload verification matched MAC/name without an API
key and passed 70 seconds of increasing uptime. No relay commands or deliberate
Wi-Fi outage were used in this extension's live check. The public template
and private canonical configuration now contain the same LED behavior.
All 19 required offline tests passed. The second unit was not modified.

### Third stock-to-ESPHome conversion

A third SP9 V1.0 reported the same stock V1.1.0.17(139)_SP9_EU. The user
selected it and confirmed that its outlet was empty or could safely lose power.
A unique ESPHome name and separate OTA/fallback credentials were prepared from
the current public template, including relay, physical button, provisional
power estimate and the Wi-Fi/HA status LED logic.

The build used 447404/479232 flash bytes and 35980/262144 RAM bytes. The
prepared 447464-byte composite restored four confirmed trailing RAM zeros;
the native UF2 was 895488 bytes. The offline SP3 emulator wrote the exact
composite within OTA2 and reached slot change. Its SP3/SP9 limits still apply.

Stock identity and image SHA256 were rechecked before provisioning and push.
Runtime serial verification passed. One FE push using a freshly fetched cloud
challenge sent 894984 stream bytes in 876 frames, taking 10.3 seconds.
ESPHome services appeared at the same address about 39 seconds after push.
No manual power cycle, repeated push or native recovery was needed.

Read-only verification matched the original MAC and unique name without an API
key. Main Plug, Power Estimated, Physical Button and Restart were present.
The outlet stayed OFF, the button reported released and power stayed 0 W.
Seventy seconds of increasing uptime passed; web/API/OTA ports were open and
stock port 5000 was closed. No relay command was sent during verification.
The proxy was stopped after streaming completed.

This verifies conversion, firmware stability and entity/state availability.
Button actuation and physical LED colours were not separately checked on the
third unit; the power coefficient is still copied from the first unit's
one-point calibration. The earlier two units were not modified during this
conversion. Private identity, credentials, builds and logs stay under ignored
`tmp/`, with the workspace index pointing to the per-device handoff.

### Next work

Validate the provisional PA18 power estimate against a second measured load and investigate the meter IC if stronger board evidence becomes available. The relay is complete. Map any additional pins from board evidence or controlled observations. Add only confirmed pins, update through native ESPHome OTA, and verify identity and stability. Record future results with hardware/firmware versions and technical evidence, keeping device identifiers and credentials in private notes.
