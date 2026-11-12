# VPN router

Share your Mac's VPN with devices that cannot run a VPN themselves: a smart TV, a game console, an old tablet. You choose per device: **Tunnel** (through the VPN) or **Direct**.

> **Status: experimental.** It changes your Mac's firewall and IP forwarding. Read *Safety* below before you turn it on.

## How it works

```
TV  ──Wi-Fi──▶  Mac (gateway)  ──▶  your VPN tunnel (utunX)  ──▶  internet
```

1. On the device you set the Mac as its **gateway** (router), once.
2. macOS forwards the device's traffic. dlus adds firewall rules (NAT) so the answers come back.
3. The traffic follows **the same route as the Mac's own traffic**. dlus does not pick a VPN: whatever VPN carries your Mac's internet also carries the device.

Because of 3, dlus works with any VPN that sends **all** of the Mac's traffic through it, whatever the brand or protocol.

## Supported VPNs

| VPN | Works | What to set |
|---|---|---|
| WireGuard (app or `wg-quick`) | yes | `AllowedIPs = 0.0.0.0/0` (the default "full tunnel") |
| OpenVPN: Tunnelblick, Viscosity, OpenVPN Connect | yes | the profile must use `redirect-gateway def1` ("route all traffic") |
| Cisco AnyConnect / Secure Client | yes, if your VPN allows full tunnel | split tunnel: see below |
| Windscribe, Proton, Nord, Express, Surfshark, Mullvad, PIA, IVPN, Cloudflare WARP | yes | default settings (they are full tunnel). Turn **off** "allow LAN traffic" blocks / firewall modes that block local devices, if the app has one |
| V2Box, Hiddify, Clash, sing-box, Shadowrocket, FoXray (proxy apps) | yes, in **TUN / system-wide / enhanced** mode | in plain "system proxy" mode there is no tunnel, so nothing to share |
| FortiClient, GlobalProtect, IKEv2 profiles in macOS settings | yes, if full tunnel | split tunnel: see below |
| NetBird, Tailscale, ZeroTier (mesh VPNs) | no, by design | they connect private networks; they are not an internet exit (unless you use an exit node, which then counts as a full tunnel) |

The **VPN** card on the VPN router page shows which interface carries the Mac's internet right now, for example *Windscribe · utun6*.

### Split tunnel ("only some networks go through the VPN")

Some VPNs send only certain networks through the tunnel (split tunnel). dlus shows such a tunnel with **Route everything through it**. That adds two routes, `0.0.0.0/1` and `128.0.0.0/1`, into the tunnel. They win over the normal default route without removing it.

- dlus checks the internet straight away. If it stops working, the routes are removed again by themselves.
- The routes are also removed when you press **undo**, turn the router off, disconnect the VPN, or restart the Mac.
- Whoever runs your VPN may not want all your traffic going through it. Ask first.

## Install

1. Open **dlus → VPN router**.
2. Press **Install helper**. macOS asks for your administrator password; the output is shown on the page.
3. Turn on **VPN router**.

What the installer does, nothing more:

- copies `dlus-gw` to `/Library/PrivilegedHelperTools/dlus-gw`, a root-only folder, so neither dlus nor any other program can change it (the installer refuses if a folder on that path is not root-only)
- adds `/etc/sudoers.d/dlus-gw`, which lets **your** user run **only that one file** as root, without a password
- runs `dlus-gw selftest`, which syntax-checks the firewall rules and changes nothing
- removes the helper from its old place (`/usr/local/libexec`), if an older dlus put it there

Prefer Terminal? Run the command dlus shows under *what exactly runs as administrator*:

```sh
sudo bash /Applications/dlus.app/Contents/Resources/gateway/install.sh
```

When dlus updates the helper, the page shows **Update helper**. Same password prompt.

## Connect a device

Easiest: **VPN router → Connect a device to the VPN → Start**, or the **VPN** button next to your TV. dlus:

1. turns the router on and sets the device to *Tunnel*
2. shows the 4 values to type on the device, and can show them on an LG TV screen
3. checks, live, that:
   - the VPN reaches the internet
   - the device's traffic arrives at the Mac
   - it goes through the tunnel
   - for TVs that opened the dlus setup page, the TV itself reaches YouTube

On the device (example LG webOS): *Settings → General → Network → Wi-Fi Connection → Advanced Wi-Fi Settings → Edit*, untick **Set Automatically**, then enter:

| Field | Value |
|---|---|
| IP address | the device's current address (dlus shows it) |
| Subnet mask | from dlus, e.g. `255.255.255.0` |
| Gateway / Router | **the Mac's address** |
| DNS | `1.1.1.1` |

No app can change a TV's network settings for you; you type them once. If the Mac later gets a different address, update the gateway on the device.

## Tunnel, Direct, kill switch

- **Tunnel:** the device's traffic goes through the VPN. With the **kill switch** on (default), the device gets no internet while the VPN is down, so nothing leaks.
- **Direct:** keep that device's network on *Automatic*. It uses your normal router and never touches the Mac.
- **DNS:** tunnelled devices' DNS also goes through the VPN, so filtered DNS cannot block sites.

## Safety

- **Firewall:** all rules live in their own anchor, `com.apple/dlus`. `/etc/pf.conf` and your other rules are never edited.
- **Only your devices:** a device that is not on the Tunnel or Direct list cannot use the Mac as a router. If someone on your network points their gateway at the Mac, their traffic is dropped, so they cannot reach the VPN's private networks through it.
- **Helper location:** `/Library/PrivilegedHelperTools/dlus-gw`, a root-only folder. Older versions used `/usr/local/libexec`, which can be writable by your user on Macs with Homebrew; **Update helper** moves it.
- **Turning it off:** the router restores IP forwarding to what it was before and removes its rules and routes.
- **The helper checks its input:** it accepts only a fixed set of commands with checked arguments (interface names, IPv4 addresses).
- **No `route-to`:** dlus does not push packets into the tunnel with pf's `route-to`. On macOS 26 that crashed the system (kernel panic) with some VPN tunnels. Traffic follows the normal routing table instead.
- **Crash guard:** if the Mac ever crashes while the router is on, dlus leaves it **off** after the restart and tells you.
- **IPv6:** dlus routes IPv4. If your router gives devices IPv6, a device can reach IPv6 sites past the VPN. Turn off IPv6 on the device (on LG TVs: *Network → IPv6*, on some models) or on your router.
- **Uninstall:** VPN router → **Remove helper**, or `sudo bash /Applications/dlus.app/Contents/Resources/gateway/uninstall.sh`.

## Troubleshooting

Press **Check routes** on the VPN router page. It shows:

- the Mac's internet route
- the VPN tunnels it found
- the VPN apps that are running
- the helper's state, including forwarding, firewall rules and the connections of each device

| Symptom | Likely cause |
|---|---|
| "No VPN carries this Mac's internet" | the VPN is off, in proxy-only mode, or split tunnel |
| Device shows *not using this Mac yet* | its gateway is not the Mac's address, or it got a new address from your router |
| Works, then stops after the Mac sleeps or changes Wi-Fi | the Mac's address changed. Update the gateway on the device |
| Some apps on the TV still blocked | they use IPv6 (see Safety) or their own DNS |
