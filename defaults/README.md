# Bundled defaults

PNG templates + a `manifest.json` that the app reads on first launch to
seed a working setup without forcing the user to capture anything. Seed
runs only when no `templates/config.json` exists yet — existing
installs are never touched.

## Layout

```
defaults/
├── manifest.json           # describes rules + bridge templates to seed
├── yes_button.png          # base capture (at source DPI)
├── yes_button.dpi100.png   # scaled variants (100/125/150/175/200 %)
├── yes_button.dpi125.png
├── yes_button.dpi150.png
├── yes_button.dpi175.png
├── yes_button.dpi200.png
├── ... (one set per default rule + bridge template)
```

## Adding / updating defaults

Capture the templates normally in the desktop UI (the regular template
capture writes the base PNG + every DPI variant alongside it), then run:

```bash
uv run python -m scripts.pack_defaults
```

That reads `templates/config.json`, finds every enabled template rule
plus the bridge idle / askuser templates, and writes a fresh
`defaults/manifest.json` + copies the matching PNGs into `defaults/`.
Commit the result.

## Schema (`manifest.json`)

```jsonc
{
  "rules": [
    {
      "name": "Click Yes",
      "matcher": "template",
      "template_filename": "yes_button.png",
      "template_source_dpi": 1.5,    // null = legacy / no DPI variants
      "action": "click",              // or "click+type+enter"
      "text": "continue",             // only used for click+type+enter
      "threshold": 0.90
    }
  ],
  "bridge": {
    "idle_template_filename": "cursor_idle.png",
    "idle_template_source_dpi": 1.5,
    "askuser_template_filename": "cursor_askuser.png",
    "askuser_template_source_dpi": 1.5
  }
}
```

Any field can be omitted. Missing files in `defaults/` are skipped
silently on seed — partial defaults are fine.
