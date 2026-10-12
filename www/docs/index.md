---
layout: home

hero:
  name: Xpengtk
  text: XPENG vehicles in Home Assistant
  tagline: Telemetry, sensors and remote commands over XPENG's own cloud, with no phone and no official app.
  actions:
    - theme: brand
      text: Overview
      link: /guide/overview
    - theme: alt
      text: Installation
      link: /guide/getting-started
    - theme: alt
      text: Entities
      link: /reference/entities

features:
- icon: "🔌"
  title: Push, not polling
  details: |
    A single mutual-TLS MQTT connection carries the vehicle state as the car reports
    it, so entities change the moment something happens.
- icon: "😴"
  title: The car is not woken
  details: |
    A parked car gets one passive query an hour. Only a genuine Home Assistant
    restart asks for a fresh frame.
- icon: "🔁"
  title: Self-renewing credentials
  details: |
    XPENG retires a session about two hours after the login that created it. Two
    helper apps mint a fresh pair and import it before that happens.
- icon: "🔒"
  title: Command permissions
  details: |
    A ceiling on what the integration may do to the car - presets, or per capability
    group. A new install starts at Safe Plus: watch, pre-condition and charge, but not
    unlock.
- icon: "🚗"
  title: Around 120 entities
  details: |
    Battery and range, charging, doors, windows and tyres, climate, location,
    sentinel, OTA state, notifications and a full set of remote commands.

footer: Apache 2.0 Licensed | Unofficial, not affiliated with XPENG
---

::: warning No credentials, certificates or keys are supplied
This project ships **none** of them. You obtain the mTLS client certificate and
key from your own copy of the official app, and your own account credentials, by
following [Certificates](/guide/certificates). How you obtain them, where you
store them, and any distribution you make of those artefacts, is your own
responsibility and entirely at your own risk.
:::
