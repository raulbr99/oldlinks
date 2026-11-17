# oldlinks

Are the old links in your repo still yours?

A link you added to your README in 2014 can point somewhere else today without anyone touching your repo:

- **The short link changed hands.** On 15-11-2026, old bit.ly links in years-old tweets from Burger King and McDonald's started redirecting to unrelated pages. Bitly suspended the accounts and disabled the links the same day. How the back-halves were taken is not confirmed.
- **The domain expired and someone else bought it.** Whoever registers it next gets every visit from every old link.

`oldlinks` scans the docs of a git repo and flags both cases.

| Level | What it means |
|---|---|
| RED | A short link points somewhere else than when the Wayback Machine first saw it · the domain was registered after the commit that added the link · the domain is not registered at all · the domain shows a parking or for-sale page |
| YELLOW | The short link was deactivated, is broken, or the service is gone (goo.gl) · bit.ly says the back-half was re-created after you added it, but it still points to the same place · the domain has expired or is about to be released · the top-level domain does not exist (a typo like `.cmm`) |
| INFO | A short link with no archived copy to compare with: replace it with the real URL |

For each finding it suggests a replacement when it can: the original destination of a changed short link, or a Wayback Machine copy of the page from before the domain changed hands.

## Use

```
python3 oldlinks.py path/to/repo --html report.html --json report.json --cache .oldlinks-cache.json
```

Python 3.9+, standard library only, and the `whois` command (it comes with macOS and most Linux distributions). Nothing to install. It only reads the repo.

The exit code is 1 when there is a RED finding, so it can run in CI:

- `--cache file` keeps the answers between runs. They are checked again after 7 days (`--max-age-days`), so a domain that changes hands next month still shows up. Failed lookups are never cached.
- `.oldlinks-ignore` in the repo (or `--ignore file`) lists findings you have accepted, one URL or domain per line, with `#` for comments. They stop failing the build.

## How it knows

- **When you added the link:** one pass over `git log -p` of the doc files (`*.md`, `*.rst`, `*.txt`, `*.html`…), keeping the date of the first commit where each URL appears.
- **When the domain was registered:** RDAP, asked to the registry itself (from IANA's bootstrap list). Registries without RDAP (like `.io`) are asked with `whois`. A domain is only called "not registered" when the registry or `whois` says so.
- **What the domain shows today:** one request to its home page, looking for the redirects and pages of parking and domain-sale services.
- **Where a short link pointed before:** the earliest redirect of that short link archived by the Wayback Machine.
- **When a bit.ly back-half was created:** bit.ly's public info page (the link with a `+` at the end).

It waits between requests to the same server. A repo with 2,000 links and 260 domains takes a few minutes the first time.

## Limits

- A domain can change owner without being re-registered (sold or transferred). Registration dates do not show that. The parking check catches some of these cases, not all.
- Without an archived copy, a changed short link cannot be told apart from an unchanged one.
- Only doc files are scanned, not code.
- Some sites block automated requests. For those, the parking check says nothing.

## License

MIT
