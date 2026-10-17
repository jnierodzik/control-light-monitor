# stacklight-monitor

Get a phone notification when a machine needs you, by reading its stack light
electrically.

Built for an industrial machine, but nothing here is specific to it — any
machine with a 24V tower light driven by switched legs will work.

## What you get

| The light does this | You get |
| --- | --- |
| Green goes solid | **"Machine running"** — the cycle started |
| Green starts flashing | **"Machine run complete"** — finished, waiting for you |
| Red comes on | **"Machine fault"** — stopped and needs you, escalates if ignored |
| The monitor itself dies | **"monitor offline"** |

Notifications go to your phone through [ntfy](https://ntfy.sh), which is free and needs
no account.

## How it works, in one paragraph

A pass-through harness sits between the machine's control box and its light, tapping
each lamp wire in parallel through optocouplers. Nothing is cut, nothing is modified,
and the lamp current never flows through anything you built — pull the harness and the
machine is exactly as it was. A Raspberry Pi samples those taps at 100 Hz, decides
whether each lamp is off, steady or flashing, and pushes a notification when that
changes. [Full design notes](docs/design.md).

---

# Building one

## 1. What to buy

| Qty | Item | Notes |
| --- | --- | --- |
| 1 | Raspberry Pi 5 | Plus official 27W supply, Active Cooler, microSD card |
| 1 | `MT-301R4P-P` optocoupler board | **24V in → 3.3V out.** Most cheap modules are the reverse |
| 1 | JST-XH 4-pin extension lead, male-to-female | Becomes the harness. Gets cut. Buy a 10-pack |
| 4 | Wago `221-413` lever connectors | 3-conductor |
| — | 22 AWG hookup wire | Black, red, green, blue |
| — | Dupont jumper wires | Opto board to the Pi's GPIO header |

**Check your light's connector first.** This build assumes JST-XH 2.5mm, 4-circuit —
one common plus three switched legs. Yours may differ.

Tools: a wire stripper that handles 22 AWG stranded, and a multimeter.

[Why each part is that part](docs/parts.md) — worth reading before ordering the
optocoupler board, which is the one easy thing to get wrong.

## 2. Print the case

Three parts in [`hardware/enclosure/`](hardware/enclosure/) — base, Pi layer, lid.
PETG or ASA, 0.2mm, no supports. Fasteners and assembly order are in
[that directory's README](hardware/enclosure/README.md).

## 3. Flash the Pi

Raspberry Pi Imager → **Raspberry Pi OS Lite (64-bit)** → **Edit Settings**:

- Hostname `stacklight`, and your username and password
- Wi-Fi SSID, password, country
- **Services tab:** enable SSH, public-key only, paste your public key

**Prefer 5 GHz if you have it.** 2.4 GHz is the band industrial machinery pollutes, and
it cost us a day of dropouts.

## 4. Wire the tap

**Machine powered off throughout.**

**Build the harness.** Cut the extension lead in half and rejoin each conductor through
a Wago, leaving the third lever empty. Cut and connect **one wire at a time** — most of
the conductors are the same colour, and a part-built harness holds its own order for
you. Label the Wagos W1–W4.

**Work out which Wago is which. Do not assume.** Power the machine, get one lamp lit,
and measure from the common to each Wago with the meter. The wiring diagram on your
light's box may not match what is actually in the connector — ours didn't, and wiring it
from the diagram would have put faults on the wrong channel and alerted nobody.

**Then wire each Wago's spare lever to the optocoupler board:**

| Signal | Sense wire | Opto terminal |
| --- | --- | --- |
| Common (gray) | black | the `-` terminals, bridged together |
| Red lamp | red | `1+` |
| Green lamp | green | `2+` |

Outputs `O1`/`O2` go to GPIO 17 and 27, plus 3.3V and GND from the Pi.

**The common must be the common.** Getting a lamp leg there instead is the one mistake
that can damage the board — and in our case it silently sounded the machine's buzzer on
every run, because the tap's return current was flowing through it.

## 5. Install

From your computer, not the Pi:

```bash
make bootstrap    # packages, mosquitto, directories, venv
make deploy       # code and config
```

Then the three `systemctl` lines it prints, on the Pi.

## 6. Set up notifications

Pick a long random topic name — **on ntfy.sh the topic name is the only secret there
is**, so anyone who guesses it can read your alerts and send fake ones.

```bash
cp config/rules-pi.yaml config/rules-pi.local.yaml
```

Put your topic in that file. It's gitignored, and `make deploy` sends it instead of the
committed placeholder. Subscribe to the same topic in the ntfy app.

Edit [`config/machine1.yaml`](config/machine1.yaml) if your light means something
different from ours.

## 7. Check it

```bash
make logs
```

Run a cycle. You should get a notification when it starts and another when it finishes.

---

## Day to day

```bash
make deploy      # push a config or code change
make restart     # pick it up (both services)
make logs        # follow both
make shutdown    # clean power-off — always use this
make history     # what the machine has been doing
```

**Always `make shutdown`.** Pulling the plug on a running Pi corrupts the SD card
eventually. It cost us an OS.

## Working on it

No Pi, camera or light required — that's deliberate, and it's what makes "does it spam
me when the light flaps" a test rather than an afternoon of watching a phone.

```bash
make setup
make test
make lint
```

## Documentation

| | |
| --- | --- |
| [Design notes](docs/design.md) | How it works, and why. Start here to understand it |
| [Parts](docs/parts.md) | Every part, why it's that part, and the measured wiring |
| [The electrical tap](docs/gpio-tap.md) | Isolation, polarity, and what went wrong on the way |
| [Pi setup](docs/pi-setup.md) | Longer-form setup — still written around the camera |
| [Enclosure](hardware/enclosure/README.md) | Print settings, fasteners, assembly |

A camera-based node is also supported and tested — it was the original approach, before
the light turned out to be readable electrically. See the
[design notes](docs/design.md#the-camera-path).
