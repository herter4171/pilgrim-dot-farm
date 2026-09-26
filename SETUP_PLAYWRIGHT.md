# Tasking

Set up this Amazon Linux 2023 EC2 host as a persistent headed Playwright MCP server that I can observe through VNC.

The desired end state is:

- GNOME desktop installed.
- TigerVNC running GNOME as `ec2-user` on display `:1`.
- VNC listens only on localhost.
- VNC resolution is 1920x1080.
- VNC starts automatically at boot.
- Node.js 22 installed from the Amazon Linux repositories.
- Playwright MCP installed.
- Playwright MCP runs as `ec2-user`.
- Playwright MCP launches a HEADED browser into `DISPLAY=:1`.
- Playwright MCP listens on localhost TCP 8931.
- Playwright MCP starts automatically after the VNC display is available.
- Browser state is persistent between MCP/server restarts.
- Verify actual public Internet access from the Playwright-controlled browser.
- Do not expose VNC or MCP directly to the Internet. I will tunnel them over SSH.

Do the work rather than merely describing it.

## 1. Inspect the host

First establish:

- Amazon Linux version.
- Architecture:
  `uname -m`
- Current user.
- RAM.
- Existing Node/browser/VNC installations.
- Whether TCP 5901 or 8931 are already in use.

Assume the intended desktop/MCP user is `ec2-user` unless the machine clearly uses another normal login user.

Do not replace working configuration unnecessarily.

## 2. Update packages

Run:

    sudo dnf upgrade -y

Confirm this is Amazon Linux 2023 before proceeding.

## 3. Install GNOME

Amazon Linux 2023 provides GNOME through the Desktop package group.

Run:

    sudo dnf groupinstall "Desktop" -y

Do NOT change the machine to graphical.target merely to make VNC work. VNC will provide the X display independently.

## 4. Install and configure TigerVNC

Install:

    sudo dnf install -y tigervnc-server

Configure display `:1` for ec2-user in:

    /etc/tigervnc/vncserver.users

It should contain:

    :1=ec2-user

Configure `/etc/tigervnc/vncserver-config-defaults`.

Ensure these settings exist:

    session=gnome
    securitytypes=vncauth,tlsvnc
    geometry=1920x1080
    localhost
    alwaysshared

Keep VNC localhost-only. Do not open port 5901 in the EC2 security group.

If `ec2-user` does not already have a VNC password, stop only for the interactive `vncpasswd` operation if absolutely necessary. Otherwise preserve the existing password.

The password must be created as ec2-user, not root:

    sudo -iu ec2-user vncpasswd

Enable VNC:

    sudo systemctl enable --now vncserver@:1.service

Verify:

    systemctl status vncserver@:1.service --no-pager
    ss -lntp | grep 5901

It should be listening locally on port 5901.

Confirm an X display exists:

    sudo -iu ec2-user env DISPLAY=:1 xdpyinfo >/dev/null

Install `xorg-x11-utils` or the appropriate AL2023 package containing `xdpyinfo` if necessary.

## 5. Disable GNOME idle locking for this VNC desktop

As ec2-user, against DISPLAY=:1 when necessary, disable idle blanking/locking so the browser remains visible.

At minimum try:

    sudo -iu ec2-user gsettings set org.gnome.desktop.session idle-delay 0

If GNOME requires the VNC session's DBus environment, perform the equivalent setting from a terminal inside the session or using the proper DBus environment.

Do not spend excessive time on this if VNC itself is working.

## 6. Install Node.js 22

Amazon Linux 2023 has namespaced Node.js packages.

Install:

    sudo dnf install -y nodejs22 nodejs22-npm

Select Node 22 as the active Node version if necessary:

    sudo alternatives --set node /usr/bin/node-22

Verify:

    node --version
    npm-22 --version

Node should be version 22.x.

## 7. Install Playwright MCP

Prefer a global installation so systemd does not depend on nvm, shell initialization, or an interactive login.

Run:

    sudo npm-22 install -g @playwright/mcp@latest

Determine the actual installed executable rather than guessing:

    command -v playwright-mcp || true
    npm-22 prefix -g
    npm-22 bin -g 2>/dev/null || true

Also verify:

    npx --yes @playwright/mcp@latest --help

The installed version must support at least:

    --port
    --host
    --browser
    --user-data-dir

Do not use Docker. We specifically need a headed browser on the VNC X display.

## 8. Install a usable browser

The target is a headed Chromium-family browser.

### x86_64

If architecture is x86_64, prefer installing Google Chrome Stable:

    sudo dnf install -y \
      https://dl.google.com/linux/direct/google-chrome-stable_current_x86_64.rpm

Verify:

    google-chrome-stable --version

Use Playwright MCP with:

    --browser chrome

This avoids depending unnecessarily on Playwright's bundled browser compatibility with Amazon Linux.

### aarch64

Google's normal Linux Chrome RPM is not available for aarch64.

On aarch64:

1. Determine whether a suitable Chromium package is available from the enabled AL2023 repositories.
2. If not, use Playwright's browser installation mechanism.
3. Launch it manually on DISPLAY=:1 and identify any missing shared libraries with `ldd` or the browser's error output.
4. Install the corresponding AL2023 packages with `dnf`.
5. Do not add random third-party repositories unless absolutely necessary.
6. Do not declare success until a real headed browser opens on DISPLAY=:1.

## 9. Create persistent Playwright state

Create:

    /home/ec2-user/.local/share/playwright-mcp
    /home/ec2-user/.local/share/playwright-mcp/profile

Set ownership:

    sudo chown -R ec2-user:ec2-user \
      /home/ec2-user/.local/share/playwright-mcp

The MCP server should use:

    --user-data-dir=/home/ec2-user/.local/share/playwright-mcp/profile

Do not use `--isolated`.

## 10. Prove headed browser operation BEFORE creating systemd

Run Playwright MCP interactively as ec2-user on the VNC display.

For x86_64 with Google Chrome, conceptually:

    sudo -iu ec2-user env \
      DISPLAY=:1 \
      HOME=/home/ec2-user \
      <PLAYWRIGHT_MCP_EXECUTABLE> \
        --browser chrome \
        --host 127.0.0.1 \
        --port 8931 \
        --user-data-dir=/home/ec2-user/.local/share/playwright-mcp/profile

If using `npx` instead of the global executable:

    sudo -iu ec2-user env \
      DISPLAY=:1 \
      HOME=/home/ec2-user \
      npx --yes @playwright/mcp@latest \
        --browser chrome \
        --host 127.0.0.1 \
        --port 8931 \
        --user-data-dir=/home/ec2-user/.local/share/playwright-mcp/profile

Do NOT pass `--headless`.

Look at the VNC desktop and confirm that a browser window actually appears when an MCP browser operation starts.

The MCP endpoint should be:

    http://127.0.0.1:8931/mcp

Terminate this test instance before proceeding to systemd.

## 11. Create a systemd service

Create:

    /etc/systemd/system/playwright-mcp.service

Use the actual absolute executable path discovered above.

The unit should resemble:

    [Unit]
    Description=Playwright MCP headed browser server
    After=network-online.target vncserver@:1.service
    Wants=network-online.target
    Requires=vncserver@:1.service

    [Service]
    Type=simple
    User=ec2-user
    Group=ec2-user
    Environment=HOME=/home/ec2-user
    Environment=DISPLAY=:1
    WorkingDirectory=/home/ec2-user
    ExecStart=<ABSOLUTE_PLAYWRIGHT_MCP_EXECUTABLE> --browser chrome --host 127.0.0.1 --port 8931 --user-data-dir=/home/ec2-user/.local/share/playwright-mcp/profile
    Restart=on-failure
    RestartSec=3

    [Install]
    WantedBy=multi-user.target

If the architecture/browser choice requires something other than `--browser chrome`, substitute the browser configuration that was actually proven to work.

Do not put `npx @latest` in the service if a stable absolute globally-installed executable is available.

If the executable is a JS file whose shebang depends on `/usr/bin/env node`, verify `/usr/bin/node` resolves to Node 22 under systemd.

Then:

    sudo systemctl daemon-reload
    sudo systemctl enable --now playwright-mcp.service

Verify:

    systemctl status playwright-mcp.service --no-pager
    journalctl -u playwright-mcp.service -n 100 --no-pager
    ss -lntp | grep 8931

Expected listener:

    127.0.0.1:8931

NOT:

    0.0.0.0:8931

## 12. Exercise MCP, not merely the HTTP socket

Configure or use an MCP client against:

    http://127.0.0.1:8931/mcp

Use the Playwright tools to:

1. Navigate to:
   `https://example.com/`
2. Read the page title/content.
3. Navigate to:
   `https://www.google.com/`
   or another ordinary public HTTPS site.
4. Obtain a browser snapshot.
5. Confirm that these pages are visible in the VNC browser window.

This proves three separate things:

- MCP transport works.
- Browser control works.
- Browser public Internet egress works.

A successful `curl` from the EC2 shell alone is NOT sufficient evidence of browser egress.

If browser navigation fails while shell curl works, investigate browser-specific DNS, proxy, sandbox, certificate, and launch errors rather than blaming MCP networking.

## 13. Reboot test

Reboot the instance once.

After reboot verify:

    systemctl is-active vncserver@:1.service
    systemctl is-active playwright-mcp.service
    ss -lntp | grep -E '5901|8931'

Then exercise a real Playwright navigation again.

The browser should once again appear on VNC display :1.

## 14. Local access instructions

Do not modify the EC2 security group to expose either service.

VNC should be reached through SSH forwarding:

    ssh -L 5901:127.0.0.1:5901 ec2-user@EC2_HOST

Then the local VNC client connects to:

    127.0.0.1:5901

Likewise, if I want the MCP endpoint locally:

    ssh -L 8931:127.0.0.1:8931 ec2-user@EC2_HOST

and the MCP client uses:

    http://127.0.0.1:8931/mcp

If I instead use SSH RemoteForward from another machine into this EC2 host, leave the actual Playwright MCP service bound to localhost unless there is a concrete reason not to.

## 15. Final report

When finished, give me a concise report containing:

- Architecture.
- AL2023 version.
- VNC service status.
- VNC display and port.
- Browser installed and version.
- Node version.
- Playwright MCP version.
- Exact Playwright MCP ExecStart command.
- MCP endpoint.
- Whether browser navigation to the public Internet succeeded.
- Whether the browser was visibly present in VNC.
- Whether both services survived a reboot.
- Any deviations from this procedure and why.

Do not report success based solely on package installation or open TCP ports. Exercise the complete path.
