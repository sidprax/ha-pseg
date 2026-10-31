# PSE&G Authentication Methods & Setup Guides

PSE&G provides electric smart meter interval data and billing cost details through its MySmartEnergy portal ([mysmartenergy.nj.pseg.com](https://mysmartenergy.nj.pseg.com/)), powered by the MyMeter platform.

This guide provides step-by-step instructions and tradeoffs for all four supported authentication methods.

---

## Comparison Table

| Method | Recommended For | Add-on Needed? | 2FA / OTP Prompts | Cookie Lifespan | Automated Renewal | Initial Setup Effort |
|---|---|---|---|---|---|---|
| [Option 2: Direct MyMeter Automated Sign-In (Recommended)](#option-2-direct-mymeter-automated-sign-in-recommended-with-add-on) | **Recommended**. Fully automated hands-off background renewal with zero 2FA prompts. | Yes ([Auth Add-on](https://github.com/sidprax/ha-pseg-auth-addon)) | None | Auto-renewed | Fully automated and silent | Low (one-time email invite) |
| [Option 1: Direct MyMeter Browser Cookie](#option-1-direct-mymeter-browser-cookie-manual-no-add-on) | Users who prefer not running any companion add-on and don't mind periodic manual cookie re-entry. | No | None | Kept alive while Home Assistant runs (expires after about 10 minutes without requests) | Manual re-paste on expiration | Low (one-time email invite) |
| [Option 3: Primary PSE&G Account Automated Sign-In](#option-3-primary-pseg-account-automated-sign-in-with-add-on-okta-2fa) | Users who prefer using their existing `pseg.com` login and don't mind entering SMS/Email OTP codes. | Yes ([Auth Add-on](https://github.com/sidprax/ha-pseg-auth-addon)) | SMS or Email OTP on setup and renewal | Auto-renewed | Requires OTP entry when prompted | Very Low (uses existing login) |
| [Option 4: Primary PSE&G Account Session Cookie](#option-4-primary-pseg-account-session-cookie-manual-no-add-on) | Quick testing or users who only want to use their primary account without installing the companion add-on. | No | None (handled during browser login) | Kept alive while Home Assistant runs (expires after about 10 minutes without requests) | Manual re-paste on expiration | Very Low (copy from browser DevTools) |

---

## Option 2: Direct MyMeter Automated Sign-In (Recommended, With Add-on)

**Recommended Method**: Best for hands-off installations. Headless Chromium signs into `mysmartenergy.nj.pseg.com` directly, handles the invisible background reCAPTCHA, and captures session cookies. Because direct MyMeter logins do not go through Okta SSO, no SMS or Email OTP code is ever requested.

### Step-by-Step Setup:
1. **Invite a secondary email on MyMeter**: Follow Steps 1 and 2 from Option 1 below.
2. **Install the Companion Add-on**:
   - Add the add-on repository: `https://github.com/sidprax/ha-pseg-auth-addon`.
   - Install **PSE&G Auth Companion**, start it, and enable **Start on boot** and **Watchdog**.
3. **Configure Home Assistant**:
   - In Home Assistant, go to **Settings** > **Devices & Services** > **Add Integration** > **PSE&G Energy**.
   - Select **Automated Sign-In: Direct MyMeter (Recommended, Zero 2FA)**.
   - Enter your invited email address and password, then submit.

### Pros and Cons:
- **Pros**:
  - Fully automated renewal: The coordinator renews cookies in the background without interruptions.
  - Zero 2FA prompts: No verification codes needed on initial setup or renewal.
  - No DevTools needed: No manual cookie copying.
- **Cons**:
  - Requires running the companion container (Chromium and Selenium).

---

## Option 1: Direct MyMeter Browser Cookie (Manual, No Add-on)

Option 1 allows connecting directly without running background containers. It requires zero SMS or Email OTP prompts. MyMeter ends a session after about 10 minutes without requests, so the integration keeps it alive between updates (see [Session Keep-Alive](#session-keep-alive)). You only need to paste a new cookie after Home Assistant was offline for longer than that.

### Step-by-Step Setup:
1. **Invite a secondary email on MyMeter**:
   - Log into your primary PSE&G account at [pseg.com](https://www.pseg.com) and navigate to **MySmartEnergy (MyMeter)**.
   - In the top navigation menu, click **Connect User / Share Access**.
   - Enter an email address (a secondary email or an alias like `yourname+pseg@gmail.com`).
   - Select your electric meter, select access through **No end date**, and send the invitation.
2. **Activate the invited user**:
   - Open the invitation email, click the link, and choose a password and security question.
3. **Capture the cookie**:
   - In your desktop browser, go directly to [mysmartenergy.nj.pseg.com](https://mysmartenergy.nj.pseg.com).
   - Sign in using your **invited email address** and password, and check **Remember Me**. Click **Login**.
   - After the dashboard loads, press **F12** (Developer Tools).
   - Switch to the **Network** tab and refresh the page (**F5**).
   - Click any request to `mysmartenergy.nj.pseg.com` (such as `Dashboard` or `GetIntervalData`).
   - In the **Headers** tab, locate **Request Headers**, find `Cookie:`, and copy the full cookie string (it includes `MM_SID`, `ASP.NET_SessionId`, etc.).
4. **Configure Home Assistant**:
   - In Home Assistant, navigate to **Settings** > **Devices & Services** > **Add Integration** > **PSE&G Energy**.
   - Select **Browser Cookie: Direct MyMeter (Manual, No Add-on)**.
   - Paste the cookie string and submit.

### Pros and Cons:
- **Pros**:
  - No companion add-on or Docker required: Works on Home Assistant Core, Container, Supervised, and OS.
  - Zero 2FA prompts: Does not rely on Okta MFA, SMS codes, or phone numbers.
- **Cons**:
  - Manual re-entry when Home Assistant is offline: The session expires after about 10 minutes without requests, and you then need to paste a new cookie via Settings > Devices & Services > PSE&G > Reconfigure.

---

## Option 3: Primary PSE&G Account Automated Sign-In (With Add-on, Okta 2FA)

Uses your primary `nj.pseg.com` account. Headless Chromium automates Okta SSO navigation and reCAPTCHA challenges, then prompts you in Home Assistant for your SMS or Email verification passcode.

### Step-by-Step Setup:
1. **Install the Companion Add-on**: Add repository `https://github.com/sidprax/ha-pseg-auth-addon` and start **PSE&G Auth Companion**.
2. **Configure Home Assistant**:
   - In Home Assistant, go to **Settings** > **Devices & Services** > **Add Integration** > **PSE&G Energy**.
   - Select **Automated Sign-In: Primary PSE&G Account (Okta 2FA)**.
   - Enter your primary PSE&G credentials, choose your 2FA method (**SMS** or **Email**), and submit.
   - Enter the 6-digit verification code received on your phone or email.

### Pros and Cons:
- **Pros**:
  - Uses existing primary credentials without inviting an extra account.
  - Automates browser navigation and reCAPTCHA.
- **Cons**:
  - Requires OTP entry on initial setup and when sessions expire.
  - Requires running the companion container.

---

## Option 4: Primary PSE&G Account Session Cookie (Manual, No Add-on)

Uses a temporary session cookie copied from an active primary account browser session.

### Step-by-Step Setup:
1. Log into [pseg.com](https://www.pseg.com) with your primary account credentials and complete 2FA.
2. Navigate to **MySmartEnergy (MyMeter)**.
3. Press **F12** to open Developer Tools, select the **Network** tab, and refresh (**F5**).
4. Click any request to `mysmartenergy.nj.pseg.com` (e.g. `Dashboard`).
5. Under **Request Headers**, copy the full value of the `Cookie:` header.
6. In Home Assistant, go to **Settings** > **Devices & Services** > **Add Integration** > **PSE&G Energy**, select **Session Browser Cookie: Primary PSE&G Account**, paste the cookie, and submit.

### Pros and Cons:
- **Pros**:
  - Zero add-on dependencies.
  - Uses existing primary login.
- **Cons**:
  - Manual re-entry when Home Assistant is offline: The session expires after about 10 minutes without requests, and you then need to paste a new cookie via Settings > Devices & Services > PSE&G > Reconfigure.

---

## Session Keep-Alive

MyMeter ends a session after several minutes (measured between 6 to 8 minutes) without requests, while the integration only fetches new data every 4 hours. To keep a pasted cookie (Options 1 and 4) working between updates, the integration sends one small request to MyMeter every 4 to 5 minutes, plus one when Home Assistant shuts down.

- **On by default for Options 1 and 4**: A pasted cookie can't be renewed without you, so the session has to stay alive.
- **Off by default for Options 2 and 3**: The companion add-on signs in again when a session has expired.
- **Change it**: Settings > Devices & Services > PSE&G > Configure > **Keep Session Alive**.
- **When it stops**: Once MyMeter reports the session as expired, no more requests are sent until you sign in again. Home Assistant shows a notification and a repair with what to do.
- **Limits**: If Home Assistant is off or can't reach MyMeter for more than several minutes (a restart that takes long, an internet outage, portal maintenance), the session still expires and needs a new cookie.
