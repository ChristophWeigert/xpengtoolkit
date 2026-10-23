# Getting Started

Xpengtk runs on Home Assistant and needs three things: the integration installed,
the mTLS certificates extracted from an XPENG APK, and XPENG account credentials.
Two companion apps keep those credentials alive, so the install runs for weeks
rather than hours.

::: warning
Use a **dedicated** XPENG account for this integration, never your primary one.
XPENG allows one active session per account, so a second client and the phone app
log each other out repeatedly. See
[Use a dedicated account](#use-a-dedicated-account).
:::

## Before you start

Two things that catch people out:

- **Use a dedicated XPENG account, never your primary one** — see
  [below](#use-a-dedicated-account).
- **Download an XPENG APK 3.12.0 or older on purpose.** From 3.13.0 onward the
  certificate is no longer extractable from the package.

## The steps at a glance

**Before step 1, once:** create a dedicated XPENG account and have your main
account share the car to it (see
[below](#use-a-dedicated-account)). Accept the share from the new account if the
app asks — but do not leave the official app signed in as that account.

| # | Step | Where | Roughly how long |
| --- | --- | --- | --- |
| **1** | [Download the integration](#installation) — add the HACS custom repository, install, and **restart Home Assistant**. This adds nothing under Settings | Home Assistant | 2 min |
| **2** | [Extract the certificates](./certificates) from an XPENG APK **3.7.0–3.12.0** | Any computer | 5 min |
| **3** | [Copy the three PEM files onto the Home Assistant host](./certificates#where-to-put-them) | Home Assistant | 2 min |
| **4** | [Install the two apps](./unattended-renewal#install-the-two-apps) (slider solver + credential renewal) and set their tokens | Home Assistant | 5 min |
| **5** | Watch it appear — the app mints the first pair with the solver, creates the integration entry, and renews it on its own timer | Home Assistant | 1 min |

**Only step 2 happens outside Home Assistant**, because extracting the
certificates needs the APK. Nothing else does — including the captcha-gated login,
which the solver app clears.

Steps 4–5 are what make the install unattended. Without them the integration still
works, but only until XPENG retires the session about two hours later, and then a
fresh pair has to be pasted in by hand.

::: tip Not on Home Assistant OS or Supervised?
The Add-on Store does not exist on Container or Core installs, so steps 4–5 run as
two plain Docker containers instead — the same images, with the app options passed
as environment variables. See [Standalone Docker](./unattended-renewal#standalone-docker).
:::

**Step 1 puts the files on the host; it does not add the integration.** The restart is
what registers it. Going to **Settings → Devices & Services → Add Integration** at that
point opens a setup form asking for a token pair that does not exist until step 5 —
which is exactly the trap this note exists for. Adding the entry is either automatic —
the renewal app in step 4 drives that same flow and creates it — or, on
[the manual route](#the-manual-route), the last thing you do once you have a pair to
paste.

## Installation

This is step 1 of the process above; steps 2–5 are under
[Certificates](./certificates) and [Unattended renewal](./unattended-renewal). It
downloads the code and restarts Home Assistant; **it adds nothing to Settings** — the
entry appears in step 5, or you add it yourself on
[the manual route](#the-manual-route).

### HACS (custom repository)

![Adding the custom repository in HACS](../images/hacs-repository.png)

Once it is listed, the repository has its own page in HACS:

![The Xpengtk page in HACS](../images/hacs-download.png)


1. HACS → Integrations → three-dot menu → **Custom repositories**
2. Add `https://github.com/xpengtoolkit/xpengtoolkit` as an **Integration**
3. Install **Xpengtk**, then restart Home Assistant

![Adding the custom repository in HACS](../images/hacs-repository.png)

### Manual install

Copy `custom_components/xpeng/` into your Home Assistant `config/custom_components/`
directory and restart. That is step 1 by hand, and like the HACS route it adds nothing
to Settings: the entry appears in step 5 when the app creates it, or
[by hand](#adding-it-by-hand) once you have a pair.

## The manual route

If you would rather not run the apps at all, or not hand them the account password
before you have seen the integration work, this route mints the first pair on a
desktop instead and replaces steps 4–5. It needs Chrome and Node, and one manual
slider drag.

1. **Mint the first pair.** From the repository root, on a machine with a browser:

   ```bash
   python3 tools/onboard_email_login.py \
       --email YOUR_SECONDARY_EMAIL --password 'YOUR_PASSWORD' --save --mqtt
   ```

   Drag the slider once when the window opens. It writes `xpeng_tokens.json` and
   `xpeng_tenant.json` (note the plural) beside the integration files in this
   checkout, which is what step 2 pastes. Run it on the Home Assistant host against
   the HACS-installed copy instead and it writes to `/config/xpeng-secrets/`, because
   HACS replaces the component directory on every update and would take the pair with
   it.

2. **[Add the integration by hand](#adding-it-by-hand), then import the pair.**
   **Settings → Devices & Services → Add Integration → Xpengtk**, choose
   **Import tokens**, and paste the contents of both files — the whole thing, JSON
   wrapper and all. The paths are
   read inside the Home Assistant container, so they cannot see a file on your
   desktop; pasting is what works here.

With no app installed, nothing renews the session, so repeat this about every two
hours until you install the apps at step 4. A hosted solver removes the slider
drag from this route entirely:

```bash
python3 tools/onboard_email_login.py \
    --email YOUR_SECONDARY_EMAIL --password 'YOUR_PASSWORD' \
    --captcha-solver http://your-solver-host:8080 --save --mqtt
```

With `--captcha-solver` no browser opens: the tool asks the service to clear the
slider and gets the same `validate` a human drag would produce. The service is
self-hosted and separate — see
[xpengtoolkit/slider-solver](https://github.com/xpengtoolkit/slider-solver).

## Adding it by hand

**Most installs never come here.** On the app route, step 4 creates the entry by driving
this same flow — and adding it by hand first is what leaves you looking at the
**Connect your XPENG vehicle** screen with no pair to give it.

Read on if you minted the pair yourself with [the manual route](#the-manual-route), or if
the entry has to be added again later; the Repairs card opens the same import form.

![Add integration, searching for Xpengtk](../images/ha-add-integration.png)

Xpengtk is a custom integration, so it appears under that heading rather than in the brand list. The first screen it opens offers the sign-in methods:

![Connect your XPENG vehicle](../images/ha-setup-connect.png)


On the **Connect your XPENG vehicle** screen, choose **Import tokens from the
onboarding tool** as the sign-in method. It is the route that always completes:
the email/password and Google routes both end at XPENG's slider captcha, which
Home Assistant cannot render or clear.

That first screen asks for the sign-in method and nothing else. Only the Google
and email screens ask for the VIN, because they have no pair to read one out of;
the import screen takes it from the token file.

![The Import onboarding tokens screen](../images/ha-setup-import.png)

Paste the whole of `xpeng_tokens.json` — including the `json` wrapper the tool
writes — and the whole of `xpeng_tenant.json`. **Both must come from the same run
of the tool:** the tenant carries the `deviceId` the tokens are sealed against, so
a pair from different runs is rejected. The VIN is read from the token file when it
carries one, so that field can normally be left blank.

The integration **adopts** the device id from the imported tenant rather than
minting a new one. If you ever see it mint a fresh one instead, the tenant did not
come through, and the credentials will be rejected.

The screen ends with three more fields: **Command permissions** and **Allow the
integration to wake the car**, which decide what the integration is allowed to do to
the car - independent of who is asking - and **Usable battery capacity (kWh)**, the
pack size behind the parked-consumption estimate. Set that one for your car if it is
not a G6 Long Range: a VIN gives the model family, and the G6 and G9 have four packs
each ([the table](../reference/options#usable_battery_kwh)). A new install starts at **Safe Plus**: watch,
pre-condition and charge, but not unlock, open, re-flash or command raw. Accept it, or
pick **Full access** if you want what the integration did before 0.4.20; it can be
changed at any time under **Configure**. See
[Command permissions](../reference/command-permissions).

## Optional: let Home Assistant drive renewals

The renewal app renews on its own timer, so no automation is needed. If you would
rather Home Assistant drove it — for a last-run helper you can put on a dashboard,
or to trigger renewals yourself — set **`renew_interval: 0`** on the app first, so
the two are not both renewing, and use the
[optional automation](./unattended-renewal#home-assistant-side-optional).

## Use a dedicated account

**Create a new XPENG account and share your car to it. Do not sign this
integration in with the account you use on your phone.**

XPENG allows **one active session per account**. This integration signs in as a
second client, so it and the official app fight over that session: your phone app
gets logged out over and over, and the integration gets logged out whenever you
open the app. That is not a bug that can be fixed here — it is how the account
model behaves.

So, once:

1. Create a **new XPENG account** (a second email address works fine).
2. In the **official app**, signed in as your main account, **share the car** to
   that new account (Vehicle → sharing / "share to another user"; the wording
   varies by region).
3. Accept the share from the new account.
4. Sign this integration in with the **new** account only.

The new account needs no phone number and is never used anywhere else. Your main
account stays logged in on your phone, and this integration keeps its own session.
If you have already pointed the integration at your primary account, remove the
entry and add it again with the dedicated one.

**Do not sign the official app in on this account.** A shared car needs no app
visit first — the integration works fully headless — and signing in makes the two
evict each other, because XPENG allows one session per account. If the entities
come up empty, that is the stale-client case in
[Fault finding](./fault-finding#most-entities-are-unknown), not a missing app
login.

## Things that are easy to miss

| Thing | What to know |
| --- | --- |
| **The first run creates the entry itself** | On a fresh install there is nothing to import into, so the renewal app drives the config flow and creates the entry from the pair it just minted. Nothing is pasted, and there is no failure to expect. An answer of `400: No XPENG config entry to import credentials into` means the entry disappeared between the app's check and its call — run it again. |
| **Disabling an entry is not removing it** | Home Assistant hides disabled entries from **Settings → Devices & services**, but the flow still counts them, and a disabled entry keeps the VIN as its `unique_id` — so it answers `already_configured` and refuses to create the replacement. The app's own *is there an entry?* check counted it too, which is why 0.1.17 treats a disabled entry as no entry. Delete an old entry; do not just switch it off. |
| **`solver_url` may need setting** | The default names the solver's container hostname as installed from the store. A local-folder install, and the bare host address, are different — see the [`solver_url` table](./unattended-renewal#if-solver_url-does-not-resolve). |
| **Keep one `device_id`** | Set it once and leave it. Changing it on a live install changes the MQTT client id, and the stale client left behind is the failure in [Fault finding](./fault-finding#most-entities-are-unknown). |
| **A new install starts at Safe Plus** | Since 0.4.20 the integration carries a ceiling on what it may do to the car, and a *new* entry starts at **Safe Plus** - so the OTA switch, the trunk-power outlet, unlocking, the trunk and the windows are not created until you widen it under **Configure**. An entry that predates the option keeps **Full access**, so an update changes nothing. If a control you expected is missing, that is [the reason](./fault-finding#a-control-is-missing), not a broken install. |

## Next

- [Certificates](./certificates) — the three mTLS files, where they come from and where to put them
- [Unattended renewal](./unattended-renewal) — the two apps, their options, and the optional automation
- [Notifications](./notifications) — the Message Centre sensor and the `xpeng_notification` event
- [Fault finding](./fault-finding) — what to check when entities stay `unknown`
- [Entities](../reference/entities) — the full entity reference
