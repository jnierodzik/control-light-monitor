# Raspberry Pi setup, from sealed boxes to a verified camera

Follow this in order. Nothing here needs any of this project's code — by the end you
will have independently proved that the core technique works on your actual light,
before a single line of `stacklight` runs on the Pi.

Steps 0 and 4 are the ones that matter. The rest is routine.

---

## 0. Check the parts before you open anything

| Part | Status | Notes |
| --- | --- | --- |
| Raspberry Pi 5, 2GB | on order | 2GB is plenty; this workload is a few MB of numpy |
| Camera Module 3, standard | on order | IMX708, 66°×41° FOV, 10cm minimum focus |
| 500mm camera cable | **check this** | see below — this is the one that bites |
| microSD card | ? | 16GB+, A1 or A2 class. Not mentioned in your list |
| USB-C power supply | ? | Pi 5 wants 5V/5A (27W). Not mentioned in your list |
| Active cooler / heatsink | ? | Pi 5 throttles hard without one. Not mentioned |
| microSD reader for the Mac | ? | Needed to flash |
| Ethernet cable | optional | Makes first boot bulletproof if Wi-Fi misbehaves |

### The camera cable

**The Pi 5 camera connectors are the narrow 22-pin 0.5mm FPC type.** The Camera Module 3
has a 15-pin connector and ships with a 15-pin-to-15-pin cable, which physically will
not fit a Pi 5.

So the 500mm cable has to be the **22-pin to 15-pin** variant — Raspberry Pi sell it as
"Camera Cable Standard – 22-pin to 15-pin", in 200/300/500mm.

Check the cable now, before anything else. Count the contacts on each end: one end
should be visibly narrower with 22 contacts, the other wider with 15. If both ends look
the same, it is the wrong cable and you will want to reorder today rather than discover
it mid-assembly.

### A note on 500mm

500mm is the longest standard cable, and MIPI CSI-2 is not a forgiving signal. If the
camera is intermittent or throws timeouts later, cable length and routing near the
machine's own wiring are the first suspects. Keep it away from motor leads and
solenoids.

---

## 1. Flash the card, from the Mac

```bash
brew install --cask raspberry-pi-imager
```

Open Raspberry Pi Imager and choose:

* **Device:** Raspberry Pi 5
* **Operating System:** Raspberry Pi OS Lite (64-bit) — Bookworm or later. Lite, not
  Desktop: this thing is headless and a desktop is dead weight on 2GB.
* **Storage:** your microSD card

Then open **Edit Settings** (the gear, or it prompts you). **This is the step that makes
headless work** — configure it here and the Pi comes up ready to SSH into:

* **Hostname:** `stacklight` (matching the `node_id` keeps things obvious)
* **Username and password:** pick and write them down
* **Services → Enable SSH → Allow public-key authentication only**, and paste your key
* **Wireless LAN:** SSID, password, and **country code** (Wi-Fi stays off without it)
* **Locale:** your timezone and keyboard

Your public key, on the Mac:

```bash
cat ~/.ssh/id_ed25519.pub
```

If that file does not exist, make one first:

```bash
ssh-keygen -t ed25519 -C "$(whoami)@$(hostname)"
```

Write the image. Takes a few minutes.

---

## 2. Connect the camera with the power off

Ribbon cables are not hot-pluggable. Do this before the Pi is ever powered.

1. Attach the active cooler / heatsink first — it is much easier before the camera cable
   is in the way.
2. On the Pi, use either **CAM/DISP 0** or **CAM/DISP 1**. Both work and the OS
   auto-detects; just note which one you used.
3. Lift the connector's small plastic latch straight up, slide the cable fully in, and
   press the latch back down. It should feel firmly seated, not springy.
4. Same at the camera end.

**Contact orientation matters and is easy to get backwards.** Follow the photos in the
official Raspberry Pi camera documentation rather than guessing. If step 4 reports no
camera, a flipped or half-seated cable is overwhelmingly the most likely cause — check
that before anything else.

Do not mount the camera on the printed arm yet. Get it working on the bench first.

---

## 3. First boot and SSH in

Insert the card, connect power, wait about a minute.

```bash
ping -c 3 stacklight.local
ssh youruser@stacklight.local
```

If `.local` does not resolve, find the Pi's address from your router's client list, or:

```bash
arp -a | grep -i b8:27:eb   # older Pi OUI
arp -a | grep -i d8:3a:dd   # newer Pi OUI
```

Once in, bring it up to date and reboot:

```bash
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## 4. Verify the camera — and prove the technique

This is the important step. Everything this project does rests on one assumption: that
with the camera locked at a short exposure, an energised LED is the only bright thing in
frame. Check that assumption here, with stock tools, before trusting any of my code.

First, does the OS see the sensor at all?

```bash
rpicam-hello --list-cameras
```

You want a line mentioning `imx708`. If you get "no cameras available", go back to the
cable in step 2.

A normally exposed reference shot:

```bash
rpicam-still -o /tmp/normal.jpg --width 1280 --height 720
```

Now the shot that matters. Point the camera at the machine's stack light with the machine
powered and at least one segment lit, then take a locked, heavily underexposed frame:

```bash
rpicam-still -o /tmp/dark.jpg \
  --width 1280 --height 720 \
  --shutter 800 --gain 1.0 --awb off --denoise off --immediate
```

Pull both back to the Mac and look at them:

```bash
scp youruser@stacklight.local:/tmp/normal.jpg /tmp/normal.jpg
scp youruser@stacklight.local:/tmp/dark.jpg /tmp/dark.jpg
open /tmp/normal.jpg /tmp/dark.jpg
```

**What you are looking for in `dark.jpg`:** a nearly black image with a small, sharply
bounded bright patch where each lit segment is, and nothing else bright anywhere.

That is the whole design, validated. If instead you see:

* **Everything black, including the LED** — exposure is too short for this light. Retry
  with `--shutter 2000`, then `4000`, until the segment appears.
* **The LED is a blown-out blob much bigger than the segment** — exposure is too long.
  Come down: `--shutter 400`, `--shutter 200`.
* **Other bright things in frame** — a window, a work lamp, a reflection off the machine.
  Either reframe, or note where they are; that region must not overlap any ROI, and it is
  exactly what the `dark_reference` check is for.
* **The segment is out of focus** — Camera Module 3 autofocuses, and we are going to lock
  it. Minimum focus distance is 10cm, so the arm must hold the camera at least that far
  from the light. Find a working `--lens-position` (dioptres, i.e. 1/metres — `3.5` is
  about 29cm) by trying a few:
  ```bash
  for lp in 2.0 3.0 3.5 4.5 6.0; do
    rpicam-still -o /tmp/lp$lp.jpg --shutter 800 --gain 1.0 --awb off \
      --autofocus-mode manual --lens-position $lp --immediate
  done
  ```

**Write down the `--shutter`, `--gain` and `--lens-position` that gave you a clean
result.** Those go straight into `exposure_us`, `analogue_gain` and `lens_position` in
the node config, and they are the starting point the calibration UI refines.

While you are here, note roughly how the light behaves: does red flash, and how fast?
Are there patterns beyond the four currently configured? That answers the two open
questions in the detection tuning.

---

## 5. Install the node software

All of this is driven from the Mac. Substitute your hostname, username and node name:

```bash
make bootstrap PI=stacklight.local PI_USER=youruser
```

That installs the apt packages, creates `/opt/stacklight` and `/etc/stacklight`, and
builds the venv. The one detail that matters:

```bash
python3 -m venv --system-site-packages .venv
```

`picamera2` is not reliably pip-installable — it comes from apt. A venv made *without*
`--system-site-packages` will install everything else fine and then fail on `picamera2`
alone, which is a confusing way to lose twenty minutes.

Now put the exposure numbers you found in step 4 into `config/machine1.yaml`:

```yaml
camera:
  source: picamera
  exposure_us: 800        # your --shutter
  analogue_gain: 1.0      # your --gain
  lens_position: 3.5      # your --lens-position
```

and push it:

```bash
make deploy PI=stacklight.local PI_USER=youruser NODE=machine1
```

`deploy` rsyncs the code, installs the config to `/etc/stacklight/machine1.yaml`, installs
the systemd unit, and runs `stacklight validate` on the Pi so a bad config fails before
the service ever starts.

### First run, in the foreground

Before making it a service, watch it work. This is the same tuning view as `make dev`,
but fed by the real camera:

```bash
ssh youruser@stacklight.local
/opt/stacklight/.venv/bin/stacklight detect /etc/stacklight/machine1.yaml --interval 1
```

You get a line per second with the measured brightness of each ROI beside the mode it
resolves to. Cover the light with your hand, uncover it, and watch the numbers move. If
the ROIs are nowhere near the segments, that is expected at this point — nothing has
been calibrated yet; step 4's numbers only fix exposure, not geometry. The calibration UI
is what places the rectangles.

### Check the colour channels

One thing I could not verify without hardware: picamera2's format naming is famously
confusing about byte order, and the code assumes an `"RGB888"` stream arrives B-G-R in
memory and swaps it back. Detection does not care — the default metric takes the maximum
channel, so a lit segment reads the same either way — but snapshots would show red and
blue swapped.

Light the *red* segment, grab a snapshot, and look at it. If the red lamp appears blue,
set:

```yaml
camera:
  swap_rb: false
```

### Run it as a service

```bash
ssh youruser@stacklight.local 'sudo systemctl enable --now stacklight-node@machine1'
make logs PI=stacklight.local PI_USER=youruser NODE=machine1
```

The unit is templated on the node name, so a second camera later is another config file
and another `systemctl enable --now stacklight-node@<name>` — nothing central changes.

**If the service fails but the foreground run worked**, it is almost certainly one of
two things: the unit's `User=pi` does not match the username you chose when flashing
(edit `systemd/stacklight-node@.service`), or that user is not in the `video` group,
without which libcamera finds no camera at all.

---

## 5a. Enclosure

The Pi has to live within 500mm of the light, because that is the cable length — which
puts it in the working area of a press that throws powder residue, primer dust and brass
shavings. **Brass shavings are conductive**, and a bare board with exposed GPIO pins in
that environment will eventually produce an intermittent fault that looks exactly like a
camera problem. Bench work bare is fine; permanent mounting is not.

### The cooling-versus-dust tension

The Active Cooler needs airflow, and airflow is how dust gets in — a fan does not merely
admit particulate, it actively pulls it through the fins.

Worth knowing: **this workload is thermally trivial.** Capturing 720p at 15fps and taking
numpy means over a few thousand pixels per segment leaves the Pi 5 drawing roughly 4–5W,
against the ~12W of a genuinely busy one. Active cooling is sized for sustained full-CPU
load, which this will never be. So dust, not heat, is the binding constraint here, and the
enclosure should be chosen accordingly.

A good compromise that uses hardware already bought: **fit the Active Cooler but leave its
fan lead unplugged.** It is a spring-mounted aluminium fin stack with a pre-applied thermal
pad, so it works as a perfectly reasonable passive heatsink, and nothing is pulling grit
through it. Check the temperature after a day of running:

```bash
vcgencmd measure_temp
```

Anything under about 70°C has plenty of margin — the Pi 5 throttles in the low 80s. If it
sits higher than you like, plug the fan back in and accept periodic cleaning.

### Recommended print

[**Pi 5 Case / Enclosure by Power_Billy**](https://www.thingiverse.com/thing:6434416)
(Thingiverse, CC BY-NC-SA). Mix-and-match bases and lids, all combinable:

| File | Use |
| --- | --- |
| `PI5_Boden_mit_Fluegel.stl` | Base **with wings** — screw tabs for mounting to a wall or bracket |
| `PI5_Gehäuse_Boden.stl` | Base without wings |
| `PI5_Deckel_aktiv_clean.stl` | Active-cooler lid, cable pass-through, **no extra vents** |
| `PI5_Deckel_aktiv_plus.stl` | Active-cooler lid, cable pass-through, plus ventilation slots |
| `PI5_Deckel_passiv_clean.stl` | Passive lid with vents, **no cable pass-through** |

For this install: **`PI5_Boden_mit_Fluegel` + `PI5_Deckel_aktiv_clean`.** The wings solve
mounting, and `aktiv_clean` is the most closed lid that still has a cable pass-through —
it has the Active Cooler's fan aperture but no additional vents to let grit in.

The listing is in German; `Kabeldurchführung` is the cable pass-through, `Flügel` the
mounting wings, `Boden` the base, `Deckel` the lid.

Two changes to the designer's suggested settings:

* **Print in PETG, not the PLA listed.** PLA softens around 60°C, and a closed case in a
  warm shop with sun on it is not a good place to find that out. PETG or ASA is tougher
  and handles the heat.
* **More than 15% infill and 3 walls**, for a part that will be mounted near a vibrating
  machine.

**Widen the cable slot if needed.** The 22-pin end of the cable is roughly 11.5mm wide,
but the 15-pin camera end is about 16mm. Either open the slot to pass the wider end, or
thread the cable through the case *before* attaching it to the camera. Measure your own
cable rather than trusting those numbers.

### Alternative

[**Pi 5 case compatible with AI Kit and camera by sbguy**](https://www.thingiverse.com/thing:6708018)
(CC BY-NC-SA) is purpose-built for a Pi 5 with active cooling *and* a camera, including a
breakaway tab specifically for the camera ribbon. It also has a camera mount on the lid,
which is not useful here — the camera goes on the arm at the light — but the tab is the
feature. It has no wall-mount wings, so mounting is on you.

Both are CC BY-NC-SA. Non-commercial: worth a thought if this press is earning money
rather than serving a hobby.

---

## 6. One decision to make: where does the broker live?

The architecture has the broker and the rules engine as separate processes from the node,
which leaves a choice:

**Everything on the Pi** (mosquitto + node + rules engine). Simplest, one box, works
today. The catch is the one you already identified: *a silent monitor is worse than no
monitor*. If the Pi dies, the thing that would have told you the Pi died dies with it.
The heartbeat watchdog cannot fire from a dead machine.

**Node on the Pi, broker and rules elsewhere.** The watchdog becomes real. Needs a second
always-on machine — another Pi, a NAS, a small VPS. Your Mac technically works but it
sleeps, which makes for an unreliable watchdog.

A reasonable middle path is everything on the Pi now, plus an external dead-man's-switch
(the rules engine pinging a hosted uptime checker on a schedule, so *something off-site*
notices silence). That is a step 5 concern and nothing is blocked on deciding it today.

---

## Next

Once step 4 gives you a clean dark frame with the LEDs visible, you have the two things
that unblock everything else: working hardware, and the exposure numbers to seed the
config with.
