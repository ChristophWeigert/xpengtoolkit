# Agent notes

## Publishing identity - read this before pushing anything

Everything that leaves this machine is published as **xpengtoolkit**, never as the
maintainer's personal account. Commits are already configured locally as
`xpengtoolkit <xpengtoolkit@users.noreply.github.com>`; the risk is **authentication**,
because `gh` and the macOS keychain on the maintainer's Mac can belong to the personal
account instead.

Before any `git push`, tag or `gh release`, all of these must say `xpengtoolkit`:

```bash
git log -1 --format='%an <%ae> %cn <%ce>'
gh auth status
printf "protocol=https\nhost=github.com\n\n" | git credential fill | grep '^username='
```

If any of them does not, stop and ask. Never publish with a personal account or token
"just this once" - a release is publicly attributed to its creator. The full rule is
in [`.cursor/rules/publish-identity.mdc`](.cursor/rules/publish-identity.mdc).

## Conventions

- Tests are plain scripts, no pytest: `python3 tests/test_config_flow.py`. Some files
  also carry `unittest` classes, so `python3 -m unittest discover -s tests` runs those.
- Credentials never enter git: `custom_components/xpeng/certs/*.pem`,
  `xpeng_tokens.json` and `xpeng_tenant.json` are ignored. Check
  `git diff --cached --name-only` before committing.
- Releases: bump `custom_components/xpeng/manifest.json`, commit with a `0.4.x:` title,
  tag `v0.4.x`, then `gh release create 0.4.x`. The companion app is versioned
  separately in `xpeng_renew/config.yaml`.
