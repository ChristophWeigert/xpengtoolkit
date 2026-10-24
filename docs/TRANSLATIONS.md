# Translations

The integration's Home Assistant UI strings ship in several European languages:

| Language | File | Home Assistant code |
| --- | --- | --- |
| English (source) | `custom_components/xpeng/translations/en.json` | `en` |
| German | `translations/de.json` | `de` |
| Dutch | `translations/nl.json` | `nl` |
| Norwegian Bokmål | `translations/nb.json` | `nb` |
| Danish | `translations/da.json` | `da` |
| Swedish | `translations/sv.json` | `sv` |
| French | `translations/fr.json` | `fr` |

`strings.json` is the reviewed copy of the English strings; `translations/en.json`
is the copy Home Assistant actually reads. A test keeps them identical, so edit
both together.

## How Home Assistant loads these

* Only `translations/<language>.json` is read at runtime. For a custom
  integration `strings.json` is never loaded: it is the source of truth for
  review and drift checks.
* English is always loaded first and the requested language is overlaid **key by
  key**, so a missing key silently falls back to English. That is why
  `tools/check_translations.py` fails on missing and extra keys instead of
  leaving them to be discovered by a user.
* A translated string whose `{placeholders}` differ from English is discarded by
  Home Assistant and English is shown instead, with only a log line. The checker
  treats that as an error.
* Translations are cached until Home Assistant restarts (or the language
  changes), so a test install needs a restart to pick up edited files.

## Rules for a language file

1. **Structure** - same keys, same nesting as `translations/en.json`, nothing
   added or removed. Keep the English key order; it makes review diffs readable.
2. **Placeholders** - keep `{entry}`, `{vin}`, `{email}`, `{code}`, `{error}`,
   `{mode}`, `{option}`, `{pair_search}` and `{url}` exactly as they are. The text
   around them is translated; the braces are not.
3. **Commands, code and URLs** stay verbatim: fenced code blocks, inline
   `commands`, file paths, `https://...` links, `mdi:` icons.
4. **Product and protocol nouns stay as they are**: XPENG, X-Guard, XOS,
   X-Combos, CDU, TBox, ICCID, MQTT, VIN, OTA, HVAC (where the local language
   does not have an established short form).
5. **Markdown** - keep `**bold**`, `` `code` ``, links, list markers and blank
   lines. Descriptions often carry a numbered walkthrough; keep it a walkthrough.
6. **Home Assistant terminology and register** - match the wording Home
   Assistant itself uses in that language (`Batterie`, `Entität`, `Gerät`,
   `Klimaanlage`, ...), and its address form: German and Dutch use the informal
   form (`du`, `je`), French uses `vous`, the Nordic languages use `du`. Stay
   consistent inside the file.
7. **Entity names stay short** - they are displayed under the device name. Do
   not turn "Battery" into a sentence. Entity ids do **not** follow the
   translation: `XPengEntity.suggested_object_id` pins them to the English name,
   so translated names never create localized entity ids.
8. **Do not invent features.** If the English says the captcha needs a human and
   a real browser, the translation says that too - no extra instructions, no
   dropped warnings.

## Adding or updating a language

```bash
cp custom_components/xpeng/translations/en.json \
   custom_components/xpeng/translations/de.json
# translate the values, leave the keys alone
python3 tools/check_translations.py --lang de
python3 tests/test_translations.py
```

The checker prints `WARN … identical to English` for values that are still
English. Single words and product nouns are expected there; a whole paragraph
is not. Review the warnings, then run the full suite.

## Known limits (deliberately out of scope so far)

* Sensor *state* text that is free-form is not translatable
  (`Available: 1.2.3`, `Unknown (4)`); Home Assistant only translates enum
  states declared in `strings.json`.
* Select option labels (`Off`, `Heat 1`, `Ventilation 2`) and the per-schedule
  HVAC switch names are still English.
* The config-flow dropdown labels (sign-in method, command preset, per-group
  permission state) and the capability-group names in the options form are
  hardcoded English in `config_flow.py`. Descriptions quote them verbatim, which
  is why the prose keeps those words in English - see
  `tests/test_translations.py::test_prose_quotes_the_dropdown_labels_verbatim`.
  Home Assistant can translate selector options (`SelectSelectorConfig(
  translation_key=...)` plus a `selector.<key>.options.<value>` section), so a
  later pass can retire this limitation by adding the translation keys and
  translating those label strings.
* The dashboard card (`www/xpeng-vehicle-card.js`), README and documentation
  site are English only.
