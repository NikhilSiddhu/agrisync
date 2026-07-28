# Bill of materials

| # | Part | Role | Notes |
|---|---|---|---|
| 1 | ESP32 WROOM-32D dev board | Board A, the brain | flashes over its own USB |
| 2 | ESP32-CAM + OV2640 | Board B, canopy imaging | needs the FTDI to flash |
| 3 | FTDI / USB-TTL programmer | flashes Board B | set the jumper to 5 V |
| 4 | BME280 (I2C) | air temp / humidity / pressure | addr 0x76 or 0x77 |
| 5 | MLX90614 (I2C) | leaf IR temp, wetness proxy | addr 0x5A |
| 6 | ADS1115 16-bit ADC (I2C) | clean analog front end | addr 0x48 |
| 7 | microSD 32 GB class-10 | JPEG buffer for Board B | FAT32, do not go bigger |
| 8 | Grey/white balance card | colour reference in frame | or print and laminate |
| 9 | Raindrop module | rain wet/dry + tips | placeholder rain gauge |
| 10 | WAAREE 10 W solar panel | power in | ~17-21 V open circuit |
| 11 | Skypearll 20 A PWM controller | charge control | 12 V lead-acid profile |
| 12 | 12 V 7 Ah SLA battery | energy storage | must be 12 V lead-acid |
| 13 | Buck converter 12->5 V 3 A | the 5 V rail | plus a >=1000 uF capacitor |
| 14 | ABS enclosure 200x155x80 | weatherproof housing | opaque white, IP65+ |
| 15 | PG cable glands (IP68) | sealed cable entry | PG7 sensors, PG9-11 solar |
| 16 | Conformal coating spray | seals solder joints | after bench test passes |
| 17 | Perfboard, wire, resistors | assembly | 1x 47 kohm for battery divider |
| 18 | Inline fuse holder + 3 A fuse | battery protection | on the + lead |
| 19 | SGP40 VOC sensor | optional extra signal | addr 0x59 |
| 20 | JXBS-3001-TR soil sensor | secondary (RS485) | drop if it delays go-live |

## Board A pin map

| Signal | Pin(s) | Notes |
|---|---|---|
| I2C SDA / SCL | 21 / 22 | BME280 + MLX90614 + ADS1115 (+ SGP40) |
| Raindrop AO / DO | 35 / 32 | DO on interrupt, counts tips |
| Leaf-wet probe (future) | ADS1115 A0 | PHYTOS-31 upgrade path |
| Battery divider | 39 | 47k / 10k, optional |
| Soil RS485 | 25 / 33 / 4 | DI / RO / DE-RE, UART2 |
| Status LED | 2 | onboard blue |
| 4G modem (parked) | 26 / 27 | not in this build |

## I2C addresses

All four sensors share 3V3, GND, GPIO21, GPIO22. Different addresses keep them from
clashing. Add 4.7 kohm pull-ups on SDA and SCL if your breakouts lack them.

| Sensor | Address | Role |
|---|---|---|
| BME280 | 0x76 or 0x77 | core |
| MLX90614 | 0x5A | core |
| ADS1115 | 0x48 | core |
| SGP40 | 0x59 | optional |

## Power chain order

1. Battery to controller BAT terminals, through the 3 A fuse
2. Solar panel to PV
3. Buck converter input to LOAD
4. Meter the buck output and adjust to exactly 5.0 V
5. Solder the >=1000 uF capacitor across the buck's 5 V output
6. Only now connect 5 V and GND to Board A and Board B

Reversed polarity kills an ESP32 instantly. Meter every pair before it touches a board.
Battery first, panel second: some controllers are damaged if the panel is connected
with no battery attached.

## Two warnings that cost real money

**The BME280 must never be sealed inside the box.** In direct sun an unshielded or
enclosed sensor reads 5 to 15 C too hot, which corrupts the dew point, which corrupts
leaf wetness, which corrupts every disease number downstream. It goes in a radiation
shield, outside.

**Do not use a breadboard outdoors.** Vibration and damp kill breadboard connections
within weeks. Transfer to perfboard and solder before the box goes in the field.
