# LEDnetWF_ble

Home Assistant custom integration for LEDnetWF devices which are not supported on the official LEDBLE integration. WIP

## Supported devices

This have only been tested with Zengge LEDnetWF devices, may also be known as:

- Zengge LEDnetWF
- YBCRG-RGBWW
- Magic Hue
- Bluetooth full colors selfie ring light

## Supported Features

- Automatic discovery of supported devices
- On/Off
- White / Color temperature mode
- RGB mode (With included turn off circle effect)
- Brightness
- Effects
- Live status updates from remote control (once connected)

## Installation

### Requirements

You need to have the bluetooth component configured and working in Home Assistant in order to use this integration.

### Manual installation

Clone this repository into `config/` Home Assistant folder.

### Config

After setting up, you can config two parameters Settings -> Integrations -> LEDnetWF -> Config.

- Disconnect delay or timeout: Timeout for bluetooth disconnect (0 for never)

## Credits

This integration is possible thanks to the work of this incredible people!

- https://github.com/8none1/zengge_lednetwf for reverse engineering and decoding the protocol used by the BLE controller!
- https://github.com/dave-code-ruiz/elkbledom for most of the base code adapted to this integration.
- https://openclipart.org/detail/185270/light-bulb-icon for the original icon.

Thanks!
