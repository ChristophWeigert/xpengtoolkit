# 0.6.26 upgrade notes

This contribution is based on upstream 0.6.25, including its parked battery/SOC
and shared MQTT baseline fixes. Install test dependencies with
`python3 -m pip install -r requirements-test.txt` before either
`python3 tools/run_tests.py` or `python3 -m unittest discover -s tests`.

- Minimum Home Assistant is **2024.7**. The paho-mqtt minimum is **2.1.0**, matching
  the callback API used by the integration and the test dependency.
- Existing config entries migrate to **VERSION 2**, retaining legacy entities
  by default. **Downgrading to 0.6.24/0.6.25 cannot load a VERSION 2 entry**;
  keep a pre-upgrade backup and restore its integration configuration as part of
  a deliberate downgrade, rather than manually editing `.storage`.
- New entries use compact controls. Existing unique IDs and evcc interfaces
  remain stable; raw fan telemetry remains available. Remote fan control is
  withheld because the previous command changes target temperature.
- The bundled card is **optional and off by default**. Its Configure toggle
  explicitly enables automatic shared resource management/migration and YAML
  module loading. Disabling the last opt-in or removing the last owning vehicle
  removes its managed resource; dashboard layouts and old copied files remain.
- Confirmed commands no longer accept an unrelated same-subsystem response.
  Firmware that rewrites IDs must echo a usable command identity; otherwise the
  caller gets a timeout and must check measured state before retrying.
- `climate_on` can change the target before a later power refusal. Its partial
  error explains that outcome; no automatic rollback is attempted.
- Pet mode and Sterilisation intentionally remain in COMFORT, available under
  Safe/Safe Plus when supported. Change that group to Read only/Disabled to
  withhold remote climate operation. The car's safety restrictions still apply.
- Window ventilation/close and the documented climate controls were physically
  checked on a P7+. These checks do not establish G6/G9 compatibility. Successful
  remote Pet activation remains unverified while Rear Seat Reminder blocks it.
