# Hardening the Pi

Ordered by how much real risk each step removes. Do them in order and stop
whenever you run out of evening; the top items matter far more than the bottom.

Threat model, honestly stated. Your data is cabbage microclimate readings, not
money. The realistic risks are:

1. **Someone injects fake readings.** This is the worst one. Bad data poisons
   the dataset silently and permanently, and you would not notice until the
   model gave you nonsense in December.
2. **The Pi gets compromised and used for something else.** A box on the open
   internet with a weak SSH password gets found within hours.
3. **Someone reads your data.** Least important. It is leaf temperatures.

Everything below is aimed at 1 and 2.

---

## 1. SSH keys, and turn off passwords

This is the single biggest improvement. Password SSH is the thing that actually
gets broken into.

**On your Mac:**

```bash
ssh-keygen -t ed25519 -C "nikhil-mac"
```

Press Enter for the default path. Give it a passphrase.

```bash
ssh-copy-id agrisync@nikhil.local
```

Test it. You should get in with no password prompt:

```bash
ssh agrisync@nikhil.local
```

**Only once that works**, disable password login on the Pi:

```bash
sudo nano /etc/ssh/sshd_config.d/99-hardening.conf
```

```
PasswordAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
```

```bash
sudo systemctl restart ssh
```

**Keep your current session open** while you test a new one from another
terminal. If you lock yourself out, you need a monitor and keyboard on the Pi
to fix it.

---

## 2. Firewall

Right now anything on your WiFi can reach port 5000.

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow from 192.168.1.0/24 to any port 22 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 5000 proto tcp
sudo ufw allow from 192.168.1.0/24 to any port 123 proto udp
sudo ufw enable
sudo ufw status verbose
```

That allows SSH, the API, and chrony time service from your home network only,
and blocks everything else from everywhere.

Port 123 is chrony. Leave it out and your ESP32 loses its clock.

**Once cloudflared is running** (section 6), tighten further: remove the port
5000 rule entirely and set `BIND_HOST=127.0.0.1` in `.env`. The tunnel reaches
the app over loopback, so nothing on your LAN needs direct access at all.

---

## 3. Automatic security updates

An unpatched Pi running for a whole season is a slow-motion problem.

```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Choose yes. It installs security patches only, so it will not surprise you with
a major version change mid-season.

---

## 4. fail2ban

Bans IPs that fail SSH repeatedly. Cheap insurance, especially if you ever port
forward SSH, which you should not do.

```bash
sudo apt install -y fail2ban
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd
```

---

## 5. Rotate your keys

Your `DEVICE_API_KEY` has been typed into terminals, pasted into chats, and sat
in shell history. Treat it as known.

```bash
openssl rand -hex 16     # new device key
openssl rand -hex 16     # new read key
```

Put both in `.env`:

```
DEVICE_API_KEY=<the first one>
READ_API_KEY=<the second one>
```

```bash
sudo systemctl restart agrisync-api
```

Then reflash the board with the matching `DEVICE_API_KEY`, or every POST comes
back 401.

Setting `READ_API_KEY` means your own read commands need it too:

```bash
curl -H "X-Read-Key: YOUR-READ-KEY" localhost:5000/api/v1/devices
```

Or `?key=` in a browser. Save yourself the typing:

```bash
echo "alias ag='curl -s -H \"X-Read-Key: YOUR-READ-KEY\" localhost:5000'" >> ~/.bashrc
source ~/.bashrc
ag/api/v1/devices | python3 -m json.tool
```

`/health` and `/health/db` stay open deliberately, so monitoring works without
credentials. They reveal nothing beyond whether the service is up.

---

## 6. Cloudflare Tunnel, for the 4G board

Once Phytoplex moves 1 km away and talks over the A7670, it cannot reach
`192.168.1.39`. That address only exists inside your house.

The wrong fix is port forwarding: it puts your API on the public internet
behind a single shared key over plain HTTP, and exposes your home IP.

The right fix is a tunnel. `cloudflared` makes an **outbound** connection from
the Pi to Cloudflare, so there are no inbound ports open on your router at all.

### Install

```bash
sudo mkdir -p /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install -y cloudflared
```

### Set up

You need a domain on Cloudflare. A cheap `.in` or `.xyz` costs a few hundred
rupees a year and is worth it here.

```bash
cloudflared tunnel login
cloudflared tunnel create agrisync
cloudflared tunnel route dns agrisync agrisync.yourdomain.com
```

Config at `~/.cloudflared/config.yml`:

```yaml
tunnel: agrisync
credentials-file: /home/agrisync/.cloudflared/<TUNNEL-ID>.json

ingress:
  - hostname: agrisync.yourdomain.com
    service: http://127.0.0.1:5000
  - service: http_status:404
```

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
curl https://agrisync.yourdomain.com/health
```

You now have HTTPS with a real certificate, no open ports, and your home IP
hidden. Set `BIND_HOST=127.0.0.1` in `.env` and restart the API.

### Lock it down with Access

A tunnel alone makes your API public, just at a different address. Add
Cloudflare Access (free tier) on top.

In the Zero Trust dashboard: **Access → Applications → Add a self-hosted
application** for `agrisync.yourdomain.com`. Create a policy allowing your own
email address.

Then, for the ESP32, which cannot do a browser login: **Access → Service Auth
→ Create Service Token**. You get a Client ID and Client Secret. Add a second
policy with action **Service Auth** that accepts that token.

The board then sends three headers instead of one:

```
CF-Access-Client-Id: <client-id>
CF-Access-Client-Secret: <client-secret>
X-API-Key: <your device key>
```

Now the only things that can reach your API are you, logged in with your email,
and one specific device holding that token. Everything else gets rejected at
Cloudflare's edge, before it ever touches your Pi.

Add an exception in Access for `/health` if you want uptime monitoring to keep
working from outside.

### Firmware changes for this

Three things change in `firmware/synthetic_wifi/src/main.cpp`:

1. The URL becomes `https://agrisync.yourdomain.com/api/v1/sensors/readings`
2. `WiFiClientSecure` instead of plain `HTTPClient` over http, with the
   Cloudflare root CA
3. Two extra headers for the service token

The A7670 has TLS built in and handles HTTPS over AT commands, so the cellular
path works the same way.

---

## What is deliberately NOT here

**HTTPS on your LAN.** Self-signed certificates on an ESP32 are a maintenance
burden with little benefit while the board is fifteen metres away on your own
WiFi. Once it moves to 4G, the Cloudflare tunnel gives you real TLS for free.
That is the right time to care.

**Encrypting the database.** Full-disk encryption on a headless Pi means it
cannot boot unattended after a power cut, which is a worse problem than the one
it solves. Your data is cabbage temperatures.

**A VPN.** WireGuard is excellent but the ESP32 cannot be a peer, so it does not
solve the problem you actually have.

---

## Checklist

- [ ] SSH keys working, password auth off
- [ ] ufw enabled, verified with `sudo ufw status verbose`
- [ ] unattended-upgrades installed
- [ ] fail2ban running
- [ ] DEVICE_API_KEY rotated and board reflashed
- [ ] READ_API_KEY set
- [ ] Cloudflare Tunnel up, `/health` answering over HTTPS
- [ ] Access policy with a service token for the board
- [ ] BIND_HOST changed to 127.0.0.1, port 5000 removed from ufw
