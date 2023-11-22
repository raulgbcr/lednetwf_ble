# LEDnetWF_ble

Home Assistant custom integration for LEDnetWF devices wich are not supported on the official LEDBLE integration. WIP

## Suported devices

This have only been tested with Zengge LEDnetWF devices, may also be known as:
 - Zengge LEDnetWF
 - YBCRG-RGBWW 
 - Magic Hue
 - Bluetooth full colors selfie ring light

## Suported Features
 - Automatic discovery of supported devices
 - On/Off
 - Color temp mode
 - RGB mode (With included turn off circle effect)
 - Brightness
 - Effects (thanks https://github.com/8none1 !!)
## Not suported
 - Current status decoding (not implemented atm)
 - External remotes or commands do not reflect their changes on Home Assistant

## Instalation
### Requeriments
You need to have the bluetooth component configured and working in Home Assistant in order to use this integration.
### Manual installation

Clone this repository into `config/custom_components/lednetwf_ble` Home Assistant folder.

### Config
After setting up, you can config two parameters Settings -> Integrations -> LEDnetWF -> Config.

 - Reset color when led turn on: Temp solution, since device response messages are not decoded yet and status when device resets is unknown.
 - Disconnect delay or timeout: Timeout for bluetooth disconnect (0 for never)

## Credits
This integration is possible thanks to the work of this incredible people!

https://github.com/8none1/zengge_lednetwf for reverse engineering and decoding the protocol used by the BLE controller!

https://github.com/dave-code-ruiz/elkbledom for most of the base code adapted to this integration.

Thanks!

